#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_BIN_DIR="${HOME}/.local/bin"
TARGET_WRAPPER="${TARGET_BIN_DIR}/codex"
SOURCE_WRAPPER="${ROOT}/wrappers/codex"
ENV_TARGET="${HOME}/.config/codex-telegram.env"
ENV_EXAMPLE="${ROOT}/env/codex-telegram.env.example"
TIMESTAMP="$(date '+%Y%m%d%H%M%S')"

mkdir -p "$TARGET_BIN_DIR" "${HOME}/.config"

if [[ -e "$TARGET_WRAPPER" || -L "$TARGET_WRAPPER" ]]; then
  if [[ "$(readlink "$TARGET_WRAPPER" 2>/dev/null || true)" != "$SOURCE_WRAPPER" ]]; then
    mv "$TARGET_WRAPPER" "${TARGET_WRAPPER}.backup.${TIMESTAMP}"
    printf 'Backed up existing wrapper to %s.backup.%s\n' "$TARGET_WRAPPER" "$TIMESTAMP"
  fi
fi

ln -sfn "$SOURCE_WRAPPER" "$TARGET_WRAPPER"
chmod +x "$SOURCE_WRAPPER" "$ROOT"/scripts/*.py "$ROOT/install.sh"

if [[ ! -f "$ENV_TARGET" ]]; then
  cp "$ENV_EXAMPLE" "$ENV_TARGET"
  printf 'Created %s from example.\n' "$ENV_TARGET"
fi

cat <<EOF
Little_Shijimi installed.

Next steps:
1. Edit $ENV_TARGET and set TG_BOT_TOKEN / TG_CHAT_ID.
2. In your shell rc, source the env file:
   if [[ -f "\$HOME/.config/codex-telegram.env" ]]; then
     source "\$HOME/.config/codex-telegram.env"
   fi
3. Optionally pin the wrapper with:
   codex() { command ~/.local/bin/codex "\$@"; }
EOF

