# lidar

A Claude Code plugin that adds a `/map` command. Like its namesake, it
scans the terrain programmatically — no model tokens are spent parsing —
sweeping the repository and writing:

- **`MAP.md`** — every code file, every function/method, parameters with
  types (when declared), return types, and relational call links: each
  function lists what it **calls** and what it is **called by**.
- **`map.json`** — the full symbol/call graph in machine-readable form,
  including external and ambiguous calls omitted from MAP.md.

## Installation

### Via Claude Code (recommended)

```sh
claude plugin marketplace add github:aahlijia/lidar
claude plugin install lidar@lidar
```

Restart Claude Code after installing.

### From a local clone

```sh
git clone https://github.com/aahlijia/lidar
cd lidar
./install.sh
```

`install.sh` checks for `uv`, registers the marketplace, and installs the
plugin in one step.

## Requirements

- [`uv`](https://docs.astral.sh/uv/) on `PATH` — the tool declares its own
  dependencies inline (PEP 723), so there is nothing else to install.

## Usage

```
/map           # map the whole repository
/map src/      # map a subtree only
```

Or run the tool directly, outside Claude Code:

```sh
uv run tool/lidar.py --root /path/to/repo
```

```
usage: lidar [--root PATH] [SUBPATH] [--output MAP.md]
             [--json map.json] [--no-json] [--exclude GLOB ...]
             [--max-file-size BYTES] [--quiet]
```

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
uv run pytest               # test suite
uv run ruff check tool/ tests/   # lint
uv run ruff format --check tool/ tests/
```
