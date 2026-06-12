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
lidar map --full              # ignore the .lidar cache, re-parse everything
lidar query callers resolve   # who calls resolve?
lidar query callees main      # what does main call?
lidar query symbol cli.py:run_map   # signature card for one symbol
lidar query file walker.py    # symbols defined in a file
lidar context run_map --budget 1500 # minimal context pack for an edit
lidar diff                    # symbols changed since the map's commit
lidar diff main               # ...or since any git rev, with callers
lidar unused                  # symbols nothing calls (dead-code leads)
lidar stats                   # hotspots, largest files, language mix
lidar export --format mermaid # render the call graph (mermaid|dot)
lidar status                  # is map.json still fresh? (exit 0/1)
lidar serve --mcp             # expose the map to agents over MCP (stdio)
lidar --claude-install        # install the Claude Code plugin
lidar --mcp-install           # register the MCP server (claude mcp add)
lidar --version
```

| Command | Meaning |
| --- | --- |
| `map [DIR] [SUBPATH]` | Generate MAP.md + map.json (`--if-stale` skips when fresh; `--full` forces a cold rebuild; `--output`, `--json`, `--no-json`, `--exclude`, `--max-file-size`, `--quiet`) |
| `query ACTION TARGET` | `callers`, `callees`, `symbol`, or `file` lookups against map.json |
| `context TARGET` | Signatures of a symbol's neighborhood (`--hops N`, `--budget TOKENS`) |
| `diff [REV]` | Symbols added/removed/changed since a git rev (default: the map's commit), each with impacted callers (`--limit`, `--json`) |
| `unused` | Symbols with no inbound calls, minus roots (`--roots GLOB`, `--limit`, `--json`); exit 1 when any are found |
| `stats` | Fan-in/out hotspots, largest files, language mix (`--top`, `--json`) |
| `export` | Call graph as `--format mermaid\|dot`, `--scope symbol\|file`, capped by `--max-nodes` |
| `status` | Freshness report from the provenance stamp in map.json |
| `serve --mcp` | Hand-rolled MCP server (stdio) exposing the read surface as agent tools (`--root`, `--no-regen`) |

Symbol targets accept a bare `name`, `Class.method`, or the qualified
`file.py:name` / `file.py:Class.method` forms; ambiguous names list
their candidates instead of guessing. The read commands (`query`,
`context`, `unused`, `stats`, `export`) regenerate a stale map
automatically — pass `--no-regen` to fail instead, and `--json`
anywhere for structured output. The legacy flags `--map [DIR]
[SUBPATH]`, `--claude-install`, `--mcp-install`, and `--version` keep
working as aliases.

`map` keeps a per-file extraction cache under `.lidar/` (added to your
`.gitignore` automatically), so re-mapping only re-parses files whose
contents changed; `--full` ignores it.

Exit codes: `0` success/fresh/no-diff, `1` failure, stale (`status`),
differences found (`diff`), or unused symbols found (`unused`); `2`
usage error, `3` target not found, `4` ambiguous target, `5` stale map
with `--no-regen`.

`unused` is call-graph based, so it lists *leads*, not verdicts: a
symbol reached only via subclassing, type annotations, dynamic dispatch,
or a callback registered by reference can still surface. It already
treats `main`, test files, decorated/annotated symbols, the language's
public surface (Rust `pub`, Go capitals, Java `public`, JS/TS `export`),
Python dunders, and `__init__.py` re-exports as roots; add your own with
`--roots`.

## Plugin usage

```
/map           # map the whole repository
/map src/      # map a subtree only
```

The plugin runs the installed `lidar` CLI, so install the package first
(see above).

## MCP server

`lidar serve --mcp` speaks the Model Context Protocol over stdio as
newline-delimited JSON-RPC 2.0 — **no SDK dependency**. It lets an agent
answer "who calls X?" with a tool call instead of reading MAP.md. The
read commands map to six tools:

| Tool | Backs |
| --- | --- |
| `query_symbol` | `query symbol` |
| `get_callers` / `get_callees` | `query callers` / `callees` |
| `get_context_pack` | `context` (`hops`, `budget`) |
| `map_status` | `status` |
| `refresh_map` | `map` (`full` for a cold rebuild) |

Reads auto-regenerate a stale map (pass `--no-regen` to disable), and
each tool accepts an optional `root` (defaults to the server's working
directory).

The plugin ships an `.mcp.json` pointing at `lidar serve --mcp` with
`cwd` set to `${CLAUDE_PROJECT_DIR}`, so `lidar --claude-install` wires
the server automatically. For a non-plugin setup, `lidar --mcp-install`
runs `claude mcp add lidar -- lidar serve --mcp`.

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
publisher for `aahlijia/lidar` on PyPI first. See
[CHANGELOG.md](CHANGELOG.md) for the per-version history.
