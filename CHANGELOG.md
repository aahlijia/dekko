# Changelog

All notable changes to **lidar-map** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Dates are when the work landed on `develop`; releases are cut by pushing a
`v*` tag.

## [0.6.0] — 2026-06-12

Graph analysis: turn the map into a source of code-health insight.

### Added
- `lidar unused` — symbols with no inbound calls, minus roots (`main`,
  test files, decorated/annotated symbols, the language's public surface
  — Rust `pub`, Go capitals, Java `public`, JS/TS `export` — Python
  dunders and `__init__.py` re-exports, plus `--roots GLOB`). A class is
  kept when any of its methods is called. `--limit`, `--json`; exits `1`
  when any are found. It is call-graph based, so it reports leads, not
  verdicts.
- `lidar stats` — file/symbol/edge totals, language mix, top fan-in/out
  hotspots, and largest files (`--top`, `--json`).
- `lidar export` — render the call graph as `--format mermaid|dot`, at
  `--scope symbol|file`, with a `--max-nodes` guard.
- `Symbol` now records `decorated` and `exported` facts (Python
  decorators, Rust attributes/`pub`, Java annotations/`public`, JS/TS
  decorators/`export`), serialized into map.json.
- A test asserting the four declared version strings (pyproject, both
  plugin manifests, uv.lock) agree.

## [0.5.0] — 2026-06-12

Expose the map to agents over the Model Context Protocol.

### Added
- `lidar serve --mcp` — a hand-rolled MCP server speaking
  newline-delimited JSON-RPC 2.0 over stdio, with **no SDK dependency**.
  Six tools mirror the read surface: `query_symbol`, `get_callers`,
  `get_callees`, `get_context_pack`, `map_status`, `refresh_map`.
- The plugin ships an `.mcp.json` (with `cwd` set to
  `${CLAUDE_PROJECT_DIR}`), so `lidar --claude-install` wires the server
  automatically.
- `lidar --mcp-install` registers the server for non-plugin setups via
  `claude mcp add lidar -- lidar serve --mcp`.

### Changed
- Map regeneration was factored into a reusable `regen_map` helper so the
  server can force a full rebuild.

## [0.4.0] — 2026-06-12

Change-awareness and incremental mapping.

### Added
- `lidar diff [REV]` — symbols added/removed/changed since a git rev
  (default: the commit the map was generated at), each with its impacted
  callers. Compares the working tree against `git archive` of the rev;
  "changed" means the symbol's source text differs. `--limit`, `--json`;
  exits `0` (no differences) / `1` (differences) / `2` (bad rev).
- A per-file extraction cache under `.lidar/`, keyed on the provenance
  content hash, so re-mapping only re-parses files whose contents
  changed. `lidar map --full` forces a cold rebuild.

### Changed
- The first time the cache is written, `.lidar/` is made self-ignoring
  and appended to the repository `.gitignore`.

## [0.3.0] — 2026-06-12

From a one-shot generator to a queryable context service.

### Added
- A subcommand CLI: `map`, `query`, `context`, `status`. The v0.2 flags
  (`--map`, `--claude-install`, `--version`) keep working as aliases.
- `lidar query` — `callers`, `callees`, `symbol`, and `file` lookups
  against map.json, with exit codes `3` (not found) and `4` (ambiguous).
  Targets accept `name`, `Class.method`, or `file.py:name`.
- `lidar context` — a minimal signature neighborhood for editing a
  symbol, with `--hops N` and a `--budget TOKENS` trimmer.
- `lidar status` — freshness report from the provenance stamp; exits `0`
  (fresh) / `1` (stale).
- map.json provenance (document version 2): tool version, git commit,
  discovery options, and per-file content hashes.
- Read commands auto-regenerate a stale map (`--no-regen` to opt out);
  `lidar map --if-stale` short-circuits when the map is already fresh.

## [0.2.0] — 2026-06-11

Packaged for distribution.

### Changed
- Converted from a `uv`-run script into a pip-installable package:
  `tool/` → `src/lidar_map/`, a hatchling build, and a `lidar` console
  script. Distributed on PyPI as **lidar-map**.
- The Claude Code plugin is embedded in the wheel and installed with
  `lidar --claude-install`.

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

Initial release: the **lidar** Claude Code plugin.

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

[0.6.0]: https://github.com/aahlijia/lidar/releases/tag/v0.6.0
[0.5.0]: https://github.com/aahlijia/lidar/releases/tag/v0.5.0
[0.4.0]: https://github.com/aahlijia/lidar/releases/tag/v0.4.0
[0.3.0]: https://github.com/aahlijia/lidar/releases/tag/v0.3.0
[0.2.0]: https://github.com/aahlijia/lidar/releases/tag/v0.2.0
[0.1.1]: https://github.com/aahlijia/lidar/releases/tag/v0.1.1
[0.1.0]: https://github.com/aahlijia/lidar/releases/tag/v0.1.0
