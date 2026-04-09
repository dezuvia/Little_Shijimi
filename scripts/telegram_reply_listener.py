#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

from telegram_common import get_telegram_updates, log_debug, send_telegram


APPROVE_WORDS = {"go", "approve", "yes", "allow"}
DECLINE_WORDS = {"no", "deny", "decline"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--log", default="")
    parser.add_argument("--idle-exit", type=int, default=300)
    parser.add_argument("--poll-timeout", type=int, default=20)
    return parser.parse_args()


def pending_dir(state_dir: Path) -> Path:
    return state_dir / "pending"


def offset_path(state_dir: Path) -> Path:
    return state_dir / "telegram_reply_listener.offset"


def lock_path(state_dir: Path) -> Path:
    return state_dir / "telegram_reply_listener.lock"


def load_offset(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def store_offset(path: Path, offset: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{offset}\n", encoding="utf-8")
    except OSError:
        return


def iter_pending_records(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    items: list[tuple[Path, dict[str, Any]]] = []
    try:
        files = sorted(root.glob("*.json"))
    except OSError:
        return items
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            items.append((path, payload))
    return items


def find_record_by_message_id(root: Path, message_id: int) -> tuple[Path, dict[str, Any]] | None:
    for path, record in iter_pending_records(root):
        if record.get("status") != "pending":
            continue
        if record.get("telegram_message_id") == message_id:
            return path, record
    return None


def find_record_by_token(root: Path, token: str) -> tuple[Path, dict[str, Any]] | None:
    token = token.strip().lower()
    if not token:
        return None
    for path, record in iter_pending_records(root):
        if record.get("status") != "pending":
            continue
        if str(record.get("token") or "").strip().lower() == token:
            return path, record
    return None


def parse_command(text: str) -> tuple[str, str | None] | None:
    words = [word for word in text.strip().lower().split() if word]
    if not words:
        return None
    action = words[0]
    if action in APPROVE_WORDS:
        return "approve", words[1] if len(words) > 1 else None
    if action in DECLINE_WORDS:
        return "decline", words[1] if len(words) > 1 else None
    return None


def record_expired(record: dict[str, Any], now: float) -> bool:
    expires_at = float(record.get("expires_at") or 0.0)
    return expires_at > 0.0 and expires_at <= now


def store_record(path: Path, record: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return


def append_control(record: dict[str, Any], action: str) -> bool:
    control_path = str(record.get("control_path") or "").strip()
    if not control_path:
        return False
    input_text = ""
    if action == "approve":
        input_text = str(record.get("approve_input") or "")
    elif action == "decline":
        input_text = str(record.get("decline_input") or "")
    if not input_text:
        return False
    payload = {
        "action": action,
        "token": record.get("token"),
        "input_text": input_text,
        "source": "telegram",
        "created_at": time.time(),
    }
    try:
        path = Path(control_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def ack(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    log_path: str,
    reply_to_message_id: int | None,
) -> None:
    send_telegram(
        text,
        bot_token=bot_token,
        chat_id=chat_id,
        log_path=log_path,
        delete_after=0,
        delete_queue_path="",
        reply_to_message_id=reply_to_message_id,
    )


def handle_update(
    update: dict[str, Any],
    *,
    pending_root: Path,
    bot_token: str,
    chat_id: str,
    log_path: str,
) -> None:
    message = update.get("message")
    if not isinstance(message, dict):
        return
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return
    if str(chat.get("id")) != chat_id:
        return
    text = str(message.get("text") or "").strip()
    parsed = parse_command(text)
    if parsed is None:
        return
    action, token = parsed
    command_message_id = message.get("message_id")
    reply_to = message.get("reply_to_message") or {}
    reply_message_id = reply_to.get("message_id")
    now = time.time()

    match = None
    if isinstance(reply_message_id, int):
        match = find_record_by_message_id(pending_root, reply_message_id)
    if match is None and token:
        match = find_record_by_token(pending_root, token)

    if match is None:
        ack(
            "No pending approval matched that reply.",
            bot_token=bot_token,
            chat_id=chat_id,
            log_path=log_path,
            reply_to_message_id=command_message_id if isinstance(command_message_id, int) else None,
        )
        return

    path, record = match
    if record_expired(record, now):
        record["status"] = "expired"
        record["resolved_at"] = now
        store_record(path, record)
        ack(
            "That approval prompt already expired.",
            bot_token=bot_token,
            chat_id=chat_id,
            log_path=log_path,
            reply_to_message_id=command_message_id if isinstance(command_message_id, int) else None,
        )
        return

    if action == "decline" and not str(record.get("decline_input") or ""):
        ack(
            "Decline is not configured for that prompt.",
            bot_token=bot_token,
            chat_id=chat_id,
            log_path=log_path,
            reply_to_message_id=command_message_id if isinstance(command_message_id, int) else None,
        )
        return

    if not append_control(record, action):
        record["status"] = "dispatch_failed"
        record["resolved_at"] = now
        store_record(path, record)
        ack(
            "Failed to reach the terminal for that prompt.",
            bot_token=bot_token,
            chat_id=chat_id,
            log_path=log_path,
            reply_to_message_id=command_message_id if isinstance(command_message_id, int) else None,
        )
        return

    record["status"] = f"{action}_dispatched"
    record["resolved_at"] = now
    store_record(path, record)
    label = str(record.get("label") or "").strip()
    ack(
        f"Sent {action} to {label or 'the terminal'}.",
        bot_token=bot_token,
        chat_id=chat_id,
        log_path=log_path,
        reply_to_message_id=command_message_id if isinstance(command_message_id, int) else None,
    )


def main() -> int:
    args = parse_args()
    bot_token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not bot_token or not chat_id:
        return 0

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    pending_root = pending_dir(state_dir)
    pending_root.mkdir(parents=True, exist_ok=True)

    lock_handle = lock_path(state_dir).open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log_debug(args.log, "reply listener already running")
        lock_handle.close()
        return 0

    log_debug(args.log, f"reply listener start state_dir={state_dir}")
    offset = load_offset(offset_path(state_dir))
    idle_deadline = time.time() + args.idle_exit

    try:
        while True:
            updates = get_telegram_updates(
                bot_token=bot_token,
                offset=offset,
                timeout=args.poll_timeout,
                log_path=args.log,
            )
            if updates is None:
                time.sleep(2)
                continue
            if updates:
                idle_deadline = time.time() + args.idle_exit
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                handle_update(
                    update,
                    pending_root=pending_root,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    log_path=args.log,
                )
            if offset is not None:
                store_offset(offset_path(state_dir), offset)
            if any(record.get("status") == "pending" for _, record in iter_pending_records(pending_root)):
                idle_deadline = time.time() + args.idle_exit
            elif time.time() >= idle_deadline:
                log_debug(args.log, "reply listener idle exit")
                break
    finally:
        lock_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
