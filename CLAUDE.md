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

Source lives under `src/dekko/`; `dekko outline src/dekko/<file>.py` or
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
- Tests live in `tests/`, mirroring `src/dekko/` module-for-module. Test
  fixtures (tiny sample-language files) live in `tests/fixtures/`.
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
  generated map of itself, regenerated on demand — expect them to show as
  modified after running `dekko map`; they're git-ignored by default but
  this repo tracks its own for dogfooding, so check whether a given change
  is worth including before committing it alongside unrelated work.
- Follow the branch/PR conventions in `CONTRIBUTING.md`: one conceptual
  change per PR, `feat:`/`fix:`/`perf:`/`docs:`/`chore:`-style commit
  prefixes (see `git log` for the house style), releases cut by pushing a
  `v*` tag.

## Evaluation reports: `test-repos/reports/`

When dekko itself is evaluated against real repos (token-cost comparisons
vs. Read/grep, bug hunts, regression checks after a fix), the write-ups go
in `test-repos/reports/`, organized into **numbered round folders**
(`01-initial-eval/`, `02-followup-fixes/`, ... `07-tokentest-7repo-fixcycle/`,
...), each described in `test-repos/reports/README.md`. Read that README
first — it's the index and explains what's in each round and why.

**Organize new rounds by the dekko version/commit under test, not just by
date.** Multiple rounds can share a version (dekko doesn't rev its version
string on every commit), so the precise signal is the branch + commit
range, not just `dekko --version`. When starting a new evaluation round:

1. Create a new folder: `test-repos/reports/NN-short-slug/` (next sequential
   number, short descriptive slug — see existing folders for the pattern).
2. Note the dekko version being tested near the top of each report in that
   round, e.g.:
   ```
   dekko version: 0.21.3 (branch feature/semantic-search, commit 7da9367)
   ```
   If the version drifts mid-round (e.g. the CLI got reinstalled from a
   newer commit partway through), say so explicitly rather than picking
   one — this has caused real confusion before (see round 07's
   `awesome-go.md`, which flags exactly this).
3. Add a section for the round to `test-repos/reports/README.md`: what's
   in it, one line per file/subfolder, and add a row to that README's
   "dekko version per round" table.
4. If a round's findings lead to an implementation plan and fixes (as in
   round 07), keep the plan/analysis/investigation/verification docs in
   the *same* round folder as the reports that motivated them — don't
   split a single bug-hunt-to-fix cycle across multiple round numbers.

This lets a regression get traced to *what changed in dekko between the
round that missed it and the round that caught it*, not just to when the
two evaluation sessions happened to run.
