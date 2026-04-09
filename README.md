# Little_Shijimi

Little_Shijimi is a local Telegram notifier wrapper for Codex.

Current release: `v1.0.0` on `2026-04-09`

This release turns the project from a one-way notifier into a remote approval bridge: Telegram replies can now drive Codex approval prompts back into the correct terminal, and the repo ships an offline replay tester for validating that flow.

## Release Highlights

- Route Telegram `go` and `no` replies back into the matching local terminal for Codex approval prompts.
- Start a background reply listener automatically during interactive wrapper sessions.
- Support approval-menu navigation by deriving the required `Enter` or arrow-key sequence from the rendered options.
- Add a replay tester that validates approval-reply routing against historical session prompts without touching a live work session.
- Keep local installs synced to the repo copy by installing `~/.local/bin/codex` as a symlink to `wrappers/codex`.

See `CHANGELOG.md` for the update log and `releases/v1.0.0.md` for the release notes body.

It wraps the real `codex` binary, watches interactive session files, detects blocking prompts, and sends short Telegram messages when:

- Codex reaches a final answer in a session
- Codex asks for approval to run an escalated command
- Telegram replies `go` or `no` to an approval prompt and the wrapper routes that input back into the matching terminal
- Codex is waiting for user input at an execution gate
- `codex exec` or other non-TTY runs finish with a captured last message

## Included Files

- `wrappers/codex`: the shell wrapper you place in front of the real Codex binary
- `scripts/agent_telegram_tty_proxy.py`: PTY proxy for interactive prompt detection
- `scripts/codex_session_watch.py`: background watcher for Codex session JSONL files
- `scripts/send_last_message.py`: sender for non-interactive captured final messages
- `scripts/telegram_common.py`: shared Telegram send/delete helpers
- `scripts/telegram_reply_listener.py`: polls Telegram replies and appends go/no actions to the matching terminal control file
- `scripts/telegram_approval_replay_tester.py`: offline replay tester that reuses one historical approval prompt and validates reply routing without touching a live work session
- `env/codex-telegram.env.example`: example environment file
- `install.sh`: local install helper

## How It Works

Interactive mode:

- The wrapper starts a background session watcher.
- The wrapper also starts a background Telegram reply listener.
- The real Codex process is run through the PTY prompt proxy.
- The proxy looks for approval prompts and execution gates in terminal output.
- Approval prompts are registered under the current terminal id, and the Telegram message tells you to reply with `go` or `no`.
- The reply listener matches the Telegram reply to the pending prompt and writes the mapped key sequence back to the correct terminal control file.
- For approval menus, the proxy derives `Enter` or `arrow + Enter` from the rendered option order and assumes the first rendered option has focus by default. Override with `AGENT_NOTIFY_APPROVAL_START_INDEX` if your local UI starts elsewhere.
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
- `CODEX_NOTIFY_REPLY_LISTENER`: override path for the Telegram reply listener script
- `CODEX_NOTIFY_REPLY_IDLE_EXIT`: idle timeout in seconds before the reply listener exits
- `CODEX_NOTIFY_REPLY_POLL_TIMEOUT`: Telegram long-poll timeout in seconds
- `CODEX_NOTIFY_LOG`: log path
- `CODEX_NOTIFY_STATE_DIR`: state and delete queue directory
- `CODEX_NOTIFY_LABEL`: label shown in Telegram headers
- `CODEX_NOTIFY_DISABLE=1`: bypass the wrapper and run Codex directly
- `AGENT_NOTIFY_APPROVAL_START_INDEX`: override the initial focused option for approval menus if your UI does not start on the first rendered option

## Install

```bash
cd Little_Shijimi
./install.sh
```

Then edit `~/.config/codex-telegram.env`.

The installer places `~/.local/bin/codex` as a symlink to this repo's `wrappers/codex`, so later changes in the repo copy are reflected locally without reinstalling unless you move the repo.

In your shell rc:

```bash
if [[ -f "$HOME/.config/codex-telegram.env" ]]; then
  source "$HOME/.config/codex-telegram.env"
fi

codex() {
  command ~/.local/bin/codex "$@"
}
```

## Offline Replay Test

Run the offline approval-reply replay tester from the repo copy:

```bash
cd Little_Shijimi
python3 scripts/telegram_approval_replay_tester.py
```

It finds one historical approval prompt in `~/.codex/sessions`, runs the PTY proxy against a fake child process, injects a mocked Telegram `go` reply, and verifies that the input lands on the correct terminal id without using a live work session.

## Update Log

- `2026-04-09`: `v1.0.0` adds Telegram approval reply routing, background reply listening, mockable Telegram API helpers, and the offline approval replay tester.

## Notes

- The project uses only Python standard library modules.
- Telegram credentials are loaded from environment variables. Do not commit real tokens.
- The current implementation focuses on Codex. The prompt proxy regex is also compatible with Claude-style approval text, but this repository ships only the Codex wrapper.

## License

MIT
