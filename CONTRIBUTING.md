# Contributing

Thanks for taking the time to contribute! 🎉 By participating in this
project, you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Quick start (dev)

```sh
git clone https://github.com/aahlijia/dekko.git
cd dekko
uv sync --extra all        # include Tier-2 grammars + tokenizer
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
- Tests live in `tests/`. Fixtures (sample repos) live in `test-repos/`.

## Releasing

Releases are cut by pushing a `v*` tag to `main`; `.github/workflows/release.yml`
builds and publishes to PyPI via [trusted publishing](https://docs.pypi.org/trusted-publishers/).
The trusted publisher for `aahlijia/dekko` must be configured on PyPI first.
See [CHANGELOG.md](CHANGELOG.md) for per-version history.

## Reporting issues

Please include: dekko version (`dekko --version`), your OS, and a minimal
reproducer if possible. For bugs in parsing or call resolution, a small fixture
repo helps enormously.
