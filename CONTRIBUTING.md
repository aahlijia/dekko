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
- Tests live in `tests/`, mirroring `src/dekko/`'s subpackages
  (`core/`, `render/`, `analysis/`, `daemon/`, `integrations/`,
  `storage/`) where a test maps cleanly to one moved module;
  cross-cutting/behavioral tests and top-level-module tests stay flat
  under `tests/`. Fixtures (tiny sample-language files) live in
  `tests/fixtures/`.
  `test-repos/` holds real, unmodified open-source repos used for
  manual/agent evaluation of dekko itself, not pytest fixtures — see
  `test-repos/TESTING-GUIDE.md`.

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
