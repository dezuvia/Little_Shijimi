#!/usr/bin/env python3
"""Run an interactive command through a PTY and notify on blocking prompts."""

from __future__ import annotations

import argparse
import hashlib
import os
import pty
import re
import select
import signal
import subprocess
import sys
import termios
import tty
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from telegram_common import build_telegram_text, log_debug, send_telegram


CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.S)
BACKSPACE_RE = re.compile(r".\x08")
PROMPT_READY_RE = re.compile(r"(Claude|Codex) has written up a plan and is ready to execute", re.I)
PROCEED_RE = re.compile(r"Would you like to proceed\?", re.I)
OPTION_RE = re.compile(
    r"(clear context \(\d+% used\)|auto-accept edits|manually approve edits|Type here to tell (Claude|Codex) what to change)",
    re.I,
)
APPROVAL_TRIGGER_RE = re.compile(
    r"(Approval requested:?|needs your approval\.|Tool call needs your approval\. Reason:|Approval needed in |Do you want to approve )",
    re.I,
)
APPROVAL_DETAIL_RE = re.compile(r"(Command:|Suggested rule:|Approve|Decline|Deny|Allow once|Allow for session)", re.I)
PROMPT_RESEND_COOLDOWN_SECS = 180


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


def maybe_extract_prompt(buffer: str) -> str | None:
    approval_match = APPROVAL_TRIGGER_RE.search(buffer)
    if approval_match:
        snippet = buffer[approval_match.start() :]
        lines = [line.rstrip() for line in snippet.splitlines()]
        lines = [line for line in lines if line.strip()]
        if not lines:
            return None
        trimmed = lines[:16]
        if not APPROVAL_DETAIL_RE.search("\n".join(trimmed)):
            trimmed = lines[:8]
        return "\n".join(trimmed)[-1800:]

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

    decoded_buffer = ""
    last_prompt_hash = ""
    last_prompt_sent_at = 0.0
    try:
        while True:
            ready, _, _ = select.select([master_fd, stdin_fd], [], [], 0.1)
            if master_fd in ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    os.write(stdout_fd, chunk)
                    decoded_buffer = normalize_output(decoded_buffer + chunk.decode("utf-8", "replace"))[-16000:]
                    prompt = maybe_extract_prompt(decoded_buffer)
                    if prompt:
                        prompt_key = normalize_prompt_key(prompt)
                        prompt_hash = hashlib.sha256(prompt_key.encode("utf-8")).hexdigest()
                        current = time.monotonic()
                        if prompt_hash != last_prompt_hash or current - last_prompt_sent_at >= PROMPT_RESEND_COOLDOWN_SECS:
                            last_prompt_hash = prompt_hash
                            last_prompt_sent_at = current
                            preview = " ".join(prompt.split())[: args.preview_chars]
                            log_debug(args.log, f"prompt matched preview={preview}")
                            send_telegram(
                                build_telegram_text(args.cwd, args.label, f"Input needed:\n{prompt}"),
                                bot_token=os.environ.get("TG_BOT_TOKEN", ""),
                                chat_id=os.environ.get("TG_CHAT_ID", ""),
                                log_path=args.log,
                                delete_after=args.delete_after,
                                delete_queue_path=args.delete_queue_path,
                            )
                elif child.poll() is not None:
                    break

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

    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
