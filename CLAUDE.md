# CLAUDE.md

Project context for Claude Code (and other agents) working in this repo.

## Getting oriented

`dekko outline src/dekko/<subpackage>/<file>.py` or
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
- `.dekko/MAP.md` / `.dekko/map.json` at the repo root are dekko's own
  generated map of itself; the map is git-ignored by default but this
  repo tracks its own for dogfooding. **Always regenerate the map before
  committing and commit the refreshed `.dekko/` files with the change:**
  run `dekko map` (or `uv run dekko map`) after your last source or doc
  edit, then `git add .dekko/`. A checkout's map must match its source
  and its Markdown/RST docs (doc links live in the map), so a commit of
  either with a stale map is a bug. The `dekko-map` pre-commit
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
  top-level capability, patch is everything else; each fix track from
  an evaluation round is its own commit with its own patch bump),
  releases cut by pushing a `v*` tag.
