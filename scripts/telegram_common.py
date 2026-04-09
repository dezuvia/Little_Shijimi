#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

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


def _mock_dir() -> Path | None:
    raw = os.environ.get("TG_API_MOCK_DIR", "").strip()
    if not raw:
        return None
    return Path(raw)


def _next_mock_message_id(root: Path) -> int:
    counter_path = root / "sendMessage.next_id"
    try:
        current = int(counter_path.read_text(encoding="utf-8").strip() or "1000")
    except (OSError, ValueError):
        current = 1000
    try:
        root.mkdir(parents=True, exist_ok=True)
        counter_path.write_text(f"{current + 1}\n", encoding="utf-8")
    except OSError:
        return current
    return current


def _load_mock_payload(root: Path, method: str) -> dict[str, Any] | None:
    queue_path = root / f"{method}.responses.jsonl"
    if queue_path.exists():
        try:
            lines = queue_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        kept = [line for line in lines if line.strip()]
        if not kept:
            return None
        try:
            payload = json.loads(kept[0])
        except json.JSONDecodeError:
            payload = None
        try:
            queue_path.write_text("".join(line + "\n" for line in kept[1:]), encoding="utf-8")
        except OSError:
            pass
        return payload if isinstance(payload, dict) else None

    response_path = root / f"{method}.response.json"
    if not response_path.exists():
        return None
    try:
        payload = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if method == "getUpdates":
        try:
            response_path.write_text(json.dumps({"ok": True, "result": []}) + "\n", encoding="utf-8")
        except OSError:
            pass
    return payload if isinstance(payload, dict) else None


def _record_mock_request(root: Path, method: str, params: dict[str, Any], *, log_path: str = "") -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / f"{method}.requests.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"params": params, "created_at": time.time()}, ensure_ascii=False) + "\n")
    except OSError as exc:
        compact = " ".join(str(exc).split())[:200]
        log_debug(log_path, f"telegram {method} mock request write failed response={compact}")


def _mock_telegram_api_request(
    method: str,
    params: dict[str, Any],
    *,
    mock_dir: Path,
    log_path: str = "",
) -> dict[str, Any] | None:
    _record_mock_request(mock_dir, method, params, log_path=log_path)
    payload = _load_mock_payload(mock_dir, method)
    if payload is None:
        if method == "sendMessage":
            payload = {"ok": True, "result": {"message_id": _next_mock_message_id(mock_dir)}}
        elif method == "getUpdates":
            payload = {"ok": True, "result": []}
        elif method == "deleteMessage":
            payload = {"ok": True, "result": True}
        else:
            payload = {"ok": True, "result": {}}
    log_debug(log_path, f"telegram {method} mock")
    return payload if payload.get("ok") is True else None


def telegram_api_request(
    method: str,
    params: dict[str, Any],
    *,
    bot_token: str,
    log_path: str = "",
) -> dict[str, Any] | None:
    mock_dir = _mock_dir()
    if mock_dir is not None:
        return _mock_telegram_api_request(method, params, mock_dir=mock_dir, log_path=log_path)
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    data = urllib.parse.urlencode(params).encode("utf-8")
    try:
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", "replace")
        payload = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        compact = " ".join(str(exc).split())[:200]
        log_debug(log_path, f"telegram {method} failed response={compact}")
        return None
    if payload.get("ok") is True:
        return payload
    compact = " ".join(json.dumps(payload, ensure_ascii=False).split())[:200]
    log_debug(log_path, f"telegram {method} failed response={compact}")
    return None


def delete_telegram(
    message_id: int,
    *,
    bot_token: str,
    chat_id: str,
    log_path: str = "",
) -> bool:
    payload = telegram_api_request(
        "deleteMessage",
        {"chat_id": chat_id, "message_id": message_id},
        bot_token=bot_token,
        log_path=log_path,
    )
    if payload is not None:
        log_debug(log_path, f"telegram deleted message_id={message_id}")
        return True
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


def send_telegram_message(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    log_path: str = "",
    delete_after: int = 0,
    delete_queue_path: str = "",
    reply_to_message_id: int | None = None,
) -> dict[str, Any] | None:
    if not bot_token or not chat_id:
        log_debug(log_path, "telegram skipped missing credentials")
        return None
    sweep_delete_queue(
        delete_queue_path=delete_queue_path,
        delete_after=delete_after,
        bot_token=bot_token,
        chat_id=chat_id,
        log_path=log_path,
    )
    params: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if isinstance(reply_to_message_id, int) and reply_to_message_id > 0:
        params["reply_to_message_id"] = reply_to_message_id
    payload = telegram_api_request("sendMessage", params, bot_token=bot_token, log_path=log_path)
    if payload is None:
        return None
    log_debug(log_path, "telegram sent")
    message_id = payload.get("result", {}).get("message_id")
    if isinstance(message_id, int):
        schedule_delete(
            delete_queue_path=delete_queue_path,
            message_id=message_id,
            delete_after=delete_after,
            log_path=log_path,
        )
    return payload


def send_telegram(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    log_path: str = "",
    delete_after: int = 0,
    delete_queue_path: str = "",
    reply_to_message_id: int | None = None,
) -> bool:
    return (
        send_telegram_message(
            text,
            bot_token=bot_token,
            chat_id=chat_id,
            log_path=log_path,
            delete_after=delete_after,
            delete_queue_path=delete_queue_path,
            reply_to_message_id=reply_to_message_id,
        )
        is not None
    )


def get_telegram_updates(
    *,
    bot_token: str,
    offset: int | None,
    timeout: int,
    log_path: str = "",
) -> list[dict[str, Any]] | None:
    params: dict[str, Any] = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    payload = telegram_api_request("getUpdates", params, bot_token=bot_token, log_path=log_path)
    if payload is None:
        return None
    result = payload.get("result")
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]
