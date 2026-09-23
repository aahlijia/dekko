# CLAUDE.md

Project context for Claude Code (and other agents) working in this repo.

## What this project is

**dekko** is a fast, offline, dependency-free static code map generator and
codebase indexer for LLM coding agents. It parses a repo once with
tree-sitter (no model tokens spent parsing) into `MAP.md` (human-readable)
and `map.json` (machine-readable), then answers targeted structural
questions — "who calls this function," "what does this file contain,"
"what tests does this change impact" — without an agent reading whole
files or grepping blind. It ships three ways: a CLI (`dekko`), a Claude
Code `/map` plugin + MCP server, and Cline MCP support.

Source lives under `src/dekko/`, split into subpackages by role:
`core/` (parsing primitives — model, extractor, grammars, languages,
walker, resolver), `render/` (MAP.md/JSON/HTML/lean/export
rendering), `analysis/` (read-side commands — query, outline, search,
affected, trace, unused, stats, summary, workset, contextpack, diff,
relevance, ambiguous, deps, sanity), `daemon/` (the daemon process and
its transport), `integrations/` (cli, server, hooks, cline, orient,
claude_md, doctor), `storage/` (on-disk caches/locks/notes/ledger under
`.dekko/`), and a handful of modules with no subpackage-specific home
(`classify.py`, `textutil.py`, `source.py`, `repo_ops.py`) staying
directly under `src/dekko/`. `dekko outline src/dekko/<subpackage>/<file>.py` or
`dekko query symbol <name>` are cheaper ways to get oriented in this
codebase than reading whole files — dekko is a good tool for exploring
its own source. See `README.md` and `docs/` (`docs/install.md`,
`docs/cli.md`, `docs/claude-code.md`) for user-facing docs, and
`CONTRIBUTING.md` for the dev-setup/test/lint/release commands.

## Working in this repo

