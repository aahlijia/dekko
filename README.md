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
lidar --map                   # map the current directory
lidar --map /path/to/repo     # map another directory
lidar --map . src             # restrict the map to a subtree
lidar --map . --output docs/  # write MAP.md + map.json into docs/
lidar --map . --output codemap.md   # custom file (json: codemap.json)
lidar --claude-install        # install the Claude Code plugin
lidar --version
```

| Flag | Meaning |
| --- | --- |
| `--map [DIR]` | Map `DIR` (defaults to the current directory) |
| `--output PATH` | Markdown file, or a directory for both outputs |
| `--json PATH` | Explicit map.json path |
| `--no-json` | Skip map.json |
| `--exclude GLOB` | Extra skip pattern (repeatable) |
| `--max-file-size BYTES` | Skip files larger than this (default 1 MB) |
| `--quiet` | Suppress the summary |
| `--claude-install` | Register the bundled plugin with Claude Code |
| `--version` | Print the version |

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
