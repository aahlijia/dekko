#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check for uv
if ! command -v uv &>/dev/null; then
    printf '\033[31merror:\033[0m uv is required but not installed.\n'
    printf 'Install it: curl -LsSf https://astral.sh/uv/install.sh | sh\n'
    exit 1
fi

printf 'Registering marketplace...\n'
if ! claude plugin marketplace add "$DIR" 2>/dev/null; then
    printf '(marketplace already registered, continuing)\n'
fi

printf 'Installing lidar plugin...\n'
claude plugin install lidar@lidar

printf '\n\033[32mDone.\033[0m Restart Claude Code to activate \033[1m/map\033[0m.\n'
