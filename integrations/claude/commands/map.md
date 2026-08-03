---
description: Generate MAP.md — a relational map of every file, function, signature, and call in the repo
argument-hint: "[subpath]"
allowed-tools: Bash(dekko:*)
---

## Code map results

The map has been (re)generated and a compact digest printed below.

!`dekko map --if-stale . $ARGUMENTS`

!`dekko summary --budget 2000`

## Your task

The repository map was generated programmatically by the tool above — do
NOT parse any source files yourself.

1. If the output above shows an error, explain the problem to the user
   and how to fix it. In particular, if the `dekko` command was not
   found, tell them to install it with `pip install dekko` (or
   `uv tool install dekko`). Otherwise:
2. Relay the digest to the user: the file/symbol/edge counts and
   language mix, the largest directories and what they do, the
   load-bearing and orchestrating symbols, and any parse errors.
3. Tell the user the full map is at `.dekko/MAP.md` (human-readable)
   and `.dekko/map.json` (machine-readable), and that they can query it
   without re-reading source via `dekko query|context|affected` or the
   dekko MCP tools (`query_symbol`, `get_context_pack`, `outline`,
   `workset`, `impacted_tests`, ...) — see the `dekko-orient` skill.
4. Do not read `.dekko/MAP.md` back into context (it can be large) —
   prefer the query commands for follow-up questions.
5. If `$ARGUMENTS` included `--exclude GLOB`, mention that the
   pattern(s) were also persisted to `.dekko/.dekkoignore` (gitignore
   syntax) so future bare `dekko map` runs keep honoring them.
