#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check for uv
if ! command -v uv &>/dev/null; then
    printf '\033[31merror:\033[0m uv is required but not installed.\n'
    printf 'Install it: curl -LsSf https://astral.sh/uv/install.sh | sh\n'
    exit 1
fi

printf 'Installing lidar-map (CLI command: lidar)...\n'
uv tool install --force --from "$DIR" lidar-map

printf 'Installing the lidar-map plugin into Claude Code...\n'
lidar --claude-install

printf '\n\033[32mDone.\033[0m Restart Claude Code to activate \033[1m/map\033[0m.\n'
