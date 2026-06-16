# Changelog

All notable changes to **dekko** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Dates are when the work landed on `develop`; releases are cut by pushing a
`v*` tag.

## [Unreleased]

Context & token management for agents: every list-shaped command can now
be held to a token budget, and three new commands (`outline`, `workset`,
`orient`) let an agent orient and scope a change without reading whole
files.

### Added
- Universal token budgeting across `query`, `unused`, `affected`, and
  `context`. Each command now ranks its rows by relevance (production
  before tests, more-connected before leaves), keeps as many as fit, and
  self-meters: text output carries a `(~N tokens · M of T omitted ·
  raise --budget)` footer and JSON carries a matching `meta` object. A
  `--budget` flag caps `query`/`unused`/`affected`; the relation MCP
  tools gained an equivalent `budget` argument.
- `dekko outline <path|dir>`: a file's (or directory's) structure —
  module purpose, each symbol's signature, doc first line, and line
  number, with no bodies — at roughly a tenth the cost of reading the
  file, plus a `full ≈ X · outline ≈ Y (P%)` size frame. Exposed as an
  MCP tool whose description steers agents to prefer it before reading a
  file.
- `dekko workset [REV] | --symbol NAME`: one budgeted bundle for a whole
  change — the impacted test files (with a ready-to-paste `pytest` hint),
  outlines of the touched files, and context packs for the most central
  touched symbols. A single shared budget (default 6000) trims
  detail-first so breadth survives a tight cap; `--packs` controls how
  many symbols get a pack. Also available as an MCP tool.
- `dekko orient`: an opt-in orientation layer. With no arguments it
  prints a steering digest (a budgeted repo summary plus pointers to the
  query surface); with `--read PATH` it emits a one-line nudge to outline
  a file before reading it, but only when the file is large enough to be
  worth it, and never blocks. Ships with a `dekko-orient` skill and
  documented (opt-in) `SessionStart` / `PreToolUse` hook snippets.

### Changed
- Internal: the directory-of-a-path helper was promoted from `summary`
  to a shared `textutil.dir_of`, in preparation for an upcoming lean
  map renderer. No user-visible change.

## [0.9.0] — 2026-06-14

Track B: the human-readable map. `MAP.md` is now a navigable document —
an overview with rankings and an architecture diagram, sharded pages for
large repos, hotspots and a freshness line — plus a standalone
interactive HTML export.

### Added
- `MAP.md` now renders purpose lines from the v3 schema's `doc`
  fields: the Contents index shows each file's module purpose after
  its symbol count, file section headers carry the same purpose, and
  each symbol block shows its docstring first line under the
  signature. Files with no doc, and parse-error files, render cleanly
  with no placeholder noise.
- `MAP.md` now opens with an `## Overview` section: a per-directory
  rollup table (files, symbols, internal vs. cross-directory call
  edges, purpose), linked load-bearing and orchestrator rankings,
  entry points, and parse errors. It is the markdown skin of
  `dekko summary` — one computation, two renderings — so the digest
  and the document always agree. Cross-directory edge counts are the
  new "coupling at a glance" number.
- The `MAP.md` Overview now embeds a `mermaid` architecture diagram,
  rendered natively by GitHub (no toolchain or network). A scale guard
  tiers it down as the repo grows: the file-scope graph while it fits
  under `--max-nodes` (300), then a directory-scope collapse, then a
  one-line pointer to `dekko export --format mermaid`. MAP.md and
  `dekko export` share one graph generator.
- `dekko map --shard auto|always|never` (default `auto`): large maps
  split into per-directory `map/<dir-slug>.md` pages with `MAP.md` as
  the index (Overview + linked TOC); `auto` shards once the single
  document would exceed ~4,000 lines or 200 KB. Anchor ids are global,
  so a symbol's link is identical in either shape. Stale pages from a
  previous run (e.g. a renamed directory) are cleared before writing.
