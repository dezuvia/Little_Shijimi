#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from telegram_common import build_telegram_text, log_debug, send_telegram


SESSION_META_CWD_RE = re.compile(r'"cwd":"([^"]*)"')
SESSION_TIMESTAMP_RE = re.compile(r'"timestamp":"([^"]*)"')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--log", default="")
    parser.add_argument("--preview-chars", type=int, default=220)
    parser.add_argument("--attach-timeout", type=int, default=45)
    parser.add_argument("--owner-exit-grace", type=int, default=8)
    parser.add_argument("--delete-after", type=int, default=0)
    parser.add_argument("--delete-queue-path", default="")
    parser.add_argument("--sessions-root", default=os.environ.get("CODEX_NOTIFY_SESSIONS_ROOT", os.path.expanduser("~/.codex/sessions")))
    return parser.parse_args()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def session_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime if path.exists() else 0)


def snapshot_sessions(root: Path) -> dict[Path, int]:
    snapshot: dict[Path, int] = {}
    for path in session_files(root):
        try:
            snapshot[path] = path.stat().st_size
        except OSError:
            continue
    return snapshot


def session_meta_prefix(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(4096)
    except OSError:
        return ""


def session_meta_cwd(path: Path) -> str:
    prefix = session_meta_prefix(path)
    match = SESSION_META_CWD_RE.search(prefix)
    if match:
        return match.group(1)
    try:
        first_line = path.open("r", encoding="utf-8", errors="replace").readline()
        payload = json.loads(first_line)
    except Exception:  # noqa: BLE001
        return ""
    return str((payload.get("payload") or {}).get("cwd") or "")


def session_timestamp(path: Path) -> str:
    prefix = session_meta_prefix(path)
    match = SESSION_TIMESTAMP_RE.search(prefix)
    if match:
        return match.group(1)
    return ""


def pick_candidate(root: Path, cwd: str, baseline: dict[Path, int], floor_epoch: float) -> tuple[Path, int, str] | None:
    best: tuple[Path, int, str] | None = None
    for path in session_files(root):
        try:
            stat = path.stat()
        except OSError:
            continue
        baseline_size = baseline.get(path)
        if baseline_size is not None and stat.st_size <= baseline_size:
            continue
        if stat.st_mtime + 2 < floor_epoch and baseline_size is not None:
            continue
        if session_meta_cwd(path) != cwd:
            continue
        offset = 0 if baseline_size is None else baseline_size
        candidate = (path, offset, "new" if baseline_size is None else "existing")
        if best is None or stat.st_mtime >= best[0].stat().st_mtime:
            best = candidate
    return best


def parse_function_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    raw_args = payload.get("arguments")
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            return json.loads(raw_args)
        except json.JSONDecodeError:
            return {}
    return {}


def extract_message(entry: dict[str, Any]) -> str:
    payload = entry.get("payload") or {}
    if entry.get("type") == "event_msg" and payload.get("type") == "agent_message" and payload.get("phase") == "final_answer":
        return str(payload.get("message") or "")
    if entry.get("type") == "event_msg" and payload.get("type") == "task_complete":
        return str(payload.get("last_agent_message") or "")
    if entry.get("type") == "response_item" and payload.get("type") == "function_call":
        args = parse_function_arguments(payload)
        if payload.get("name") == "exec_command" and args.get("sandbox_permissions") == "require_escalated":
            justification = str(args.get("justification") or "Approval needed to run a command outside the sandbox.").strip()
            cmd = str(args.get("cmd") or "").strip()
            blocks = [f"Approval needed.\n{justification}"]
            if cmd:
                blocks.append(f"Command: {cmd}")
            prefix_rule = args.get("prefix_rule")
            if isinstance(prefix_rule, list) and prefix_rule:
                rule = " ".join(str(part).strip() for part in prefix_rule if str(part).strip())
                if rule:
                    blocks.append(f"Suggested rule: {rule}")
            return "\n".join(blocks)
        if payload.get("name") == "request_user_input":
            questions = args.get("questions") or []
            if not questions:
                return "Input needed."
            blocks: list[str] = []
            for question in questions:
                prompt = str(question.get("question") or "Input needed.").strip()
                options = question.get("options") or []
                labels = [str(option.get("label") or "").strip() for option in options]
                labels = [label for label in labels if label]
                if labels:
                    prompt = f"{prompt}\nOptions: {' | '.join(labels)}"
                blocks.append(prompt)
            return "Input needed:\n\n" + "\n\n".join(blocks)
    return ""


def poll_session(
    path: Path,
    *,
    owner_pid: int,
    cwd: str,
    label: str,
    offset: int,
    log_path: str,
    preview_chars: int,
    delete_after: int,
    delete_queue_path: str,
) -> None:
    log_debug(log_path, f"polling session={path} label={label} cwd={cwd} offset={offset}")
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        log_debug(log_path, f"session open failed error={exc}")
        return
    try:
        handle.seek(offset)
        last_hash = ""
        while True:
            line = handle.readline()
            if line:
                offset = handle.tell()
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = extract_message(entry)
                if not message:
                    continue
                digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
                if digest == last_hash:
                    continue
                last_hash = digest
                preview = " ".join(message.split())[:preview_chars]
                log_debug(log_path, f"matched final message preview={preview}")
                send_telegram(
                    build_telegram_text(cwd, label, message),
                    bot_token=os.environ.get("TG_BOT_TOKEN", ""),
                    chat_id=os.environ.get("TG_CHAT_ID", ""),
                    log_path=log_path,
                    delete_after=delete_after,
                    delete_queue_path=delete_queue_path,
                )
                continue
            if not pid_alive(owner_pid):
                try:
                    current_size = path.stat().st_size
                except OSError:
                    current_size = 0
                if current_size <= offset:
                    log_debug(log_path, "owner exited and session drained")
                    break
            time.sleep(0.5)
    finally:
        handle.close()


def main() -> int:
    args = parse_args()
    root = Path(args.sessions_root)
    baseline = snapshot_sessions(root)
    start = time.time()
    deadline = start + args.attach_timeout
    owner_deadline = 0.0

    log_debug(args.log, f"watch start label={args.label} cwd={args.cwd} pid={args.owner_pid}")

    while True:
        candidate = pick_candidate(root, args.cwd, baseline, start)
        if candidate is not None:
            path, offset, kind = candidate
            log_debug(args.log, f"session attached {path} kind={kind} offset={offset}")
            poll_session(
                path,
                owner_pid=args.owner_pid,
                cwd=args.cwd,
                label=args.label,
                offset=offset,
                log_path=args.log,
                preview_chars=args.preview_chars,
                delete_after=args.delete_after,
                delete_queue_path=args.delete_queue_path,
            )
            return 0

        now = time.time()
        if pid_alive(args.owner_pid):
            owner_deadline = 0.0
        elif owner_deadline == 0.0:
            owner_deadline = now + args.owner_exit_grace
            log_debug(args.log, f"owner exited before session attach; keeping watch for {args.owner_exit_grace}s")
        elif now >= owner_deadline:
            log_debug(args.log, "owner exit grace elapsed before session attach")
            return 0

        if args.attach_timeout > 0 and now >= deadline:
            if pid_alive(args.owner_pid):
                deadline = now + args.attach_timeout
            else:
                log_debug(args.log, "attach timeout")
                return 0

        time.sleep(0.25)


if __name__ == "__main__":
    raise SystemExit(main())
