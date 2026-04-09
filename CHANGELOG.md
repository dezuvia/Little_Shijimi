# Changelog

All notable changes to Little_Shijimi are documented in this file.

The format is based on Keep a Changelog and the project uses semantic versioning tags for releases.

## [1.0.0] - 2026-04-09

### Added

- Telegram reply routing for approval prompts so `go` and `no` can be sent from Telegram back into the matching local terminal.
- `scripts/telegram_reply_listener.py` to poll Telegram replies and dispatch approval input to pending terminals.
- `scripts/telegram_approval_replay_tester.py` to replay one historical approval prompt and verify the routing path offline.
- Mockable Telegram API request helpers for replay and local testing.
- Reply-listener and approval-menu configuration knobs in the wrapper environment.

### Changed

- `wrappers/codex` now starts the reply listener automatically during interactive sessions.
- Approval handling now records terminal-specific pending state and resolves menu navigation from rendered approval options.
- Installation remains symlink-based so the local wrapper can stay aligned with the repo copy.

### Documentation

- Updated English and Traditional Chinese READMEs for the new approval-reply workflow and offline test flow.
- Added release notes under `releases/v1.0.0.md`.

## [0.1.0] - 2026-04-03

### Added

- Initial public wrapper for Codex Telegram notifications.
- Interactive session watcher and non-interactive final-message capture.

