#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from telegram_common import build_telegram_text, log_debug, send_telegram


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--message-file", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--log", default="")
    parser.add_argument("--preview-chars", type=int, default=220)
    parser.add_argument("--delete-after", type=int, default=0)
    parser.add_argument("--delete-queue-path", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tg_bot_token = os.environ.get("TG_BOT_TOKEN", "")
    tg_chat_id = os.environ.get("TG_CHAT_ID", "")
    try:
        message = Path(args.message_file).read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        log_debug(args.log, f"last message read failed error={exc}")
        return 0
    if not message:
        log_debug(args.log, "last message file empty")
        return 0
    preview = " ".join(message.split())[: args.preview_chars]
    log_debug(args.log, f"matched final message preview={preview}")
    send_telegram(
        build_telegram_text(args.cwd, args.label, message),
        bot_token=tg_bot_token,
        chat_id=tg_chat_id,
        log_path=args.log,
        delete_after=args.delete_after,
        delete_queue_path=args.delete_queue_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

