---
description: Generate MAP.md — a relational map of every file, function, signature, and call in the repo
argument-hint: "[subpath]"
allowed-tools: Bash(dekko:*)
---

## Code map results

The mapping tool has already run; its summary is below.

!`dekko map --if-stale . $ARGUMENTS`

## Your task

The repository map was generated programmatically by the tool above — do
NOT parse any source files yourself.

1. If the summary above shows an error, explain the problem to the user
   and how to fix it. In particular, if the `dekko` command was not
   found, tell them to install it with `pip install dekko` (or
   `uv tool install dekko`). Otherwise:
2. Relay the summary to the user: how many files were mapped, in which
   languages, how many functions and call relationships were found, and
   anything skipped. If the summary says "map fresh", tell the user the
   existing MAP.md is already up to date and nothing was regenerated.
3. Tell the user the map was written to `MAP.md` (human-readable) and
   `map.json` (machine-readable) at the repo root.
4. Do not read MAP.md back into context unless the user asks a question
   that requires it.
