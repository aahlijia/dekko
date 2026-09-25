---
name: dekko-orient
description: Use dekko's tools instead of grep/Read whenever a repo has a .dekko/ directory — not just at session start. Trigger before any Grep/Glob/Read of a source file, before a `grep`/`rg`/`find`/`cat` in Bash, and on any "find/locate/understand this symbol," "what does this file/dir contain," "who calls this," "what will this change break," or "which tests should I run" impulse. dekko has already parsed and indexed the repo; grepping or reading whole files re-does work dekko already did, at higher token cost.
---

# Orienting with dekko (read less of the repo)

A repo with a `.dekko/` directory is already parsed and indexed. Call
dekko **before** grepping for a symbol or reading a whole file.

The MCP tools are `mcp__dekko__*` or `mcp__plugin_dekko_dekko__*`
(this skill writes the bare name). If neither is in your toolset, they
may be deferred: search for "dekko" before falling back to Read/Grep.
With no MCP tools at all, use the CLI column below. No `.dekko/` yet?
Run `dekko map .` (or `/map`) once.

## The default ladder

Each rung is roughly an order of magnitude cheaper than the next:

1. **Orient**: `summary`, unless the session-start preamble is
   already in context.
2. **Locate**: `search_code` when you know what the code does,
   `query_symbol` when you know its name. Never grep for a definition.
3. **Shape**: `outline` on the file or directory the hit lives in.
4. **Relate**: `get_callers` / `get_callees` / `find_usages` /
   `impacted_tests`, or `workset` for the whole bundle in one call.
5. **Read**: only the lines you are about to edit, via
   `get_context_pack` with `with_source` or a line-ranged Read. A
   whole-file Read is the last resort.

Your own edits never make dekko wrong: every tool regenerates a stale
map incrementally before answering. Don't run `dekko map` by hand, and
don't fall back to grep because "the map might be stale now."

## Need → tool

| Need | MCP tool | CLI |
|---|---|---|
| Find code by what it does (not its name) | `search_code` | `dekko search "<text>"` |
| A symbol's signature, doc, line, fan-in/out | `query_symbol` | `dekko query symbol <sym>` |
| Who calls it / what it calls | `get_callers` / `get_callees` | `dekko query callers\|callees <sym>` |
| A symbol's whole neighborhood, optionally with source | `get_context_pack` | `dekko context <sym>` |
| A file's or directory's shape, no bodies (~1/10 a Read) | `outline` | `dekko outline <path>` |
| Everything for a change or symbol, one budget | `workset` | `dekko workset [REV] \| --symbol NAME` |
| Tests a change impacts | `impacted_tests` | `dekko affected [REV]` |
| In-repo call sites of a third-party name (`requests.get`) | `find_usages` | `dekko query uses <name>` |
| What takes or returns a type | `find_type_usages` | `dekko query type <type>` |
| What a type extends, and what extends it | `get_supertypes` / `get_subtypes` | `dekko query supertypes\|subtypes <type>` |
| How far to trust fan-in counts here | `check_ambiguous` | `dekko ambiguous` |
| Symbol notes (rationale) | `list_notes` / `add_note` | `dekko note list\|add` |
| Text dekko doesn't model: strings, comments, config, prose | grep/Read | grep/Read |

`search_code` is relevance-ranked over names, signatures and doc lines,
not substring matching, so phrase it as behavior ("where do we retry
failed requests?"). Zero hits is not an error: broaden and retry.

**Targets** accept a bare name, `Class.method`, `file.py:name`, or
`file.py::name`. If a reply says the target is ambiguous, append the
`:LINE` from one of the candidate rows it prints
(`file.py:Class.method:42`) instead of reading the file. MCP tools
take the target as `symbol`, `name`, `target` or `type`; pass one.

**Knobs** (`budget`, `sites`, `with_source`, `task`, `include_tests`,
`hops`) are documented in each tool's schema, and every footer says
what was dropped to fit. Raise a budget once rather than paying for
everything up front.

## More in `reference.md`

Read `reference.md` in this skill's directory when you need:

- **CLI-only queries** with no MCP tool: `diff`, `query file`,
  `ledger`, `query importers|peers|throws|catches|env|cohesion`,
  `deps`, `trace`, `unused`, `stats`, `lean`.
- **The CLI flag for each knob** above, when working without MCP tools.
- **Staleness**: `map_status` / `refresh_map` (full rebuild).

## Boundaries

- Navigate with dekko, read to edit: it doesn't replace reading the
  exact lines you change.
- `get_callers` and `search_code` hide test paths by default; an empty
  result isn't dead code. See `dekko-verify` before acting on a zero.
