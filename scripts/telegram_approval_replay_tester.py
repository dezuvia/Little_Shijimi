#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from agent_telegram_tty_proxy import control_path, maybe_extract_approval_prompt


SCRIPT_DIR = Path(__file__).resolve().parent
PROXY_SCRIPT = SCRIPT_DIR / "agent_telegram_tty_proxy.py"
LISTENER_SCRIPT = SCRIPT_DIR / "telegram_reply_listener.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-root", default=str(Path.home() / ".codex" / "sessions"))
    parser.add_argument("--sample-file", default="")
    parser.add_argument("--terminal-key", default="ttys-test-A")
    parser.add_argument("--decoy-terminal-key", default="ttys-test-B")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--keep-temp", action="store_true")
    return parser.parse_args()


def iter_strings(value: Any):
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for child in value.values():
            yield from iter_strings(child)
        return
    if isinstance(value, list):
        for child in value:
            yield from iter_strings(child)


def find_history_prompt(root: Path, sample_file: str) -> tuple[str, Path]:
    candidates: list[Path]
    if sample_file:
        candidates = [Path(sample_file)]
    else:
        candidates = sorted(root.rglob("*.jsonl"), reverse=True)
    for path in candidates:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for text in iter_strings(payload):
                        prompt = maybe_extract_approval_prompt(text)
                        if prompt:
                            return prompt, path
        except OSError:
            continue
    raise SystemExit("No historical approval prompt found.")


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_child_script(path: Path) -> None:
    write_file(
        path,
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from __future__ import annotations",
                "import argparse",
                "import os",
                "import select",
                "import termios",
                "import time",
                "import tty",
                "from pathlib import Path",
                "import sys",
                "",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--prompt-file', required=True)",
                "parser.add_argument('--result-file', required=True)",
                "args = parser.parse_args()",
                "prompt = Path(args.prompt_file).read_text(encoding='utf-8')",
                "sys.stdout.write(prompt + '\\n')",
                "sys.stdout.flush()",
                "stdin_fd = sys.stdin.fileno()",
                "original_tty = termios.tcgetattr(stdin_fd)",
                "tty.setraw(stdin_fd)",
                "captured = bytearray()",
                "try:",
                "    deadline = time.time() + 5.0",
                "    while time.time() < deadline:",
                "        ready, _, _ = select.select([stdin_fd], [], [], 0.1)",
                "        if stdin_fd not in ready:",
                "            continue",
                "        chunk = os.read(stdin_fd, 32)",
                "        if not chunk:",
                "            continue",
                "        captured.extend(chunk)",
                "        if b'\\r' in chunk or b'\\n' in chunk:",
                "            break",
                "finally:",
                "    termios.tcsetattr(stdin_fd, termios.TCSADRAIN, original_tty)",
                "Path(args.result_file).write_text(captured.hex() + '\\n', encoding='utf-8')",
                "sys.stdout.write(f'RECEIVED_HEX:{captured.hex()}\\n')",
                "sys.stdout.flush()",
                "",
            ]
        ),
    )
    path.chmod(0o755)


def wait_for_path(path: Path, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def wait_for_pending_record(root: Path, timeout: float) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        matches = sorted((root / "pending").glob("*.json"))
        if matches:
            return matches[0]
        time.sleep(0.1)
    raise SystemExit("Pending approval record was not created.")


def drain_fd(fd: int, timeout: float) -> str:
    chunks: list[str] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if fd not in ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk.decode("utf-8", "replace"))
        if "RECEIVED_HEX:" in chunks[-1]:
            break
    return "".join(chunks)


