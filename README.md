# Little_Shijimi

Little_Shijimi is a local Telegram notifier wrapper for Codex.

It wraps the real `codex` binary, watches interactive session files, detects blocking prompts, and sends short Telegram messages when:

- Codex reaches a final answer in a session
- Codex asks for approval to run an escalated command
- Codex is waiting for user input at an execution gate
- `codex exec` or other non-TTY runs finish with a captured last message

## Included Files

- `wrappers/codex`: the shell wrapper you place in front of the real Codex binary
- `scripts/agent_telegram_tty_proxy.py`: PTY proxy for interactive prompt detection
- `scripts/codex_session_watch.py`: background watcher for Codex session JSONL files
- `scripts/send_last_message.py`: sender for non-interactive captured final messages
- `scripts/telegram_common.py`: shared Telegram send/delete helpers
- `env/codex-telegram.env.example`: example environment file
- `install.sh`: local install helper

## How It Works

Interactive mode:

- The wrapper starts a background session watcher.
- The real Codex process is run through the PTY prompt proxy.
- The proxy looks for approval prompts and execution gates in terminal output.
- The watcher tails matching `~/.codex/sessions/**/*.jsonl` files and sends final answers or approval-style events.

Non-interactive mode:

- If the command contains `exec` and does not already provide `--output-last-message`, the wrapper injects that flag.
- The captured last message is sent to Telegram after the command exits.

## Environment Variables

Required:

- `TG_BOT_TOKEN`
- `TG_CHAT_ID`

Common optional variables:

- `CODEX_NOTIFY_REAL_CODEX`: real Codex binary path, default `/opt/homebrew/bin/codex`
- `CODEX_NOTIFY_DELETE_AFTER`: auto-delete sent Telegram messages after N seconds
- `CODEX_NOTIFY_PREVIEW_CHARS`: preview length for debug logs
- `CODEX_NOTIFY_ATTACH_TIMEOUT`: session attach timeout for the watcher
- `CODEX_NOTIFY_LOG`: log path
- `CODEX_NOTIFY_STATE_DIR`: state and delete queue directory
- `CODEX_NOTIFY_LABEL`: label shown in Telegram headers
- `CODEX_NOTIFY_DISABLE=1`: bypass the wrapper and run Codex directly

## Install

```bash
cd Little_Shijimi
./install.sh
```

Then edit `~/.config/codex-telegram.env`.

In your shell rc:

```bash
if [[ -f "$HOME/.config/codex-telegram.env" ]]; then
  source "$HOME/.config/codex-telegram.env"
fi

codex() {
  command ~/.local/bin/codex "$@"
}
```

## Notes

- The project uses only Python standard library modules.
- Telegram credentials are loaded from environment variables. Do not commit real tokens.
- The current implementation focuses on Codex. The prompt proxy regex is also compatible with Claude-style approval text, but this repository ships only the Codex wrapper.

## License

MIT

