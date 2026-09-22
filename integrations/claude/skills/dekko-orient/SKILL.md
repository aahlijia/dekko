---
name: dekko-orient
description: Use dekko's tools instead of grep/Read whenever a repo has a .dekko/ directory — not just at session start. Trigger before any Grep/Glob/Read of a source file, before a `grep`/`rg`/`find`/`cat` in Bash, and on any "find/locate/understand this symbol," "what does this file/dir contain," "who calls this," "what will this change break," or "which tests should I run" impulse. dekko has already parsed and indexed the repo; grepping or reading whole files re-does work dekko already did, at higher token cost.
---

# Orienting with dekko (read less of the repo)

A repo with a `.dekko/` directory is already parsed and indexed. Call
dekko's MCP tools or CLI **before** grepping for a symbol or reading a
whole file — that is the token cost this skill exists to avoid.

The MCP tools appear as `mcp__dekko__*` (standalone `claude mcp add`
install) or `mcp__plugin_dekko_dekko__*` (plugin install); this skill
writes the short form. If neither shows up in your toolset yet, they
may be deferred behind a tool search — search for "dekko" before
falling back to Read/Grep, don't give up after one miss. With no MCP
tools at all, every tool below has the CLI form shown next to it.

No `.dekko/` directory yet? Run `dekko map .` (or the `/map` command)
once, then use the tools below.

## The default ladder

Work top-down. Each rung is roughly an order of magnitude cheaper than
the one it replaces, and most tasks never need the bottom rung on more
than one or two files:

