#!/usr/bin/env python3
"""Run an interactive command through a PTY and notify on blocking prompts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from telegram_common import build_telegram_text, log_debug, send_telegram, send_telegram_message


CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.S)
BACKSPACE_RE = re.compile(r".\x08")
PROMPT_READY_RE = re.compile(r"(Claude|Codex) has written up a plan and is ready to execute", re.I)
PROCEED_RE = re.compile(r"Would you like to proceed\?", re.I)
OPTION_RE = re.compile(
    r"(clear context \(\d+% used\)|auto-accept edits|manually approve edits|Type here to tell (Claude|Codex) what to change)",
    re.I,
)
APPROVAL_LINE_RE = re.compile(
    r"^(Approval requested:?|.*needs your approval\.|Tool call needs your approval\. Reason:|Approval needed(?: in .*)?$|Do you want to approve .*)$",
    re.I,
)
APPROVAL_DETAIL_LINE_RE = re.compile(
    r"^(Command:|Suggested rule:|Approve$|Decline$|Deny$|Allow once$|Allow for session$)",
    re.I,
)
PROMPT_RESEND_COOLDOWN_SECS = 180
PROMPT_STABILIZE_SECS = 3.0  # wait for streaming to settle before notifying
DEFAULT_APPROVAL_TTL_SECS = 900
APPROVAL_OPTIONS = ("Approve", "Decline", "Deny", "Allow once", "Allow for session")
ARROW_UP = "\x1b[A"
ARROW_DOWN = "\x1b[B"
ENTER_KEY = "\r"


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--label", default=os.environ.get("AGENT_NOTIFY_LABEL", ""))
    parser.add_argument("--cwd", default=os.environ.get("AGENT_NOTIFY_CWD", os.getcwd()))
    parser.add_argument("--log", default=os.environ.get("AGENT_NOTIFY_LOG", ""))
    parser.add_argument("--preview-chars", type=int, default=int(os.environ.get("AGENT_NOTIFY_PREVIEW_CHARS", "220")))
    parser.add_argument("--delete-after", type=int, default=int(os.environ.get("AGENT_NOTIFY_DELETE_AFTER", "0")))
    parser.add_argument("--delete-queue-path", default=os.environ.get("AGENT_NOTIFY_DELETE_QUEUE_PATH", ""))
    parser.add_argument("--", dest="separator", action="store_true")
    args, remainder = parser.parse_known_args()
    if remainder and remainder[0] == "--":
        remainder = remainder[1:]
    if not remainder:
        raise SystemExit("agent_telegram_tty_proxy.py: missing command")
    return args, remainder


def strip_ansi(text: str) -> str:
    text = OSC_RE.sub("", text)
    text = CSI_RE.sub("", text)
    return text


def strip_backspaces(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = BACKSPACE_RE.sub("", text)
    return text.replace("\x08", "")


def normalize_output(text: str) -> str:
    text = strip_ansi(text)
    text = strip_backspaces(text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def safe_terminal_key(terminal_key: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in terminal_key).replace(" ", "_")


def pending_dir(state_dir: Path) -> Path:
    return state_dir / "pending"


def control_path(state_dir: Path, terminal_key: str) -> Path:
    return state_dir / "control" / f"{safe_terminal_key(terminal_key)}.jsonl"


def approval_message(prompt: str) -> str:
    return f"Input needed:\n{prompt}\n\nReply to this message with go or no."


def extract_approval_options(prompt: str) -> list[str]:
    options: list[str] = []
    for line in prompt.splitlines():
        stripped = line.strip()
        for candidate in APPROVAL_OPTIONS:
            if stripped.lower() == candidate.lower():
                options.append(candidate)
                break
    return options


def approval_start_index(option_count: int) -> int:
    if option_count <= 0:
        return 0
    configured = parse_env_int("AGENT_NOTIFY_APPROVAL_START_INDEX", 0)
    if configured < 0:
        return 0
    if configured >= option_count:
        return option_count - 1
    return configured


def menu_input_sequence(*, options: list[str], target_labels: tuple[str, ...]) -> str | None:
    if not options:
        return None
    target_index = -1
    lowered = {option.lower(): index for index, option in enumerate(options)}
    for label in target_labels:
        match = lowered.get(label.lower())
        if match is not None:
            target_index = match
            break
    if target_index < 0:
        return None
    start_index = approval_start_index(len(options))
    if target_index == start_index:
        return ENTER_KEY
    if target_index > start_index:
        return ARROW_DOWN * (target_index - start_index) + ENTER_KEY
    return ARROW_UP * (start_index - target_index) + ENTER_KEY


def approval_inputs_for_prompt(prompt: str) -> tuple[str, str]:
    options = extract_approval_options(prompt)
    approve_menu = menu_input_sequence(
        options=options,
        target_labels=("Approve", "Allow once", "Allow for session"),
    )
    decline_menu = menu_input_sequence(options=options, target_labels=("Decline", "Deny"))
    if approve_menu is not None and decline_menu is not None:
        return approve_menu, decline_menu
    return approval_inputs()


def maybe_extract_approval_prompt(buffer: str) -> str | None:
    recent_lines = [line.rstrip() for line in buffer.splitlines() if line.strip()]
    if recent_lines:
        recent_lines = recent_lines[-24:]
    for index, line in enumerate(recent_lines):
        if not APPROVAL_LINE_RE.search(line.strip()):
            continue
        trimmed = recent_lines[index : index + 16]
        if not any(APPROVAL_DETAIL_LINE_RE.search(candidate.strip()) for candidate in trimmed[1:]):
            continue
        return "\n".join(trimmed)[-1800:]
    return None


def maybe_extract_prompt(buffer: str) -> str | None:
    prompt = maybe_extract_approval_prompt(buffer)
    if prompt:
        return prompt
    has_ready = PROMPT_READY_RE.search(buffer) is not None
    has_proceed = PROCEED_RE.search(buffer) is not None
    option_hits = len(OPTION_RE.findall(buffer))
    if not has_ready and not (has_proceed and option_hits >= 1) and option_hits < 2:
        return None

    positions = []
    for regex in (PROMPT_READY_RE, PROCEED_RE, OPTION_RE):
        match = regex.search(buffer)
        if match:
            positions.append(match.start())
    if not positions:
        return None
    snippet = buffer[min(positions) :]
    lines = [line.rstrip() for line in snippet.splitlines()]
    lines = [line for line in lines if line.strip()]
    if len(lines) > 12:
        lines = lines[-12:]
    if not lines:
        return None
    return "\n".join(lines)[-1800:]


def normalize_prompt_key(prompt: str) -> str:
    return " ".join(prompt.split())[:1200]


def parse_env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def approval_inputs() -> tuple[str, str]:
    approve_value = os.environ.get("AGENT_NOTIFY_APPROVE_INPUT", "go")
    decline_value = os.environ.get("AGENT_NOTIFY_DECLINE_INPUT", "no")
    return approve_value.rstrip("\n") + "\n", decline_value.rstrip("\n") + "\n"


def store_pending_record(path: Path, record: dict[str, object], *, log_path: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        compact = " ".join(str(exc).split())[:200]
        log_debug(log_path, f"pending record write failed response={compact}")


def register_pending_approval(
    prompt: str,
    *,
    prompt_hash: str,
    cwd: str,
    label: str,
    log_path: str,
    state_dir: Path | None,
    terminal_key: str,
) -> str | None:
    if state_dir is None or not terminal_key:
        send_telegram(
            build_telegram_text(cwd, label, approval_message(prompt)),
            bot_token=os.environ.get("TG_BOT_TOKEN", ""),
            chat_id=os.environ.get("TG_CHAT_ID", ""),
            log_path=log_path,
        )
        return None

    token = prompt_hash[:12]
    now = time.time()
    ttl = parse_env_int("AGENT_NOTIFY_APPROVAL_TTL_SECS", DEFAULT_APPROVAL_TTL_SECS)
    approve_input, decline_input = approval_inputs_for_prompt(prompt)
    payload = send_telegram_message(
        build_telegram_text(cwd, label, approval_message(prompt)),
        bot_token=os.environ.get("TG_BOT_TOKEN", ""),
        chat_id=os.environ.get("TG_CHAT_ID", ""),
        log_path=log_path,
        delete_after=0,
        delete_queue_path="",
    )
    if payload is None:
        return None

    message_id = payload.get("result", {}).get("message_id")
    record = {
        "token": token,
        "status": "pending",
        "prompt_hash": prompt_hash,
        "prompt": prompt,
        "label": label,
        "cwd": cwd,
        "terminal_key": terminal_key,
        "approval_options": extract_approval_options(prompt),
        "approval_start_index": approval_start_index(len(extract_approval_options(prompt))),
        "control_path": str(control_path(state_dir, terminal_key)),
        "approve_input": approve_input,
        "decline_input": decline_input,
        "telegram_message_id": message_id if isinstance(message_id, int) else None,
        "created_at": now,
        "expires_at": now + ttl if ttl > 0 else 0,
    }
    record_path = pending_dir(state_dir) / f"{safe_terminal_key(terminal_key)}-{token}.json"
    store_pending_record(record_path, record, log_path=log_path)
    log_debug(log_path, f"approval registered token={token} terminal={terminal_key}")
    return token


def open_control_reader(state_dir: Path | None, terminal_key: str, *, log_path: str):
    if state_dir is None or not terminal_key:
        return None
    target = control_path(state_dir, terminal_key)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = target.open("a+", encoding="utf-8")
        handle.seek(0, os.SEEK_END)
    except OSError as exc:
        compact = " ".join(str(exc).split())[:200]
        log_debug(log_path, f"control reader open failed response={compact}")
        return None
    return handle


def read_control_actions(handle) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    while True:
        line = handle.readline()
        if not line:
            break
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            actions.append(payload)
    return actions


def set_winsize(target_fd: int, source_fd: int) -> None:
    try:
        import fcntl

        packed = fcntl.ioctl(source_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(target_fd, termios.TIOCSWINSZ, packed)
    except OSError:
        return


def main() -> int:
    args, command = parse_args()
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    if not (os.isatty(stdin_fd) and os.isatty(stdout_fd)):
        return subprocess.call(command)

    master_fd, slave_fd = pty.openpty()
    set_winsize(slave_fd, stdin_fd)
    child = subprocess.Popen(command, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd)
    os.close(slave_fd)

    def on_winch(signum: int, frame: object) -> None:
        _ = signum, frame
        set_winsize(master_fd, stdin_fd)

    previous_handler = signal.signal(signal.SIGWINCH, on_winch)
    original_tty = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)

    raw_state_dir = os.environ.get("AGENT_NOTIFY_STATE_DIR", "").strip()
    state_dir = Path(raw_state_dir) if raw_state_dir else None
    terminal_key = os.environ.get("AGENT_NOTIFY_TERMINAL_KEY", "").strip()
    control_handle = open_control_reader(state_dir, terminal_key, log_path=args.log)
    decoded_buffer = ""
    last_prompt_hash = ""
    last_prompt_sent_at = 0.0
    pending_prompt: str | None = None
    pending_approval_prompt: str | None = None
    pending_prompt_hash = ""
    pending_first_seen = 0.0
    try:
        while True:
            if control_handle is not None:
                for action in read_control_actions(control_handle):
                    token = str(action.get("token") or "").strip().lower()
                    action_name = str(action.get("action") or "").strip().lower()
                    injected = str(action.get("input_text") or "")
                    if not injected:
                        log_debug(args.log, f"control ignored token={token or 'missing'} action={action_name or 'missing'}")
                        continue
                    try:
                        os.write(master_fd, injected.encode("utf-8"))
                    except OSError as exc:
                        compact = " ".join(str(exc).split())[:200]
                        log_debug(args.log, f"control write failed response={compact}")
                        continue
                    log_debug(args.log, f"control injected token={token} action={action_name}")

            ready, _, _ = select.select([master_fd, stdin_fd], [], [], 0.1)
            if master_fd in ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    os.write(stdout_fd, chunk)
                    decoded_buffer = normalize_output(decoded_buffer + chunk.decode("utf-8", "replace"))[-16000:]
                    approval_prompt = maybe_extract_approval_prompt(decoded_buffer)
                    prompt = maybe_extract_prompt(decoded_buffer)
                    if prompt:
                        prompt_key = normalize_prompt_key(prompt)
                        prompt_hash = hashlib.sha256(prompt_key.encode("utf-8")).hexdigest()
                        if prompt_hash != pending_prompt_hash:
                            # Prompt changed (still streaming) — reset debounce timer
                            pending_prompt = prompt
                            pending_approval_prompt = approval_prompt
                            pending_prompt_hash = prompt_hash
                            pending_first_seen = time.monotonic()
                elif child.poll() is not None:
                    break

            # Check if pending prompt has stabilized (streaming finished)
            if pending_prompt_hash:
                current = time.monotonic()
                if current - pending_first_seen >= PROMPT_STABILIZE_SECS:
                    should_send = (
                        pending_prompt_hash != last_prompt_hash
                        or current - last_prompt_sent_at >= PROMPT_RESEND_COOLDOWN_SECS
                    )
                    if should_send:
                        last_prompt_hash = pending_prompt_hash
                        last_prompt_sent_at = current
                        preview = " ".join(pending_prompt.split())[: args.preview_chars]
                        log_debug(args.log, f"prompt matched (stabilized) preview={preview}")
                        if pending_approval_prompt:
                            register_pending_approval(
                                pending_approval_prompt,
                                prompt_hash=pending_prompt_hash,
                                cwd=args.cwd,
                                label=args.label,
                                log_path=args.log,
                                state_dir=state_dir,
                                terminal_key=terminal_key,
                            )
                        else:
                            send_telegram(
                                build_telegram_text(args.cwd, args.label, f"Input needed:\n{pending_prompt}"),
                                bot_token=os.environ.get("TG_BOT_TOKEN", ""),
                                chat_id=os.environ.get("TG_CHAT_ID", ""),
                                log_path=args.log,
                                delete_after=args.delete_after,
                                delete_queue_path=args.delete_queue_path,
                            )
                    pending_prompt = None
                    pending_approval_prompt = None
                    pending_prompt_hash = ""
                    pending_first_seen = 0.0

            if stdin_fd in ready:
                try:
                    user_input = os.read(stdin_fd, 1024)
                except OSError:
                    user_input = b""
                if user_input:
                    os.write(master_fd, user_input)

            if child.poll() is not None and master_fd not in ready:
                break
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, original_tty)
        signal.signal(signal.SIGWINCH, previous_handler)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if control_handle is not None:
            try:
                control_handle.close()
            except OSError:
                pass

    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
