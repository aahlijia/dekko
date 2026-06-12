# Changelog

All notable changes to **dekko** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Dates are when the work landed on `develop`; releases are cut by pushing a
`v*` tag.

## [Unreleased]

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

[Unreleased]: https://github.com/aahlijia/dekko/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/aahlijia/dekko/releases/tag/v0.7.0
[0.6.0]: https://github.com/aahlijia/dekko/releases/tag/v0.6.0
[0.5.0]: https://github.com/aahlijia/dekko/releases/tag/v0.5.0
[0.4.0]: https://github.com/aahlijia/dekko/releases/tag/v0.4.0
[0.3.0]: https://github.com/aahlijia/dekko/releases/tag/v0.3.0
[0.2.0]: https://github.com/aahlijia/dekko/releases/tag/v0.2.0
[0.1.1]: https://github.com/aahlijia/dekko/releases/tag/v0.1.1
[0.1.0]: https://github.com/aahlijia/dekko/releases/tag/v0.1.0
