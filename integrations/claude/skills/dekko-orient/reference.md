# dekko-orient reference

Loaded on demand from `SKILL.md`. Full flag reference and per-command
caveats (language coverage, exact-match rules, weak-signal disclosures)
are in `docs/cli.md` in the dekko repo.

## Knobs, with their CLI flags

| MCP arg | CLI flag | What it does |
|---|---|---|
| `budget` | `--budget` | Token cap; the footer reports cost and what was dropped. `0` means no cap |
| `sites` | `--sites` | Callers/callees: one row per call site with `path:line`. Use it before editing call sites |
| `with_source` | `--with-source` | Context pack: inline the target's body and hop-1 call-site lines, budget-counted. Usually cheaper than reading the whole file |
| `task` | `--task` | `workset` / `get_context_pack`: rank output by relevance to what you're doing |
| `include_tests` | `search --include-tests`; `query callers` includes tests unless `--no-tests` | MCP `get_callers` and `search_code` hide test paths by default; `get_callees` doesn't. Include them before concluding "no callers" |
| `hops` | `--hops` | Context pack depth, default 1; widen to 2 only when 1 isn't enough |
| `type_impact` | `--type-impact` | `workset` on a type: pull in every type-usage site and implementor |
| `transitive` | `--transitive` | Supertypes/subtypes: the full chain |

## CLI-only queries (no MCP tool, use Bash)

| Need | Use | Instead of |
|---|---|---|
| A file's symbol list with line numbers, no docs (cheaper than `outline`) | `dekko query file <path>` | reading the file to list what it defines |
| Which files and symbols this session already pulled into context | `dekko ledger` | re-reading a file you already have |
| Symbols changed since a rev, with their callers | `dekko diff [REV]` | reading `git diff` and grepping each touched name |
| What imports this module, before changing or removing it | `dekko query importers <source>` | grepping the import string |
| Symbols that probably belong together (shared callees) | `dekko query peers <symbol>` | diffing call lists by hand |
| What calling a function can raise | `dekko query throws <symbol>` (`--transitive` for the callee tree) | reading the call path for `raise`/`throw` |
| Whether an exception type is handled anywhere | `dekko query catches <type>` | grepping `except`/`catch` blocks |
| Where an env var is read | `dekko query env <NAME>` (`--list` for all) | grepping `getenv`/`process.env`/`os.environ` |
| Gut-check before splitting a file (weak signal, disclosed as such) | `dekko query cohesion <file>` | reading the file to guess groupings |
| File-to-file imports, circular imports | `dekko deps` (`--file`, `--cycles`) | tracing import statements by hand |
| Which names collide when a fan-in count looks off | `dekko ambiguous` (`--by name`, `--name X`) | recounting with grep |
| Shortest call path, dead-code leads, hotspots, terse map | `dekko trace \| unused \| stats \| lean` | |

These are CLI-only by design: their schemas would cost tokens in every
session for questions most sessions never ask.

## Orientation digests

| Command | Cap |
|---|---|
| `summary` MCP tool | ~2000 tokens by default |
| `dekko orient` | ~1500 tokens, digest plus a steering preamble |
| `dekko summary` | 5000 tokens by default; `--budget` to tighten |

Avoid the raw `dekko://summary` MCP *resource*: it is unbounded, and a
large repo's digest runs ~30k characters.

## Staleness

```
map_status               # or: dekko status (freshness only, no regen)
refresh_map [full]       # or: dekko map --if-stale .
```

Every other tool regenerates a stale map before answering (unless the
server was started with `--no-regen`), so these are rarely needed for
correctness. Use `map_status` for the staleness fact itself (what
changed, added, removed, or "no map yet") without paying for a regen,
and `refresh_map` with `full: true` to force an uncached rebuild after
a bulk rename or history rewrite.
