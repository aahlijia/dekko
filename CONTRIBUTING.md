# Contributing

Thanks for taking the time to contribute! 🎉 By participating in this
project, you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Quick start (dev)

```sh
git clone https://github.com/aahlijia/dekko.git
cd dekko
uv sync --extra all        # include Tier-2 grammars + tokenizer
pre-commit install         # local hooks: ruff, dekko map, uv.lock/plugin-version sync
uv run pytest              # test suite
uv run ruff check .        # lint
uv run ruff format --check .  # format check
uv build                   # sdist + wheel
```

A plain `uv sync` installs only the Tier-1 grammars; Tier-2 and tokenizer
tests skip on a default install (same as a user install). CI runs across
{ubuntu, macos, windows} × {3.10, 3.13}.

## Ground rules

- Open an issue before starting a non-trivial change so we can align on scope
  and avoid duplicated work.
- Keep PRs focused: one conceptual change per PR.
- `ruff` is the linter/formatter and `pytest` the test runner; CI will fail if
  either does.
- **Regenerate dekko's own map before every commit** and commit the
  refreshed `.dekko/MAP.md` / `.dekko/map.json` alongside the change:
  run `uv run dekko map` after your last source edit, then
  `git add .dekko/`. This repo tracks its own map (dogfooding), and a
  checkout's map must match its source — `uv run dekko status` on a
  fresh clone should report the map fresh. The `dekko-map` pre-commit
  hook regenerates a stale map but does not stage the result, so
  `pre-commit install` alone does not cover this.
- Tests live in `tests/`, mirroring `src/dekko/`'s subpackages
  (`core/`, `render/`, `analysis/`, `daemon/`, `integrations/`,
  `storage/`) where a test maps cleanly to one moved module;
  cross-cutting/behavioral tests and top-level-module tests stay flat
  under `tests/`. Fixtures (tiny sample-language files) live in
  `tests/fixtures/`.
  `test-repos/` holds real, unmodified open-source repos used for
  manual/agent evaluation of dekko itself, not pytest fixtures — see
  `test-repos/TESTING-GUIDE.md`.

## Commit messages

