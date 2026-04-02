#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

MAX_TELEGRAM_TEXT = 3900


def log_debug(log_path: str, message: str) -> None:
    if not log_path:
        return
    log_dir = os.path.dirname(log_path) or "."
    if not os.access(log_dir, os.W_OK):
        return
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{timestamp} {message}\n")
    except OSError:
        return


def format_header(path: str, label: str) -> str:
    home = os.path.expanduser("~")
    if path == home:
        display = "~"
    elif path.startswith(home + os.sep):
        display = f"~/{os.path.basename(path)}"
    else:
        display = path
    return f"{display} - [{label}]"


def build_telegram_text(path: str, label: str, message: str) -> str:
    return f"{format_header(path, label)}\n{message}"[:MAX_TELEGRAM_TEXT]


def delete_telegram(
    message_id: int,
    *,
    bot_token: str,
    chat_id: str,
    log_path: str = "",
) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "message_id": message_id}).encode("utf-8")
    try:
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", "replace")
        payload = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        compact = " ".join(str(exc).split())[:200]
        log_debug(log_path, f"telegram delete failed response={compact}")
        return False
    if payload.get("ok") is True:
        log_debug(log_path, f"telegram deleted message_id={message_id}")
        return True
    compact = " ".join(body.split())[:200]
    log_debug(log_path, f"telegram delete failed response={compact}")
    return False


def schedule_delete(
    *,
    delete_queue_path: str,
    message_id: int,
    delete_after: int,
    log_path: str = "",
) -> None:
    if delete_after <= 0 or not delete_queue_path:
        return
    record = {"message_id": message_id, "delete_at": int(time.time()) + delete_after}
    target = Path(delete_queue_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        compact = " ".join(str(exc).split())[:200]
        log_debug(log_path, f"telegram delete queue write failed response={compact}")


def sweep_delete_queue(
    *,
    delete_queue_path: str,
    delete_after: int,
    bot_token: str,
    chat_id: str,
    log_path: str = "",
) -> None:
    if delete_after <= 0 or not delete_queue_path or not bot_token or not chat_id:
        return
    target = Path(delete_queue_path)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        compact = " ".join(str(exc).split())[:200]
        log_debug(log_path, f"telegram delete queue read failed response={compact}")
        return

    now = int(time.time())
    kept: list[str] = []
    changed = False
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            changed = True
            continue
        message_id = record.get("message_id")
        delete_at = int(record.get("delete_at") or 0)
        if not isinstance(message_id, int):
            changed = True
            continue
        if delete_at > now:
            kept.append(json.dumps(record, ensure_ascii=False))
            continue
        changed = True
        deleted = delete_telegram(message_id, bot_token=bot_token, chat_id=chat_id, log_path=log_path)
        if not deleted:
            kept.append(json.dumps(record, ensure_ascii=False))

    if not changed:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    except OSError as exc:
        compact = " ".join(str(exc).split())[:200]
        log_debug(log_path, f"telegram delete queue rewrite failed response={compact}")


def send_telegram(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    log_path: str = "",
    delete_after: int = 0,
    delete_queue_path: str = "",
) -> bool:
    if not bot_token or not chat_id:
        log_debug(log_path, "telegram skipped missing credentials")
        return False
    sweep_delete_queue(
        delete_queue_path=delete_queue_path,
        delete_after=delete_after,
        bot_token=bot_token,
        chat_id=chat_id,
        log_path=log_path,
    )
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    try:
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", "replace")
        payload = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        compact = " ".join(str(exc).split())[:200]
        log_debug(log_path, f"telegram send failed response={compact}")
        return False
    if payload.get("ok") is not True:
        compact = " ".join(body.split())[:200]
        log_debug(log_path, f"telegram send failed response={compact}")
        return False
    log_debug(log_path, "telegram sent")
    message_id = payload.get("result", {}).get("message_id")
    if isinstance(message_id, int):
        schedule_delete(
            delete_queue_path=delete_queue_path,
            message_id=message_id,
            delete_after=delete_after,
            log_path=log_path,
        )
    return True

