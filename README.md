# lidar-map

A code-map generator with a CLI and a Claude Code `/map` plugin —
installed as `lidar-map`, run as `lidar`. Like its namesake, it scans the terrain programmatically — no model tokens
are spent parsing — sweeping the repository and writing:

- **`MAP.md`** — every code file, every function/method, parameters with
  types (when declared), return types, and relational call links: each
  function lists what it **calls** and what it is **called by**.
- **`map.json`** — the full symbol/call graph in machine-readable form,
  including external and ambiguous calls omitted from MAP.md.

## Installation

Install the `lidar-map` package (the CLI command is `lidar`):

```sh
uv tool install lidar-map     # or: pip install lidar-map / pipx install lidar-map
```

Then, to add the `/map` command to Claude Code:

```sh
lidar --claude-install
```

Restart Claude Code after installing.

### From a local clone

```sh
git clone https://github.com/aahlijia/lidar
cd lidar
./install.sh
```

`install.sh` installs the CLI with `uv tool install` and registers the
plugin in one step.

## CLI usage

```sh
lidar map                     # map the current directory
lidar map /path/to/repo       # map another directory
lidar map . src               # restrict the map to a subtree
lidar map --if-stale          # regenerate only when sources changed
lidar query callers resolve   # who calls resolve?
lidar query callees main      # what does main call?
lidar query symbol cli.py:run_map   # signature card for one symbol
lidar query file walker.py    # symbols defined in a file
lidar context run_map --budget 1500 # minimal context pack for an edit
lidar status                  # is map.json still fresh? (exit 0/1)
lidar --claude-install        # install the Claude Code plugin
lidar --version
```

| Command | Meaning |
| --- | --- |
| `map [DIR] [SUBPATH]` | Generate MAP.md + map.json (`--if-stale` skips when fresh; `--output`, `--json`, `--no-json`, `--exclude`, `--max-file-size`, `--quiet`) |
| `query ACTION TARGET` | `callers`, `callees`, `symbol`, or `file` lookups against map.json |
| `context TARGET` | Signatures of a symbol's neighborhood (`--hops N`, `--budget TOKENS`) |
| `status` | Freshness report from the provenance stamp in map.json |

Symbol targets accept a bare `name`, `Class.method`, or the qualified
`file.py:name` / `file.py:Class.method` forms; ambiguous names list
their candidates instead of guessing. Read commands (`query`,
`context`) regenerate a stale map automatically — pass `--no-regen` to
fail instead, and `--json` anywhere for structured output. The legacy
flags `--map [DIR] [SUBPATH]`, `--claude-install`, and `--version`
keep working as aliases.

Exit codes: `0` success/fresh, `1` failure or stale (`status`),
`2` usage error, `3` target not found, `4` ambiguous target,
`5` stale map with `--no-regen`.

## Plugin usage

```
/map           # map the whole repository
/map src/      # map a subtree only
```

The plugin runs the installed `lidar` CLI, so install the package first
(see above).

## Language support

Parsing is done with [tree-sitter](https://tree-sitter.github.io/) via
`tree-sitter-language-pack`.

- **Tier 1 — full fidelity** (dedicated queries; typed params and return
  types where the language declares them): Python, Rust, C, C++,
  JavaScript, TypeScript (+ TSX), Go, Java.
- **Tier 2 — generic fallback** (function names, parameter text, and call
  links): every other grammar in the language pack — Ruby, PHP, C#,
  Kotlin, Swift, Lua, and many more.

## How call resolution works

Best-effort static resolution, in order: same class/container → same file
→ imported names → unique repo-wide name match. Calls that stay ambiguous
are marked as such rather than guessed; calls to stdlib/third-party code
are recorded in `map.json` only.

## Development

```sh
uv run pytest                    # test suite
uv run ruff check .              # lint
uv run ruff format --check .
uv build                         # sdist + wheel into dist/
```

Releases: pushing a `v*` tag builds and publishes to PyPI via trusted
publishing (`.github/workflows/release.yml`); configure the trusted
publisher for `aahlijia/lidar` on PyPI first.
