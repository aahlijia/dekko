---
name: dekko-notes
description: Read and write dekko's symbol-anchored notes in any repo with a .dekko/ directory. Trigger every time you pull a symbol's context (query_symbol, get_context_pack, dekko query/context), right after a non-obvious change, and right after any rename/move/signature edit (to sweep orphaned notes). Notes are durable, committed annotations keyed by symbol id — they carry rationale grep/Read cannot show.
---

# Keeping dekko notes current

dekko stores **symbol-anchored notes** in `.dekko/notes.json`, keyed by
symbol id (`path::Qualified.name`). They are committed to git and shown
inline by `query_symbol` / `get_context_pack` (CLI: `dekko query
symbol` / `dekko context`). See the `dekko-orient` skill for target
syntax (bare name, `Class.method`, `file.py:name`, or the tolerated
`::` form) and for when to reach for dekko over grep/Read generally.

## Consult notes before editing

Whenever you pull a symbol's context, read any `note:` lines first —
they record rationale, gotchas, and constraints the code alone does
not show. Call the `list_notes` MCP tool (or `dekko note list <sym>`)
directly if you need to see a symbol's notes outside a context pack.

## Write a note after a non-obvious change

After a change whose reasoning is not evident from the diff — a
workaround, an invariant that must hold, a deliberate trade-off — add
a note so the next reader (human or agent) sees it. Call the
`add_note` MCP tool directly, or:

```
dekko note add path/to/file.py:func "why this is the way it is"
```

Keep notes short and about *why*, not what.

## Re-anchor notes when a symbol moves

Note ids embed the file path and qualified name, so renaming or moving
a symbol **orphans** its notes. After such a change:

1. Find orphans: `dekko note list --orphaned`.
2. For each orphan that still applies, re-anchor it to the new id:
   ```
   dekko note add <new-target> "<the note text>"
   dekko note rm  <old-target>
   ```
   (the old id is shown in the orphaned listing).
3. Remove notes that no longer apply: `dekko note rm <old-target>`.

Run this sweep after any rename, file move, or signature change that
alters a symbol's qualified name.

## Boundaries

- Notes are for human/agent rationale, not generated data — never put
  machine state or large output in them.
- Do not edit `.dekko/notes.json` by hand; use the `note` commands or
  the `add_note` / `list_notes` tools so the file stays valid and
  git-tracked.