def terminate_process(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def main() -> int:
    args = parse_args()
    prompt, source_path = find_history_prompt(Path(args.history_root), args.sample_file)
    temp_root = Path(tempfile.mkdtemp(prefix="telegram-approval-replay."))
    state_dir = temp_root / "state"
    mock_dir = temp_root / "mock"
    prompt_file = temp_root / "prompt.txt"
    result_file = temp_root / "result.txt"
    child_script = temp_root / "approval_child.py"
    log_path = temp_root / "tester.log"
    write_file(prompt_file, prompt)
    build_child_script(child_script)
    write_file(
        mock_dir / "sendMessage.response.json",
        json.dumps({"ok": True, "result": {"message_id": 4242}}, ensure_ascii=False) + "\n",
    )

    decoy_path = control_path(state_dir, args.decoy_terminal_key)
    write_file(decoy_path, "")

    env = os.environ.copy()
    env.update(
        {
            "TG_BOT_TOKEN": "test-bot",
            "TG_CHAT_ID": "test-chat",
            "TG_API_MOCK_DIR": str(mock_dir),
            "AGENT_NOTIFY_STATE_DIR": str(state_dir),
            "AGENT_NOTIFY_TERMINAL_KEY": args.terminal_key,
            "AGENT_NOTIFY_APPROVE_INPUT": "go",
            "AGENT_NOTIFY_DECLINE_INPUT": "no",
        }
    )

    master_fd, slave_fd = pty.openpty()
    proxy_proc: subprocess.Popen[bytes] | None = None
    listener_proc: subprocess.Popen[bytes] | None = None
    try:
        proxy_proc = subprocess.Popen(
            [
                sys.executable,
                str(PROXY_SCRIPT),
                "--label",
                args.terminal_key,
                "--cwd",
                str(Path.cwd()),
                "--log",
                str(log_path),
                "--",
                sys.executable,
                str(child_script),
                "--prompt-file",
                str(prompt_file),
                "--result-file",
                str(result_file),
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
        )
        os.close(slave_fd)

        pending_record_path = wait_for_pending_record(state_dir, args.timeout)
        pending_record = read_json(pending_record_path)
        reply_update = {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 9001,
                        "chat": {"id": "test-chat"},
                        "text": "go",
                        "reply_to_message": {"message_id": pending_record["telegram_message_id"]},
                    },
                }
            ],
        }
        write_file(mock_dir / "getUpdates.response.json", json.dumps(reply_update, ensure_ascii=False) + "\n")
        listener_proc = subprocess.Popen(
            [
                sys.executable,
                str(LISTENER_SCRIPT),
                "--state-dir",
                str(state_dir),
                "--log",
                str(log_path),
                "--idle-exit",
                "5",
                "--poll-timeout",
                "1",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        output = drain_fd(master_fd, args.timeout)
        if not wait_for_path(result_file, 1.0):
            raise SystemExit(f"Replay did not deliver terminal input.\n\nPTY output:\n{output}")

        refreshed_record = read_json(pending_record_path)
        expected_input = str(refreshed_record.get("approve_input") or "")
        delivered_hex = result_file.read_text(encoding="utf-8").strip()
        expected_hex = expected_input.encode("utf-8").hex()
        if delivered_hex != expected_hex:
            raise SystemExit(
                f"Expected terminal input hex {expected_hex!r}, got {delivered_hex!r}.\n\nPTY output:\n{output}"
            )

        target_control = control_path(state_dir, args.terminal_key)
        if not target_control.exists():
            raise SystemExit("Target control file was not created.")
        control_lines = [line for line in target_control.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not control_lines:
            raise SystemExit("Target control file is empty.")
        control_event = json.loads(control_lines[-1])
        if control_event.get("action") != "approve":
            raise SystemExit(f"Expected approve action, got {control_event!r}.")
        if control_event.get("input_text") != expected_input:
            raise SystemExit(f"Control event did not carry expected input: {control_event!r}.")

        decoy_lines = [line for line in decoy_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if decoy_lines:
            raise SystemExit("Decoy terminal control file received unexpected input.")

        if refreshed_record.get("status") != "approve_dispatched":
            raise SystemExit(f"Pending record did not reach approve_dispatched: {refreshed_record!r}")

        print(f"History sample: {source_path}")
        print(f"Pending record: {pending_record_path}")
        print(f"Terminal key: {args.terminal_key}")
        print("Replay result: PASS")
        return 0
    finally:
        terminate_process(listener_proc)
        terminate_process(proxy_proc)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if not args.keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)
        else:
            print(f"Kept temp dir: {temp_root}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