- `uv run pytest` / `uv run ruff check .` / `uv run ruff format --check .`
  — this is what CI runs; match it locally before considering something
  done. A project `.venv` exists (`.venv/bin/python -m pytest -q` works
  equivalently if `uv run` isn't available).
- Tests live in `tests/`, mirroring `src/dekko/`'s subpackages
  (`tests/core/`, `tests/render/`, `tests/analysis/`, `tests/daemon/`,
  `tests/integrations/`, `tests/storage/`) for tests that map cleanly
  to a single moved module; cross-cutting/behavioral tests (exercising
  several modules or the CLI end-to-end) and tests for the top-level
  modules stay flat directly under `tests/`. Test fixtures (tiny
  sample-language files) live in `tests/fixtures/`.
- `test-repos/` holds real, unmodified open-source repos (awesome-go,
  claude-buddy, claude-code, cline, spring-boot, tensorflow, zed) used as
  realistic targets for manual/agent evaluation of dekko itself — not part
  of the pytest suite. It's gitignored (`test-repos/` in `.gitignore`), so
  nothing under it is tracked; `test-repos/reports/` (see below) is where
  evaluation write-ups accumulate locally.
- `test-repos/TESTING-GUIDE.md` is the checklist an agent dispatched to
  test dekko should work from: every CLI command/flag, the MCP tools, the
  daemon, install/uninstall flows, hooks, and the known-hard cross-cutting
  correctness cases (overload disambiguation, search relevance, call-graph
  resolution on trait/interface-heavy code, vendored-dir exclusion, budget
  capping, staleness). **Update it whenever a feature is added or an
  existing one's behavior changes** — a new subcommand, flag, MCP tool, or
  behavior change isn't done until this guide reflects it; don't leave it
  to whoever tests next to discover the gap.
- `.dekko/MAP.md` / `.dekko/map.json` at the repo root are dekko's own
  generated map of itself; the map is git-ignored by default but this
  repo tracks its own for dogfooding. **Always regenerate the map before
  committing and commit the refreshed `.dekko/` files with the change:**
  run `dekko map` (or `uv run dekko map`) after your last source edit,
  then `git add .dekko/`. A checkout's map must match its source, so a
  source commit with a stale map is a bug. The `dekko-map` pre-commit
  hook runs `dekko map --if-stale` but only when hooks are installed and
  it does not stage the result, so it is not a substitute for doing this
  yourself.
- Follow the branch/PR conventions in `CONTRIBUTING.md`: one conceptual
  change per PR, Conventional-Commits-style commit prefixes (`feat`/
  `fix`/`docs`/`style`/`refactor`/`perf`/`test`/`chore`/`build`/`ci`/
  `revert` — CONTRIBUTING.md's "Commit messages" section has the full
  table and what each one means here), dekko's versioning rules
  (CONTRIBUTING.md's "Versioning" section: major is maintainer-only,
  minor closes out a testing round's fix cycle or ships a new
  top-level capability, patch is everything else; each round fix
  track is its own commit with its own patch bump, see "Evaluation
  rounds" below), releases cut by pushing a `v*` tag.

## Evaluation rounds: reports, fix designs, and versions

When dekko itself is evaluated against real repos (token-cost comparisons
vs. Read/grep, bug hunts, regression checks after a fix), the work is
organized into **rounds**, grouped by dekko major version. Read
`test-repos/reports/README.md` first: it's the index for every round and
documents the layout. `CONTRIBUTING.md`'s "Testing rounds and the version
line" section is the authority on the version rules below.

**Round numbering resets at every major release.** Rounds are named
`round<MAJOR>.<N>`: round 1.1 is the first round of the 1.x series (it
is the former round 34), the next is 1.2, and after 2.0.0 the counter
starts again at 2.1. Pre-1.0 rounds 01-33 keep their old names under
`v0/`.

**Where things go** (both trees are gitignored, local-only):

| What | Where |
|---|---|
| Eval reports for round X.N | `test-repos/reports/vX/roundX.N/` |
| Fix designs for round X.N | `.features/fixes/vX/roundX.N/` (start with `00-overview.md`) |
| Everything from before 1.0.0 | `test-repos/reports/v0/`, `.features/fixes/v0/` |

Older source comments, tests, and CHANGELOG entries cite the pre-move
flat paths (`test-repos/reports/NN-slug/`, `.features/fixes/roundNN/`).
Those now live under `v0/`, except round 34, which is `v1/round1.1/`.

**Versions follow rounds:**

- **One fix track = one commit = one patch bump.** Implement each design
  in the round's fix folder on its own, bump the patch version in that
  same commit (`scripts/sync_plugin_version.py` after editing
  `pyproject.toml`), regenerate the map, commit. Never batch two tracks
  into one commit.
- Round X.N's fixes are `X.(N-1).1`, `X.(N-1).2`, ... in commit order:
  round 1.1's fixes are 1.0.1, 1.0.2, ...
- When every track is implemented and verified, the round closes with
  the minor release `X.N.0` (round 1.1 → **1.1.0**), cut through the
  normal release process.
- Mark each design doc's status line as tracks land (e.g. `**Status:
  IMPLEMENTED as 1.0.3 (<commit>)**`) and update the round's README
  section when the round closes.

**When starting a new evaluation round:**

1. Pick the next round number for the current major version (look at
   the highest `roundX.N` under `test-repos/reports/vX/`) and create
   `test-repos/reports/vX/roundX.N/`.
2. Note the dekko version being tested near the top of each report in
   that round, e.g.:
   ```
   dekko version: 1.1.0 (branch develop, commit 7da9367)
   ```
   If the version drifts mid-round (e.g. the CLI got reinstalled from a
   newer commit partway through), say so explicitly rather than picking
   one — this has caused real confusion before (see round 07's
   `awesome-go.md` under `v0/`, which flags exactly this).
3. Add a section for the round to `test-repos/reports/README.md`: what's
   in it, one line per file, and a row in its "dekko version per round"
   table.
4. Fix designs for the round go in `.features/fixes/vX/roundX.N/`, same
   round number as the reports that motivated them. Post-fix
   verification write-ups go back in the round's reports folder. Don't
   split one bug-hunt-to-fix cycle across round numbers.

This lets a regression get traced to *what changed in dekko between the
round that missed it and the round that caught it*, not just to when the
two evaluation sessions happened to run.