- The `MAP.md` Overview gained a **Largest files** list (linked, by
  symbol count; also shown by `dekko summary`) and a best-effort
  **Hotspots** table — recent git churn weighted by fan-in, surfacing
  the files where a change spreads furthest. The hotspots section is
  omitted silently on non-git roots or any git failure.
- The `MAP.md` header now carries a freshness/trust line —
  `Mapped N files in T ms (cache: X reused / Y parsed)` — so a reader
  can see at a glance how the map was built.
- `dekko map --order path|name|fan-in` (default `path`): order the
  `MAP.md` file sections by path (today's walk order), base filename,
  or fan-in (most depended-on first). `fan-in` also orders the symbols
  within each file by inbound degree — load-bearing first.
- `dekko export --format html`: a single self-contained, interactive
  HTML file (default `.dekko/map.html`) — collapsible directory tree,
  client-side substring search over names/qualnames/paths, and a symbol
  pane with signature, doc, and clickable callers/callees showing
  call-site lines. Test symbols are de-emphasized; the header carries
  the summary stats. No dependencies, no network, no build step; a size
  guard refuses maps too large to inline (exit 2, like `--max-nodes`).
- `dekko export --output PATH` writes any format to a file instead of
  stdout (html defaults to `.dekko/map.html`).

### Changed
- `signature()` moved from `render_md` to `textutil` so renderers and
  the summary/overview share it without an import cycle. Internal
  only; output is unchanged.
- `--output` and `--shard` interact: an explicit `--output FILE` forces
  `--shard never` (one file as asked); `--output DIR` shards into
  `DIR/map/` under the usual rules.
- The `MAP.md` Contents index is quieter: files with no symbols, doc,
  or parse error collapse into a per-directory `also present:` line
  instead of empty sections; test files move into a collapsed
  `<details>tests (N files)</details>` block; and the redundant
  `(parse error)` marker is dropped (the Overview's parse-error list
  already carries it).

## [0.8.0] — 2026-06-13

### Added
- The generated `MAP.md` now opens with a one-line note steering agents
  to `dekko summary` and the `query`/`context`/`affected` commands (or
  the MCP tools) instead of reading the whole file.
- An optional `PostToolUse` hook snippet in the README keeps the map
  refreshed as you edit, made cheap by the freshness fast path below.
- Symbol-anchored **notes** — durable, committed annotations keyed by
  symbol id. `dekko note add <symbol> "<text>"`, `note list [<symbol>]`
  (with `--orphaned` to find notes whose symbol moved), and
  `note rm <symbol> [INDEX]`. Notes live in `.dekko/notes.json` and are
  shown inline by `dekko query symbol` and `dekko context` (toggle with
  `--notes/--no-notes`, default on). Exposed over MCP as `add_note` and
  `list_notes` (14 tools total). The plugin ships a `dekko-notes` skill
  telling Claude Code to consult notes before editing, write them after
  non-obvious changes, and re-anchor them after a rename.
- `dekko summary` — a ~40-line repo digest meant to be read whole:
  file/symbol/edge counts, language mix, a per-directory rollup (file
  and symbol counts, internal vs cross-directory coupling, and a
  purpose line from the directory's index/module docstring), the
  load-bearing (fan-in) and orchestrating (fan-out) symbols, likely
  entry points, and parse errors. `--json` and `--no-tests` like the
  other read commands. The `/map` plugin command now prints this digest
  instead of a raw byte count, and points the agent at the query
  surface rather than the full `.dekko/MAP.md`.
- The MCP server now serves resources: `resources/list` /
  `resources/read` expose `dekko://summary`, and a matching `summary`
  tool covers clients that only call tools (12 tools total).
- `dekko affected [REV]` — the test files a runner should exercise
  after a change. Combines two kinds of evidence: reverse call-graph
  reachability from every added/changed symbol (`direct` at one hop,
  `transitive` beyond), plus an always-on import-edge fallback
  (`import`) that catches tests touching changed *files* through
  fixtures, references, or deleted symbols where no call edge
  survives. Prints a ready-to-paste `pytest …` line; `--json`,
  `--limit`; exit `0` none / `1` impacted / `2` bad rev. Exposed over
  MCP as the `impacted_tests` tool (the server now has 11 tools).
  Static analysis can't see fixture injection or dynamic dispatch, so
  the report is a set of strong leads, not a proof of completeness.
- Context packs (v2): the target and every neighbor now carry their
  doc first line; new strictly-opt-in `--with-source` inlines the
  target's body plus the exact call-site lines (`> line: code`) of
  hop-1 callers. Source counts against `--budget` and is truncated
  from the bottom (with a marker) after neighbors are trimmed — the
  target's signature and location always survive. The MCP
  `get_context_pack` tool accepts a matching `with_source` flag.
  JSON output gains `doc` on symbols, `sites` on neighbors, and
  `source`/`source_truncated` when source is requested.
- `dekko query callers|callees X --sites` — one row per call site
  (`path:line` of each call expression) instead of one per related
  definition. The MCP `get_callers`/`get_callees` tools accept a
  matching `sites` flag.
- `dekko query uses NAME` — list every symbol that references an
  external (out-of-repo) name such as `Path` or `run`, with call
  sites; exposed over MCP as the new `find_usages` tool (the server
  now has 10 tools).
- `--no-tests` on `query`, `context`, `trace`, `unused`, and `stats` —
  excludes test files' symbols and edges from results entirely (a
  bare-name query that collided with a test fixture now resolves).
- Text output of `query` and `context` ends with a `(~N tokens)`
  self-metering footer (never present with `--json`).
- `map.json` doc version **3** (older documents still load, with
  defaults for the new fields):
  - Call edges carry `lines` — the sorted, deduplicated 1-based lines
    of every call site backing the edge. External calls do too.
  - Symbols carry `doc` — the first line of the symbol's docstring or
    doc comment, extracted best-effort per language (Python
    docstrings; `///`/`//!` for Rust; `//` blocks for Go; `/** */`
    and `//` for JS/TS/Java/C/C++; preceding comments for Tier-2
    grammars). Files carry a module-level `doc` the same way.
  - Symbols carry `test` — whether the defining file is test code
    (path-based: test directories and filename patterns).
- New `classify` module hosting the shared test-path classifier
  (moved from `unused`, which now imports it).

### Changed
- Freshness checks are faster on large repos: provenance records an
  `(mtime, size)` signature per file, and a file whose signature is
  unchanged is no longer re-hashed. The content hash still decides for
  any file whose stat moved, so verdicts are unchanged; maps written
  before this release fall back to hashing every file.
- External calls in `map.json` always name their caller: module-level
  calls use the `path::<module>` convention instead of `null`, and
  every entry records its call-site lines.
- The `.dekko/` directory now governs its own ignores via an inner
  `.gitignore` (`*`, `!.gitignore`, `!notes.json`) and dekko no longer
  adds a blanket `.dekko/` entry to the repository `.gitignore` —
  generated maps and the cache stay ignored, while `notes.json` is
  trackable. (A repo whose `.gitignore` already excludes `.dekko/` from
  an earlier version must drop that line for notes to be committable.)

## [0.7.1] — 2026-06-12

### Added
- `dekko --claude-uninstall` — reverses `--claude-install`, removing the
  bundled plugin and its marketplace registration.
- `dekko --mcp-uninstall` — reverses `--mcp-install`, removing the
  standalone MCP server (`claude mcp remove dekko`).

### Changed
- Renamed from **lidar-map** / `lidar` to **dekko** / `dekko` before the
  first PyPI release. The PyPI package, CLI command, Python import package,
  cache directory (`.dekko/`), and MCP server name all changed; no published
  packages were affected.
- `MAP.md` and `map.json` are now written into the `.dekko/` directory by
  default (alongside the cache) instead of the repository root; `--output`
  still overrides the location.
- The gitignore wiring (the inner `.dekko/.gitignore` and the `.dekko/`
  entry in the repo `.gitignore`) is now written only when a run actually
  creates the `.dekko/` directory. If `.dekko/` already exists, gitignores
  are left untouched — removing either entry is no longer undone on the
  next run.

### Fixed
- `install.sh` invokes the freshly installed CLI by absolute path — a
  repo-local `.venv/bin/dekko` could shadow it on `PATH` and break
  `--claude-install` — and forces a rebuild with `--refresh-package`, so a
  re-install at the same version no longer reuses a stale cached wheel.

## [0.7.0] — 2026-06-12

Close out the roadmap backlog: path tracing, a complete MCP surface, and
extractor/resolver correctness and performance work.

### Added
- `dekko trace FROM TO` — shortest call path(s) between two symbols over
  the resolved graph (`--max-paths K`, `--json`). "No path" is a clean
  exit `1`, not an error; unknown/ambiguous endpoints exit `3`/`4` like
  the other read commands. It auto-regenerates a stale map.
- Three new MCP tools so the server now mirrors the whole read surface
  (nine tools): `trace_path`, `find_unused`, and `stats`.
- `dekko map --jobs N` — parallel extraction across a process pool
  (`0` = all cores; sequential by default). Cache hits stay in-process and
  results re-assemble in discovery order, so output is identical to a
  single-worker run.

### Changed
- The `.dekko` extraction cache is now tagged with the `dekko`
  version and discarded on a version change, so an upgrade re-parses once
  and always reflects extractor changes (no manual `--full`).
- Resolver same-file and self-container checks use a pre-built
  `(name, path)` bucket instead of rescanning every repo-wide candidate,
  cutting the worst case for very common names. Resolution results are
  unchanged.

### Fixed
- Relative-import sources no longer double the leading dot
  (`from . import x` rendered as `..x`); they now read `.x` / `..x` /
  `.pkg.x` correctly in context packs.

### Documented
- A "Limitations" section in the README: calls inside Rust macro bodies
  are invisible to tree-sitter token trees, and dynamic dispatch has no
  static call site.

## [0.6.0] — 2026-06-12

Graph analysis: turn the map into a source of code-health insight.

### Added
- `dekko unused` — symbols with no inbound calls, minus roots (`main`,
  test files, decorated/annotated symbols, the language's public surface
  — Rust `pub`, Go capitals, Java `public`, JS/TS `export` — Python
  dunders and `__init__.py` re-exports, plus `--roots GLOB`). A class is
  kept when any of its methods is called. `--limit`, `--json`; exits `1`
  when any are found. It is call-graph based, so it reports leads, not
  verdicts.
- `dekko stats` — file/symbol/edge totals, language mix, top fan-in/out
  hotspots, and largest files (`--top`, `--json`).
- `dekko export` — render the call graph as `--format mermaid|dot`, at
  `--scope symbol|file`, with a `--max-nodes` guard.
- `Symbol` now records `decorated` and `exported` facts (Python
  decorators, Rust attributes/`pub`, Java annotations/`public`, JS/TS
  decorators/`export`), serialized into map.json.
- A test asserting the four declared version strings (pyproject, both
  plugin manifests, uv.lock) agree.

## [0.5.0] — 2026-06-12

Expose the map to agents over the Model Context Protocol.

### Added
- `dekko serve --mcp` — a hand-rolled MCP server speaking
  newline-delimited JSON-RPC 2.0 over stdio, with **no SDK dependency**.
  Six tools mirror the read surface: `query_symbol`, `get_callers`,
  `get_callees`, `get_context_pack`, `map_status`, `refresh_map`.
- The plugin ships an `.mcp.json` (with `cwd` set to
  `${CLAUDE_PROJECT_DIR}`), so `dekko --claude-install` wires the server
  automatically.
- `dekko --mcp-install` registers the server for non-plugin setups via
  `claude mcp add dekko -- dekko serve --mcp`.

### Changed
- Map regeneration was factored into a reusable `regen_map` helper so the
  server can force a full rebuild.

## [0.4.0] — 2026-06-12

Change-awareness and incremental mapping.

### Added
- `dekko diff [REV]` — symbols added/removed/changed since a git rev
  (default: the commit the map was generated at), each with its impacted
  callers. Compares the working tree against `git archive` of the rev;
  "changed" means the symbol's source text differs. `--limit`, `--json`;
  exits `0` (no differences) / `1` (differences) / `2` (bad rev).
- A per-file extraction cache under `.dekko/`, keyed on the provenance
  content hash, so re-mapping only re-parses files whose contents
  changed. `dekko map --full` forces a cold rebuild.

### Changed
- The first time the cache is written, `.dekko/` is made self-ignoring
  and appended to the repository `.gitignore`.

## [0.3.0] — 2026-06-12

From a one-shot generator to a queryable context service.

### Added
- A subcommand CLI: `map`, `query`, `context`, `status`. The v0.2 flags
  (`--map`, `--claude-install`, `--version`) keep working as aliases.
- `dekko query` — `callers`, `callees`, `symbol`, and `file` lookups
  against map.json, with exit codes `3` (not found) and `4` (ambiguous).
  Targets accept `name`, `Class.method`, or `file.py:name`.
- `dekko context` — a minimal signature neighborhood for editing a
  symbol, with `--hops N` and a `--budget TOKENS` trimmer.
- `dekko status` — freshness report from the provenance stamp; exits `0`
  (fresh) / `1` (stale).
- map.json provenance (document version 2): tool version, git commit,
  discovery options, and per-file content hashes.
- Read commands auto-regenerate a stale map (`--no-regen` to opt out);
  `dekko map --if-stale` short-circuits when the map is already fresh.

## [0.2.0] — 2026-06-11

Packaged for distribution.

### Changed
- Converted from a `uv`-run script into a pip-installable package:
  `tool/` → `src/dekko/`, a hatchling build, and a `dekko` console
  script. Distributed on PyPI as **dekko**.
- The Claude Code plugin is embedded in the wheel and installed with
  `dekko --claude-install`.

### Added
- `--map [DIR] [SUBPATH]`, `--output`, `--claude-install`, and
  `--version` flags.
- A GitHub Actions release workflow using PyPI trusted publishing.

## [0.1.1] — 2026-06-11

### Fixed
- `/map` permission failure caused by command substitution in the
  command preamble.
- A Python 3.11+ f-string that failed to compile on the declared 3.10
  floor.
- Repeated tree-sitter query recompilation (now cached), cutting a
  representative run from ~0.26s to ~0.17s.

### Added
- A test that compiles every tool module against the declared Python
  floor.

## [0.1.0] — 2026-06-11

Initial release: the **dekko** Claude Code plugin.

### Added
- A `/map` command that scans the repository with tree-sitter and writes
  `MAP.md` (files, functions, parameters with types, return types, and
  bidirectional call links) plus a machine-readable `map.json` — without
  spending model tokens on parsing.
- Tier-1 languages with full type fidelity (Python, Rust, C, C++,
  JavaScript, TypeScript/TSX, Go, Java) and a generic Tier-2 fallback for
  every other grammar in the language pack.
- Best-effort static call resolution (same container → same file →
  imports → unique repo-wide match); ambiguous calls are marked, never
  guessed.

[Unreleased]: https://github.com/aahlijia/dekko/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/aahlijia/dekko/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/aahlijia/dekko/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/aahlijia/dekko/releases/tag/v0.7.1
[0.7.0]: https://github.com/aahlijia/dekko/releases/tag/v0.7.0
[0.6.0]: https://github.com/aahlijia/dekko/releases/tag/v0.6.0
[0.5.0]: https://github.com/aahlijia/dekko/releases/tag/v0.5.0
[0.4.0]: https://github.com/aahlijia/dekko/releases/tag/v0.4.0
[0.3.0]: https://github.com/aahlijia/dekko/releases/tag/v0.3.0
[0.2.0]: https://github.com/aahlijia/dekko/releases/tag/v0.2.0
[0.1.1]: https://github.com/aahlijia/dekko/releases/tag/v0.1.1
[0.1.0]: https://github.com/aahlijia/dekko/releases/tag/v0.1.0
