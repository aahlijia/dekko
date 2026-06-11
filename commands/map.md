---
description: Generate MAP.md — a relational map of every file, function, signature, and call in the repo
argument-hint: "[subpath]"
allowed-tools: Bash(uv run:*)
---

## Code map results

The mapping tool has already run; its summary is below.

!`uv run --quiet "${CLAUDE_PLUGIN_ROOT}/tool/lidar.py" $ARGUMENTS`

## Your task

The repository map was generated programmatically by the tool above — do
NOT parse any source files yourself.

1. If the summary above shows an error (e.g. `uv` missing or the tool
   failed), explain the problem to the user and how to fix it. Otherwise:
2. Relay the summary to the user: how many files were mapped, in which
   languages, how many functions and call relationships were found, and
   anything skipped.
3. Tell the user the map was written to `MAP.md` (human-readable) and
   `map.json` (machine-readable) at the repo root.
4. Do not read MAP.md back into context unless the user asks a question
   that requires it.
