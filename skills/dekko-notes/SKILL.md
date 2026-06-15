---
name: dekko-notes
description: Keep dekko's symbol-anchored notes current. Use whenever you read a symbol's context, rename or move a symbol, or make a non-obvious behavioral change in a repo that has a .dekko/ map. Notes are durable, committed code annotations keyed by symbol id.
---

# Keeping dekko notes current

dekko stores **symbol-anchored notes** in `.dekko/notes.json`, keyed by
symbol id (`path::Qualified.name`). They are committed to git and shown
inline by `dekko query symbol` and `dekko context`, so they are durable
memory that travels with the code. This skill is about keeping them
accurate as you work.

## Consult notes before editing

When you pull a symbol's context (`dekko query symbol <sym>`,
`dekko context <sym>`, or the `query_symbol` / `get_context_pack` MCP
tools), read any `note:` lines first — they record rationale, gotchas,
and constraints that the code alone does not show.

## Write a note after a non-obvious change

After you make a change whose reasoning is not evident from the diff —
a workaround, an invariant that must hold, a deliberate trade-off — add
a note so the next reader (human or agent) sees it:

```
dekko note add path/to/file.py:func "why this is the way it is"
```

or the `add_note` MCP tool. Keep notes short and about *why*, not what.

## Re-anchor notes when a symbol moves

Note ids embed the file path and qualified name, so renaming or moving
a symbol **orphans** its notes. After such a change:

1. Find orphans: `dekko note list --orphaned`.
2. For each orphan that still applies, re-anchor it to the new id:
   ```
   dekko note add <new-target> "<the note text>"
   dekko note rm  <old-target>
   ```
   (The old id is shown in the orphaned listing.)
3. Remove notes that no longer apply: `dekko note rm <old-target>`.

Run the orphan sweep after any rename, file move, or signature change
that alters a symbol's qualified name.

## Boundaries

- Notes are for human/agent rationale, not generated data — never put
  machine state or large output in them.
- Do not edit `.dekko/notes.json` by hand; use the `note` commands or
  the `add_note` / `list_notes` tools so the file stays valid and
  git-tracked.