1. **Orient** — `summary` (or the session-start hook's preamble, if it
   is already in context; don't pay for it twice).
2. **Locate** — `search_code` when you know what the code does,
   `query_symbol` when you know its name. Never grep for a definition.
3. **Shape** — `outline` on the file or directory the hit lives in.
4. **Relate** — `get_callers` / `get_callees` / `find_usages` /
   `impacted_tests` for impact, or `workset` for the whole bundle in
   one call.
5. **Read** — only the exact lines you are about to edit, with
   `get_context_pack` + `with_source` or a line-ranged Read. A
   whole-file Read is the last resort, not the first move.

Edits never make dekko wrong. Every tool auto-regenerates a stale map
incrementally before answering (unless the server was started with
`--no-regen`), so keep using dekko after your own changes — don't stop
and run `dekko map` by hand, and don't fall back to grep because "the
map might be stale now."

## When to reach for dekko vs. grep/Read

| Need | Use | Not |
|---|---|---|
| You know *what the code does* but not its exact name/spelling | `search_code` | grepping guessed keywords across every file |
| A symbol's signature, doc, line number, or its callers/callees | `query_symbol`, `get_callers`, `get_callees` | grep for the name; Read to find the line |
| Which in-repo symbols reference an external/third-party name (a library import, not a local symbol) | `find_usages` | grepping the import name across every file |
| Which functions take or return a type (what breaks if its shape changes) | `find_type_usages` | grepping the type name |
| What a class/interface/trait extends or implements, and what extends it | `get_supertypes`, `get_subtypes` (`transitive` for the full chain) | reading class headers file by file |
| A file or directory's shape | `outline` | reading the whole file |
| Everything needed to work a diff or symbol | `workset` (`type_impact` when the symbol is a type) | assembling outlines + packs by hand |
| Tests a change impacts | `impacted_tests` | guessing from filenames; running the whole suite |
| How much to trust fan-in counts in this repo | `check_ambiguous` | assuming every count is exact |
| Whether the map on disk is stale, and fixing it without shell access | `map_status`, `refresh_map` | assuming the map is current; shelling out to `dekko map` when only MCP tools are available |
| Text dekko doesn't model — string literals, comments, config, prose | grep/Read | — |

`search_code` in particular replaces the "grep for a few plausible
keywords and hope" impulse: it's BM25-style relevance over symbol
names, signatures, and doc lines (not substring matching), so it
finds the right symbol even when your guessed keyword isn't literally
in the source. Reach for it first whenever the task is phrased as
behavior ("where do we retry failed requests?") rather than a known
identifier. It returns zero hits (not an error) when nothing matches;
broaden the query and retry, then switch to `query_symbol` /
`get_callers` once you have an exact name.

## Orient first

```
mcp__dekko__summary          # MCP tool — capped at ~2000 tokens by default
dekko orient                 # CLI — same digest + steering preamble, capped at ~1500 tokens
dekko summary                # CLI — capped at 5000 tokens by default; pass --budget to tighten
```

Names every directory's purpose, load-bearing/orchestrating symbols,
entrypoints, and largest files in a few hundred tokens.

Caution: the raw `dekko://summary` MCP *resource* is **unbounded** by
design — a large repo's digest can run ~30k characters. Call the
`summary` tool or `dekko orient` instead.

## Read less of the repo

```
mcp__dekko__search_code <text>        # or: dekko search "<free-text query>"
mcp__dekko__outline <file-or-dir>     # or: dekko outline path/to/file.py
mcp__dekko__workset                   # or: dekko workset [REV] | dekko workset --symbol NAME
```

`outline` is the module doc plus every symbol's signature, first doc
line, and line number — no bodies, ~1/10 the cost of a full read. A
directory target rolls up its files. `workset` is one budgeted bundle
for a change or symbol: impacted tests + touched-file outlines +
call-graph packs for the most central touched symbols — it replaces
assembling `affected` + N outlines + N packs by hand.

```
mcp__dekko__query_symbol <sym>        # or: dekko query symbol <sym>
mcp__dekko__get_context_pack <sym>    # or: dekko context <sym>
mcp__dekko__get_callers <sym>         # or: dekko query callers <sym>
mcp__dekko__get_callees <sym>         # or: dekko query callees <sym>
mcp__dekko__find_usages <name>        # or: dekko query uses <name>
mcp__dekko__find_type_usages <type>   # or: dekko query type <type>
mcp__dekko__get_supertypes <type>     # or: dekko query supertypes <type>
mcp__dekko__get_subtypes <type>       # or: dekko query subtypes <type>
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
resolve to the same symbol. If a reply says the target is ambiguous
(an overload set sharing the same file and name), append `:LINE` from
one of the candidate rows it prints (`file.py:Class.method:42`) rather
than reading the file to pick one.

## Get more out of each call

- **Pass a `budget`** (`--budget`) whenever you don't need the default.
  The footer reports token cost and what was dropped to fit, so you
  can raise it once instead of paying for everything up front.
- **`sites=true`** (`--sites`) on callers/callees gives one row per
  call site with `path:line` — use it when you're about to edit call
  sites, skip it when you only need the caller list.
- **`with_source`** (`--with-source`) on a context pack inlines the
  target's body and hop-1 call-site lines, budget-counted — usually
  cheaper than a Read of the whole file when you need one function.
- **`task="..."`** (`--task`) on `workset` and `get_context_pack`
  ranks the output by relevance to what you're doing, blended with
  structural centrality and the working diff.
- **`include_tests`** — `get_callers` and `search_code` hide test-path
  results by default and say so in a footer; `get_callees` does not.
  Pass `include_tests=true` before concluding something has no callers.
- **`hops`** on a context pack defaults to 1; widen to 2 only when the
  1-hop neighborhood clearly isn't enough.

## Impulses to redirect

| If you're about to... | Do this instead |
|---|---|
| Read a file to find where a function is defined | `query_symbol <name>` (has the line number) |
| Grep a name to see who calls it | `get_callers <name>` |
| Read a function's body to see what it calls | `get_callees <name>` |
| Read a whole file to "get the lay of it" | `outline <file>` |
| Grep test files for a symbol to pick tests | `impacted_tests` / `dekko affected` |
| Read several files to understand one change | `workset` (one call) |
| Run `dekko map` after editing | Nothing — the next call regenerates incrementally |
| Grep a struct/class name to see where its shape matters | `find_type_usages` (+ `get_subtypes`) |

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
| Which symbols changed since a rev, with their callers | `dekko diff [REV]` | reading `git diff` and grepping each touched name |
| What else imports/depends on this module before I change or remove it | `dekko query importers <source>` | grepping the import string across every file |
| What other symbols probably belong in the same module (share callees with a target) | `dekko query peers <symbol>` | eyeballing and diffing call lists by hand |
| What can calling this function raise, before I change it | `dekko query throws <symbol>` (`--transitive` for the callee-tree version) | reading every function on the call path hunting for `raise`/`throw` |
| Whether a given exception type is actually handled anywhere | `dekko query catches <type>` | grepping `except`/`catch` blocks across the repo |
| Where a specific env var is read | `dekko query env <NAME>` (`--list` for every var read anywhere) | grepping `getenv`/`process.env`/`os.environ` across the repo |
| A cheap first gut-check before splitting a file (which symbols are mutually reachable) | `dekko query cohesion <file>` | reading the whole file to guess groupings — and note this is a weak, disclosed-as-such signal, not real clustering |
| File-to-file import structure / circular-import hunting | `dekko deps` (`--file`, `--cycles`) | tracing `import`/`use`/`#include` statements by hand |
| Which names collide, and where, when a fan-in count looks off | `dekko ambiguous` (`--by name`, `--name X`) | trusting the count, or grepping to recount |
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
  to include them) — an empty result doesn't mean dead code. See the
  `dekko-verify` skill before acting on any zero-caller result.
