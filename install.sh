#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check for uv
if ! command -v uv &>/dev/null; then
    printf '\033[31merror:\033[0m uv is required but not installed.\n'
    printf 'Install it: curl -LsSf https://astral.sh/uv/install.sh | sh\n'
    exit 1
fi

printf 'Installing dekko (CLI command: dekko)...\n'
# --refresh-package forces a rebuild from the current source tree: uv
# caches the built wheel by version, so without it a re-install at the
# same version would reuse a stale wheel and ignore local changes.
uv tool install --force --reinstall --refresh-package dekko \
    --from "$DIR" dekko

# Invoke the freshly installed tool by its absolute path. Inside a repo
# checkout, a local .venv/bin/dekko (an editable install with no bundled
# _plugin directory) can shadow it on PATH and break --claude-install.
DEKKO_BIN="$(uv tool dir)/dekko/bin/dekko"

printf 'Installing the dekko plugin into Claude Code...\n'
"$DEKKO_BIN" --claude-install

printf '\n\033[32mDone.\033[0m Restart Claude Code to activate \033[1m/map\033[0m.\n'
