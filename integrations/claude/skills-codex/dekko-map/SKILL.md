---
name: dekko-map
description: Generate or refresh dekko's code map (MAP.md and map.json, a relational map of every file, function, signature, and call in the repo) and summarize it. Trigger when the user asks to map, index, or get an overview of the repo, asks what a codebase contains or how it is laid out, or invokes this skill directly, optionally with a subpath or `--exclude GLOB`.
---

# Map the repository

Run these yourself, in order, from the repo root. If the user gave a
subpath or other arguments, append them to the first command:

```sh
dekko map --if-stale . [ARGS]
dekko summary --budget 2000
```

The map is generated programmatically by those commands. Do NOT parse
any source files yourself.

1. If a command printed an error, explain the problem and how to fix
   it. If `dekko` was not found, tell the user to install it with
   `pip install dekko` (or `uv tool install dekko`); for any other
   install or environment error, point them at the `dekko-doctor`
   skill. Otherwise:
2. Relay the digest: the file/symbol/edge counts and language mix, the
   largest directories and what they do, the load-bearing and
   orchestrating symbols, and any parse errors.
3. Tell the user the full map is at `.dekko/MAP.md` (human-readable)
   and `.dekko/map.json` (machine-readable), and that they can query it
   without re-reading source via `dekko search|query|context|affected`
   or the dekko MCP tools (`search_code`, `query_symbol`,
   `get_context_pack`, `outline`, `workset`, `impacted_tests`, ...).
   See the `dekko-orient` skill, and use those tools yourself for every
   follow-up question in this session instead of grep or whole-file
   reads.
4. Do not read `.dekko/MAP.md` back into context (it can be large).
   Prefer the query commands for follow-up questions.
5. If the arguments included `--exclude GLOB`, mention that the
   pattern(s) were also saved to `.dekko/.dekkoignore` (gitignore
   syntax), so future bare `dekko map` runs keep honoring them.
