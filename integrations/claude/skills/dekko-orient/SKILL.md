---
name: dekko-orient
description: Use dekko's tools instead of grep/Read whenever a repo has a .dekko/ directory — not just at session start. Trigger on any "find/locate/understand this symbol," "what does this file/dir contain," "what will this change break," or "read this file" impulse. dekko has already parsed and indexed the repo; grepping or reading whole files re-does work dekko already did, at higher token cost.
---

# Orienting with dekko (read less of the repo)

A repo with a `.dekko/` directory is already parsed and indexed. Call
dekko's MCP tools (`mcp__dekko__*`) or CLI **before** grepping for a
symbol or reading a whole file — that is the token cost this skill
exists to avoid. If `mcp__dekko__*` tools don't show up in your
toolset yet, they may be deferred behind a tool search — search for
"dekko" before falling back to Read/Grep, don't give up after one miss.

No `.dekko/` directory yet? Run `dekko map .` (or the `/map` command)
once, then use the tools below.

## When to reach for dekko vs. grep/Read

| Need | Use | Not |
|---|---|---|
| You know *what the code does* but not its exact name/spelling | `search_code` | grepping guessed keywords across every file |
| A symbol's signature, doc, or its callers/callees | `query_symbol`, `get_callers`, `get_callees` | grep for the name |
| Which in-repo symbols reference an external/third-party name (a library import, not a local symbol) | `find_usages` | grepping the import name across every file |
| A file or directory's shape | `outline` | reading the whole file |
| Everything needed to work a diff or symbol | `workset` | assembling outlines + packs by hand |
| Tests a change impacts | `impacted_tests` | guessing from filenames |
| Whether the map on disk is stale, and fixing it without shell access | `map_status`, `refresh_map` | assuming the map is current; shelling out to `dekko map` when only MCP tools are available |
| Text dekko doesn't model — strings, comments, config, prose | grep/Read | — |

`search_code` in particular replaces the "grep for a few plausible
keywords and hope" impulse: it's BM25-style relevance over symbol
names, signatures, and doc lines (not substring matching), so it
finds the right symbol even when your guessed keyword isn't literally
in the source. Reach for it first whenever the task is phrased as
behavior ("where do we retry failed requests?") rather than a known
identifier.

## Orient first

```
mcp__dekko__summary          # MCP tool — capped at ~2000 tokens by default
dekko orient                 # CLI — same digest + steering preamble, capped at ~1500 tokens
```

Names every directory's purpose, load-bearing/orchestrating symbols,
entrypoints, and largest files in a few hundred tokens.

Caution: bare `dekko summary` (CLI, no `--budget`) and the raw
`dekko://summary` MCP resource are **unbounded** — a large repo's
digest can run ~30k characters. Call the `summary` tool, `dekko
orient`, or pass `--budget` explicitly instead.

## Read less of the repo

```
mcp__dekko__search_code <text>        # or: dekko search "<free-text query>"
```

Don't know the exact symbol name? Describe what it does instead of
grepping guessed keywords — `search_code`/`dekko search` ranks
symbols by relevance to a free-text description (BM25-style over
names, signatures, and doc lines), not substring matching. Falls back
to zero hits (not an error) rather than a wrong match; broaden the
query and retry. Once you have an exact name from a hit, switch to
`query_symbol`/`get_callers` for the precise picture.

```
mcp__dekko__outline <file-or-dir>     # or: dekko outline path/to/file.py
```

Module doc + every symbol's signature, first doc line, and line
number — no bodies, ~1/10 the cost of a full read. A directory target
rolls up its files.

```
mcp__dekko__workset                   # or: dekko workset [REV] | dekko workset --symbol NAME
```

One budgeted bundle for a change or symbol: impacted tests +
touched-file outlines + call-graph packs for the most central touched
symbols — replaces assembling `affected` + N outlines + N packs by
hand.

```
mcp__dekko__query_symbol <sym>        # or: dekko query symbol <sym>
mcp__dekko__get_context_pack <sym>    # or: dekko context <sym>
mcp__dekko__get_callers <sym>         # or: dekko query callers <sym>
mcp__dekko__get_callees <sym>         # or: dekko query callees <sym>
mcp__dekko__find_usages <name>        # or: dekko query uses <name>
mcp__dekko__impacted_tests [REV]      # or: dekko affected [REV]
```

`query_symbol` gives signature/doc/fan-in-out at a glance;
`get_callers`/`get_callees` give the actual exact call edges (unlike
grep, which can't tell a call from a same-named string) — use them
for impact analysis before a change. `get_context_pack` bundles a
symbol's neighborhood in one budgeted pack. `find_usages` is the
external-name counterpart to `get_callers`: point it at a third-party
import (e.g. `requests.get`) to find in-repo call sites, rather than
grepping the import name across every file.

Targets accept a bare name, `Class.method`, `file.py:name`, or the
`file.py::name` / `Class::method` form (the C++/Rust habit) — all
resolve to the same symbol.

## Check and fix staleness without shelling out

```
mcp__dekko__map_status                # or: dekko status (freshness only, no regen)
mcp__dekko__refresh_map [full]        # or: dekko map --if-stale .
```

Every other MCP tool here already auto-regenerates a stale map before
answering (unless the server was started with `--no-regen`), so this
is rarely needed for correctness. Reach for it when you want the
staleness fact itself without paying for a regen (`map_status`: what
changed, added, removed — or "no map yet"), or want to force a full
uncached rebuild rather than the default incremental one
(`refresh_map` with `full: true`) — useful after a bulk rename or
history rewrite where incremental diffing would do needless
per-symbol work.

## CLI-only structural queries (no MCP tool — use Bash)

These answer narrower relational questions than the MCP tools above,
and have no MCP equivalent — reach for them with Bash instead of
grepping by hand:

| Need | Use | Not |
|---|---|---|
| What else imports/depends on this module before I change or remove it | `dekko query importers <source>` | grepping the import string across every file |
| What other symbols probably belong in the same module (share callees with a target) | `dekko query peers <symbol>` | eyeballing and diffing call lists by hand |
| What can calling this function raise, before I change it | `dekko query throws <symbol>` (`--transitive` for the callee-tree version) | reading every function on the call path hunting for `raise`/`throw` |
| Whether a given exception type is actually handled anywhere | `dekko query catches <type>` | grepping `except`/`catch` blocks across the repo |
| Where a specific env var is read | `dekko query env <NAME>` (`--list` for every var read anywhere) | grepping `getenv`/`process.env`/`os.environ` across the repo |
| A cheap first gut-check before splitting a file (which symbols are mutually reachable) | `dekko query cohesion <file>` | reading the whole file to guess groupings — and note this is a weak, disclosed-as-such signal, not real clustering |
| File-to-file import structure / circular-import hunting | `dekko deps` (`--file`, `--cycles`) | tracing `import`/`use`/`#include` statements by hand |
| Shortest call path between two symbols, dead-code leads, hotspot stats, terse map | `dekko trace \| unused \| stats \| lean ...` | — |

All of these are CLI-only by design (schema-token cost vs. actual
per-turn need) — see `docs/cli.md` for full flag reference and
per-command caveats (language coverage, exact-match rules, weak-signal
disclosures) before relying on one heavily.

## Boundaries

- Structural aids, not a substitute for reading the exact lines you
  are about to edit — outline/query to navigate, read to edit.
- Stateless: re-run when you need it; a digest doesn't track edits
  made after it was generated.
- Most tools take a `budget`/`--budget`; the footer reports token cost
  and what was dropped to fit.
- `get_callers` hides test-file callers by default (`include_tests`
  to include them) — an empty result doesn't mean dead code.