Every commit subject starts with a [Conventional Commits](https://www.conventionalcommits.org/)
prefix — `prefix: description`, lowercase prefix, present tense, no
trailing period. History and release notes lean on these to tell a
commit's shape apart at a glance, so pick the one that actually
describes the change, not the one that sounds most important:

| Prefix | Meaning & purpose | Example |
|---|---|---|
| `feat` | A new feature for the user. | `feat: add email verification` |
| `fix` | A bug fix. | `fix: resolve crash on logout` |
| `docs` | Documentation-only changes. | `docs: update setup guide in README` |
| `style` | Formatting, whitespace, missing semicolons — no logic change. | `style: fix indentation in auth controller` |
| `refactor` | Rewriting code with no behavior change — not a fix, not a feature. | `refactor: simplify database connection loop` |
| `perf` | A change whose point is improved performance. | `perf: compress homepage images` |
| `test` | Adding or correcting tests, no production-code change. | `test: add unit test for payment gateway` |
| `chore` | Build tasks, dependency bumps, tooling config. | `chore: upgrade lodash package version` |
| `build` | Changes to the build system or external dependencies. | `build: migrate from npm to yarn` |
| `ci` | Changes to CI/CD configuration and scripts. | `ci: add linting workflow to GitHub Actions` |
| `revert` | Reverts a previous commit. | `revert: undo previous payment fix` |

A commit that mixes categories (a fix bundled with an unrelated
refactor) should usually be split into separate commits instead — see
"Keep PRs focused" above. When a commit genuinely does two things at
once, prefix by its dominant effect on a user: `fix` beats `refactor`,
`feat` beats `docs`.

## Versioning

Version numbers follow SemVer's `MAJOR.MINOR.PATCH` shape
(`pyproject.toml`'s `version`), each number with its own specific
trigger — see "Releasing" below for the mechanics of applying a bump:

- **MAJOR — maintainer-only.** Bumped only by deliberate maintainer
  decision, never as a side effect of routine work: a breaking change
  to the CLI/MCP surface (a removed or renamed command, flag, or MCP
  tool), a `map.json` schema change that isn't backward-readable,
  dropping a supported Python version, or a milestone promotion (e.g.
  0.x → 1.0.0). Requires a `### Breaking` CHANGELOG section describing
  what breaks and how to migrate.
- **MINOR — closes out a body of work.** Bumped when either applies:
  1. **A testing round's fix cycle is fully closed** — every track
     designed against that round's eval findings is implemented,
     verified, and merged to `develop` (see
     `test-repos/reports/README.md` and `test-repos/TESTING-GUIDE.md`
     for what "closed" means for a round). Individual fixes within
     the round land as PATCH bumps under the *current* minor version
     as each track is built; the MINOR bump happens once, at the
     point the round is verified closed, so the *next* round's fixes
     start on the new minor line at `.1` (see "Testing rounds and the
     version line" below).
  2. **A genuinely new top-level capability ships** outside of a
     round: a new CLI subcommand, a new MCP tool, or a new top-level
     output format — not a new flag or option on an existing command
     (that's PATCH; see below).
  Resets PATCH to 0.
- **PATCH — everything else.** Every individual fix, perf change, doc
  fix, refactor, dependency bump, or small enhancement to an existing
  command (a new flag, an expanded output section, a new detection
  rule inside an existing analysis) bumps PATCH. This is the workhorse
  number for day-to-day commits — most commits bump this and nothing
  else. One exception: a `docs:` commit that only changes process or
  contributor docs (this file, `CLAUDE.md`, benchmark write-ups) and
  ships no code or user-facing docs takes no bump, so it can't eat a
  round's reserved patch numbers.

### Testing rounds and the version line

Evaluation rounds are numbered `<MAJOR>.<N>`, and the round counter
**resets at every MAJOR release**: the first round of the 1.x series
is round 1.1, the first round after 2.0.0 is round 2.1. (Round 1.1 is
the former round 34; pre-1.0 rounds 01-33 keep their old numbers.)

Round numbers and MINOR versions move together:

- **One fix track = one commit = one PATCH bump.** Each design in a
  round's fix folder (e.g. `.features/fixes/v1/round1.1/01-*.md`) is
  implemented, tested, and committed on its own, with its own PATCH
  bump in that same commit. No batching two tracks into one commit,
  no track split across commits. (A design doc that explicitly
  batches several one-liners into one track, like a "small fixes"
  track, is still one track: one commit, one bump.)
- Round `X.N`'s fixes land as `X.(N-1).1`, `X.(N-1).2`, ... in the
  order they are committed (not necessarily the design docs' file
  numbering). Round 1.1's first fix is **1.0.1**, its second
  **1.0.2**.
- When every track in the round is implemented and verified, the
  round is closed with the MINOR release **`X.N.0`**: round 1.1
  closes as **1.1.0**, round 1.2 as **1.2.0**. That release goes
  through the normal "Releasing" steps below (develop → main PR, tag).
- If a capability MINOR (trigger 2 above) ships mid-round, it takes
  the next MINOR number and the round closes on the one after it; say
  so in the round's README section. Otherwise round `X.N` always
  closes as `X.N.0`.
- A round whose findings need no fixes gets no MINOR bump. The round
  counter still advances for the next evaluation, and the next round's
  README section says the version line skipped it.

Where things go (both directories are local and gitignored):

- Eval reports: `test-repos/reports/v<MAJOR>/round<MAJOR>.<N>/`,
  indexed in `test-repos/reports/README.md`.
- Fix designs: `.features/fixes/v<MAJOR>/round<MAJOR>.<N>/`, same
  round number as the reports that motivated them.
- Everything from before 1.0.0 lives under `v0/` in both places.

When in doubt between MINOR and PATCH for a given change, default to
PATCH — it's the more reversible mistake, and the CHANGELOG narrative
can still describe a string of PATCH bumps as one body of work even
after the fact.

## Releasing

Releases are cut by pushing a `v*` tag to `main`; `.github/workflows/release.yml`
builds and publishes to PyPI via [trusted publishing](https://docs.pypi.org/trusted-publishers/).
The trusted publisher for `aahlijia/dekko` must be configured on PyPI first.
See [CHANGELOG.md](CHANGELOG.md) for per-version history.

The version is declared in four places that must all agree
(`pyproject.toml`, `plugin.json`, `marketplace.json`, `uv.lock`) --
`tests/test_version.py::test_declared_versions_agree` checks this.
Bump the version with `scripts/sync_plugin_version.py <new-version>`
rather than hand-editing `pyproject.toml`; it updates `pyproject.toml`,
both plugin manifests, and `uv.lock` together. If `pyproject.toml`
still ends up hand-edited (e.g. a merge conflict resolution), the
`sync-plugin-version` pre-commit hook re-syncs the plugin manifests to
it automatically on commit, the same way the existing `uv-lock` hook
already keeps `uv.lock` in sync. `release.yml`'s `build` job is the
last line of defense: it fails the release if the plugin manifests
don't match the tag being released.

## Reporting issues

Please include: dekko version (`dekko --version`), your OS, and a minimal
reproducer if possible. For bugs in parsing or call resolution, a small fixture
repo helps enormously.
