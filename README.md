# dekko

A code-map generator with a CLI and a Claude Code `/map` plugin —
installed and run as `dekko`. It scans the repo programmatically — no model tokens
are spent parsing — sweeping the repository and writing two files (by
default into a `.dekko/` directory at the repo root):

- **`MAP.md`** — a human-readable map: an `## Overview` (per-directory
  rollup, an embedded `mermaid` architecture diagram, load-bearing and
  orchestrator rankings, largest files, and churn × fan-in hotspots)
  followed by every code file, every function/method, parameters with
  types (when declared), return types, and relational call links: each
  function lists what it **calls** and what it is **called by**. Large
  repos shard into per-directory `map/` pages automatically.
- **`map.json`** — the full symbol/call graph in machine-readable form,
  including external and ambiguous calls omitted from MAP.md.

A standalone, dependency-free `dekko export --format html` renders the
same map as an interactive single-file browser (tree, search, clickable
callers/callees) for readers who never install dekko.

For agents, dekko adds a token-aware read surface on top of the map:
`dekko lean` emits a budget-capped navigation map of the whole repo (the
middle ground between the ~400-token `summary` and a full MAP.md),
`outline` shows a file's shape without its bodies, `workset` bundles
everything relevant to a change, and every list command takes a
`--budget`. The same surface is exposed over MCP via `dekko serve`.

## Installation

```sh
uv tool install dekko     # or: pip install dekko / pipx install dekko
```

The default install bundles **nine Tier-1 languages** (Python, Rust, C,
C++, JavaScript, TypeScript/TSX, Go, Java) as individual grammar
packages, so dekko parses them **fully offline** — no grammar download
at runtime. For the ~55 additional [Tier-2](#language-support)
languages, add the extra:

```sh
pip install dekko[all]    # adds the tree-sitter grammar pack
```

Tested on macOS and Linux; Windows is best-effort (exercised in CI).

Then, to add the `/map` command to Claude Code:

```sh
dekko --claude-install
```

Restart Claude Code after installing.

### From a local clone

```sh
git clone https://github.com/aahlijia/dekko.git
cd dekko
./install.sh
```

`install.sh` installs the CLI with `uv tool install` and registers the
plugin in one step.

### Uninstall

Remove the Claude Code plugin, then uninstall the CLI:

```sh
dekko --claude-uninstall   # remove the /map plugin and its marketplace
dekko --mcp-uninstall       # only if you ran --mcp-install
uv tool uninstall dekko     # or: pip uninstall dekko / pipx uninstall dekko
```

`--claude-uninstall` reverses `--claude-install`, undoing the bundled
plugin (which carries the MCP server). It does not touch a standalone MCP
registration added by `--mcp-install` — drop that with `--mcp-uninstall`
(i.e. `claude mcp remove dekko`). To do the removals by hand instead:

```sh
claude plugin uninstall dekko@dekko        # remove the /map plugin
claude plugin marketplace remove dekko     # drop the bundled marketplace
claude mcp remove dekko                     # remove a standalone MCP server
```

The `.dekko/` cache directory in any mapped repo is safe to delete by
hand; it is already git-ignored.

## CLI usage

```sh
dekko map                     # map the current directory
dekko map /path/to/repo       # map another directory
dekko map . src               # restrict the map to a subtree
dekko map --if-stale          # regenerate only when sources changed
dekko map --full              # ignore the .dekko cache, re-parse everything
dekko map --jobs 0            # parallel extraction (0 = all cores)
dekko map --shard always      # force per-directory map/ pages (auto|always|never)
dekko map --order fan-in      # order sections by fan-in (path|name|fan-in)
dekko query callers resolve --sites # call sites that call resolve
dekko query callees main      # what does main call?
dekko query symbol cli.py:run_map   # signature card (with doc + notes)
dekko query uses Path         # who references the external name Path?
dekko query file walker.py    # symbols defined in a file
dekko query callers main --no-tests # drop test-file results
dekko context run_map --budget 1500 # minimal context pack for an edit
dekko context run_map --with-source # ...with the body + call sites inlined
dekko outline server.py       # a file's signatures + docs, no bodies
dekko trace main run_map      # shortest call path(s) between two symbols
dekko summary                 # ~40-line repo digest (dirs, hotspots)
dekko lean                    # budget-capped whole-repo navigation map
dekko lean --task "fix auth"  # rank the map by relevance to a task
dekko lean --dense            # terser: signatures only on the most central
dekko lean --output .dekko/LEAN.md  # write it (gitignored; commit via whitelist)
dekko diff                    # symbols changed since the map's commit
dekko affected                # test files impacted by your changes
dekko affected main           # ...vs any git rev
dekko workset                 # one budgeted bundle for your current change
dekko unused                  # symbols nothing calls (dead-code leads)
dekko stats                   # hotspots, largest files, language mix
dekko orient                  # opt-in session digest + steering preamble
dekko ledger                  # what this session already has in context
dekko hooks install           # enable the opt-in push hooks (settings.json)
dekko note add cli.py:run_map "why" # anchor a durable note to a symbol
dekko note list --orphaned    # notes whose symbol moved
dekko export --format mermaid # render the call graph (mermaid|dot)
dekko export --format html    # interactive single-file browser (.dekko/map.html)
dekko status                  # is map.json still fresh? (exit 0/1)
dekko serve --mcp             # expose the map to agents over MCP (stdio)
dekko --claude-install        # install the Claude Code plugin
dekko --mcp-install           # register the MCP server (claude mcp add)
dekko --version
```

| Command | Meaning |
| --- | --- |
| `map [DIR] [SUBPATH]` | Generate MAP.md + map.json (`--if-stale` skips when fresh; `--full` forces a cold rebuild; `--jobs N` parallelizes extraction, `0` = all cores; `--shard auto\|always\|never` splits large maps into `map/` pages; `--order path\|name\|fan-in` orders sections; `--output`, `--json`, `--no-json`, `--exclude`, `--max-file-size`, `--quiet`) |
| `query ACTION TARGET` | `callers`, `callees`, `symbol`, `file`, or `uses` lookups; `--sites` for per-call-site rows, `--no-tests` to drop test code, `--notes/--no-notes` |
| `context TARGET` | A symbol's neighborhood with docs and notes (`--hops N`, `--budget TOKENS`); `--with-source` inlines the body and hop-1 call sites; `--task "…"` keeps the most task-relevant neighbors under a tight budget |
| `outline PATH` | A file's (or directory's) structure — signatures, doc first lines, line numbers, no bodies — at ~1/10 the read cost (`--budget`, `--limit`, `--json`) |
| `trace FROM TO` | Shortest call path(s) from one symbol to another (`--max-paths K`, `--json`); no path is a clean exit `1` |
| `summary` | ~40-line repo digest: counts, per-directory rollup with coupling and purpose, hotspots, entry points, parse errors (`--json`) |
| `lean` | A budget-capped whole-repo navigation map: every file + purpose, symbols (signatures on the most central, names on the rest), and module edges, shed to fit a token cap (`--budget`, `--output PATH`, `--json`). `--task "…"` ranks by relevance; `--dense` keeps signatures only on the most central. Denser than `summary`, far cheaper than MAP.md |
| `workset [REV]` | One budgeted bundle for a change: impacted tests, touched-file outlines, and packs for the most central touched symbols (`--symbol NAME`, `--budget`, `--packs N`, `--task "…"`, `--json`) |
| `diff [REV]` | Symbols added/removed/changed since a git rev (default: the map's commit), each with impacted callers (`--limit`, `--json`) |
| `affected [REV]` | Test files impacted by changes — reverse call-graph reachability plus an import-edge fallback; prints a `pytest …` line (`--limit`, `--json`) |
| `unused` | Symbols with no inbound calls, minus roots (`--roots GLOB`, `--limit`, `--json`); exit 1 when any are found |
| `stats` | Fan-in/out hotspots, largest files, language mix (`--top`, `--json`) |
| `orient [--read PATH]` | Opt-in orientation: a budgeted session digest with steering, or a pre-read nudge to outline a large file first (never blocks) |
| `ledger` | What the current session already loaded into context (files read, symbols seen, tokens consumed), projected from the session transcript (`--transcript PATH`, `--session ID`, `--budget`, `--json`) |
| `hooks install\|uninstall\|run` | Manage the opt-in push hooks in `.claude/settings.json` (`install --enable EVENT` for `session-start`, `prompt-submit`, `pre-read`) |
| `note add\|list\|rm` | Durable symbol-anchored notes in `.dekko/notes.json` (`list --orphaned` finds notes whose symbol moved) |
| `export` | Call graph as `--format mermaid\|dot` (`--scope symbol\|file`, capped by `--max-nodes`) or `--format html` (a self-contained interactive browser); `--output PATH` writes a file instead of stdout (html defaults to `.dekko/map.html`) |
| `status` | Freshness report from the provenance stamp in map.json |
| `serve --mcp` | Hand-rolled MCP server (stdio) exposing the read surface as agent tools (`--root`, `--no-regen`) |

Symbol targets accept a bare `name`, `Class.method`, or the qualified
`file.py:name` / `file.py:Class.method` forms; ambiguous names list
their candidates instead of guessing. The read commands (`query`,
`outline`, `context`, `trace`, `summary`, `lean`, `unused`, `stats`,
`export`) regenerate a stale map automatically — pass `--no-regen` to
fail instead, and `--json` anywhere for structured output. The legacy flags `--map [DIR]
[SUBPATH]`, `--claude-install`, `--mcp-install`, and `--version` keep
working as aliases.

### Token budgets

The `--budget` caps and the lean map's self-scaling cap count tokens
with a `~4 chars/token` estimate by default — no dependency, and
byte-identical output on every machine. For more accurate counts
(chars/4 systematically under-counts code), install the optional
tokenizer extra:

```sh
pip install dekko[tokenizer]      # adds tiktoken (o200k_base)
```

dekko then uses it automatically. Because real token counts depend on
the installed tokenizer, output becomes environment-specific when the
extra is present; set `DEKKO_TOKENIZER=chars4` to force the estimate
back on (e.g. for a committed `.dekko/LEAN.md` that should diff cleanly
across contributors).

`map` writes `MAP.md` and `map.json` into a `.dekko/` directory at the
repository root — override the location with `--output` — alongside a
per-file extraction cache. Large maps additionally shard into
`.dekko/map/<dir>.md` pages, and `dekko export --format html` writes
`.dekko/map.html`. An inner `.dekko/.gitignore` ignores all of this
generated output (maps, pages, html, cache) while keeping `notes.json`
(your committed symbol annotations) tracked; your repository
`.gitignore` is left untouched. The cache lets re-mapping re-parse only
files whose contents changed (`--full` ignores it) and is tagged with
the `dekko` version, so upgrading re-parses everything once to pick up
extractor changes.

To **commit a map** into your repo, opt in by whitelisting it in
`.dekko/.gitignore` — e.g. add `!MAP.md` and `!map.json` (and `!map/`
plus `!map/*.md` if you shard). Generated maps are ignored by default
because they would otherwise churn on every regeneration.

Exit codes: `0` success/fresh/no-diff/no-impact, `1` failure, stale
(`status`), differences found (`diff`), impacted tests found
(`affected`), unused symbols found (`unused`), or no call path
(`trace`); `2` usage error or bad git rev, `3` target not found, `4`
ambiguous target, `5` stale map with `--no-regen`.

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

The plugin runs the installed `dekko` CLI, so install the package first
(see above).

### Keeping the map fresh automatically (optional)

Read commands already regenerate a stale map on demand, and the
freshness check is cheap — unchanged files are skipped by an
`(mtime, size)` comparison, so only edited files are re-hashed. If you
want the map refreshed the moment you edit a file, add a Claude Code
`PostToolUse` hook in your `settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          { "type": "command", "command": "dekko map --if-stale \"${CLAUDE_PROJECT_DIR}\"" }
        ]
      }
    ]
  }
}
```

The incremental cache makes each refresh re-parse only the changed
file. This is opt-in rather than bundled with the plugin so you control
when dekko runs.

### Proactive orientation (opt-in)

By default dekko is a *pull* tool — it helps when the agent asks. The
`dekko-orient` skill (bundled, no setup) steers an agent to orient with
`dekko summary` and to prefer `outline` / `workset` / `query` over
reading whole files. If you want that orientation to fire deterministically,
add hooks to your `settings.json`.

`dekko orient` prints a short steering preamble plus the `summary`
digest — a ready-made `SessionStart` payload:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "dekko orient --root \"${CLAUDE_PROJECT_DIR}\"" }
        ]
      }
    ]
  }
}
```

Add `--no-regen` to that command to skip refreshing a stale map at
session start (faster, but the digest may be out of date).

`dekko orient --read <file>` prints a one-line nudge to `outline` a file
first **only when the file is large** (and nothing otherwise — it never
blocks). Wiring it to `PreToolUse` on `Read` is **experimental**: the
hook output-to-context behavior varies by harness and is not verified
here. The harness-specific path extraction stays in the hook (via `jq`),
not in dekko:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read",
        "hooks": [
          { "type": "command", "command": "jq -r '.tool_input.file_path' | xargs -r dekko orient --root \"${CLAUDE_PROJECT_DIR}\" --read" }
        ]
      }
    ]
  }
}
```

Both hooks are opt-in; core behavior is unchanged when neither is set.

### Active Context Layer (opt-in)

`dekko orient` above is the manual, one-shot push. The **Active Context
Layer** is the fuller version: a set of opt-in Claude Code hooks that keep
an agent oriented, point it at the code relevant to *this* task, and stop
it re-spending tokens on context it already holds. It is built from three
pieces that also work on their own.

**Task-aware ranking (`--task`).** `lean`, `workset`, and `context` accept
`--task "…"`; the description is blended with structural centrality (and
your working diff) so the most relevant code survives a tight budget:

```sh
dekko lean --task "refactor the token budgeting"   # map ranked for the task
dekko context fit_to_budget --task "add a floor" --budget 600
```

Ranking is lexical and dependency-free; with no `--task`, output is
byte-for-byte unchanged. `dekko lean --dense` is an orthogonal lever — it
keeps full signatures only on the most central symbols (names for the
rest), for the tersest whole-repo map.

**Session ledger (`dekko ledger`).** Projects the Claude Code session
transcript into "what is already in context" — files read, symbols seen,
and the real tokens consumed (read straight from the transcript's usage):

```sh
dekko ledger --budget 200000        # …and how much budget is left
```

dekko persists no session state of its own; the transcript is the source
of truth, so the ledger also sees files the agent read *directly*.

**Push hooks (`dekko hooks install`).** Wires the above into project
`.claude/settings.json` — opt-in, per-project, nothing active until you
run it:

```sh
dekko hooks install                                   # SessionStart only
dekko hooks install --enable session-start --enable prompt-submit
dekko hooks uninstall                                 # remove dekko's entries
```

- **`session-start`** injects a steering preamble + a budget-capped lean
  map, so the first turn already holds a navigation map.
- **`prompt-submit`** points at the files most relevant to the new prompt
  that aren't already in context (relevance ⋈ ledger dedup), the list
  tightening as the session's budget fills.
- **`pre-read`** advises outlining a large file first — non-blocking
  (`permissionDecision: "defer"`; it never denies a read).

Every hook is fail-silent: a missing map, an empty signal, or any error
yields no output and a clean exit, so a hook can never break or hijack a
session. `dekko hooks install` writes direct `dekko hooks run <event>`
commands (no shell substitution), and `uninstall` removes only dekko's
entries, leaving your other hooks untouched.

## MCP server

`dekko serve --mcp` speaks the Model Context Protocol over stdio as
newline-delimited JSON-RPC 2.0 — **no SDK dependency**. It lets an agent
answer "who calls X?" with a tool call instead of reading MAP.md. The
read surface maps to eighteen tools:

| Tool | Backs |
| --- | --- |
| `query_symbol` | `query symbol` (signature, doc, fan-in/out, notes) |
| `get_callers` / `get_callees` | `query callers` / `callees` (`sites`) |
| `find_usages` | `query uses` (references to an external name) |
| `get_context_pack` | `context` (`hops`, `budget`, `with_source`, `task`) |
| `outline` | `outline` (`target`, `budget`, `limit`) |
| `trace_path` | `trace` (`from`, `to`, `max_paths`) |
| `affected` → `impacted_tests` | `affected` (`rev`) |
| `workset` | `workset` (`rev` or `symbol`, `budget`, `packs`, `task`) |
| `summary` | `summary` |
| `lean` | `lean` (`budget`, `task`, `dense`) — budget-capped whole-repo navigation map |
| `find_unused` | `unused` (`roots`, `limit`) |
| `stats` | `stats` (`top`) |
| `ledger` | `ledger` — what the session already holds in context |
| `add_note` / `list_notes` | `note add` / `note list` |
| `map_status` | `status` |
| `refresh_map` | `map` (`full` for a cold rebuild) |

It also serves one MCP **resource**, `dekko://summary`, for clients that
attach resources as context. Reads auto-regenerate a stale map (pass
`--no-regen` to disable), and each tool accepts an optional `root`
(defaults to the server's working directory).

The plugin ships an `.mcp.json` pointing at `dekko serve --mcp` with
`cwd` set to `${CLAUDE_PROJECT_DIR}`, so `dekko --claude-install` wires
the server automatically. For a non-plugin setup, `dekko --mcp-install`
runs `claude mcp add dekko -- dekko serve --mcp`.

## Notes

`dekko note` anchors durable annotations to a symbol by id, stored in
`.dekko/notes.json`. Notes are meant to be **committed** — the inner
`.dekko/.gitignore` keeps that one file tracked while ignoring the
generated map and cache — so rationale travels with the code and shows
up inline in `dekko query symbol` and `dekko context`.

```sh
dekko note add resolver.py:resolve "ambiguous calls are marked, never guessed"
dekko note list resolver.py:resolve
dekko note list --orphaned   # notes whose symbol was renamed or moved
dekko note rm resolver.py:resolve 1
```

Because a note's key is `path::Qualified.name`, renaming or moving a
symbol orphans its notes; `note list --orphaned` finds them so you can
re-anchor (`note add` the new target, `note rm` the old) or delete them.
The plugin ships a `dekko-notes` skill that prompts Claude Code to do
this upkeep as it edits.

> If you used dekko before 0.8.0, your repository `.gitignore` may
> contain a blanket `.dekko/` line from an older version. Remove it so
> `notes.json` can be tracked — newer versions rely on the inner
> `.dekko/.gitignore` instead and never touch your repo `.gitignore`.

## Language support

Parsing is done with [tree-sitter](https://tree-sitter.github.io/).

- **Tier 1 — full fidelity** (dedicated queries; typed params and return
  types where the language declares them): Python, Rust, C, C++,
  JavaScript, TypeScript (+ TSX), Go, Java. Shipped with every install
  as individual grammar packages, so they parse **offline** — no network
  and no grammar download.
- **Tier 2 — generic fallback** (function names, parameter text, and call
  links): every other grammar in `tree-sitter-language-pack` — Ruby,
  PHP, C#, Kotlin, Swift, Lua, and many more. Requires
  `pip install dekko[all]`, which downloads grammars on demand; without
  it, a Tier-2 file is skipped with a note rather than parsed.

## How call resolution works

Best-effort static resolution, in order: same class/container → same file
→ imported names → unique repo-wide name match. Calls that stay ambiguous
are marked as such rather than guessed; calls to stdlib/third-party code
are recorded in `map.json` only.

## Limitations

The call graph is static and best-effort, so a few edges are invisible by
design:

- **Rust macro bodies**: tree-sitter parses macro invocations
  (`println!`, `vec!`, custom macros) as opaque token trees, so calls
  written inside a macro body are not seen and those edges are missed.
- **Dynamic dispatch**: calls made through reflection, callbacks passed by
  reference, or runtime registries have no static call site. This is why
  `dekko unused` treats decorated/exported symbols as roots and bills its
  output as *leads, not verdicts*.

## Development

```sh
uv sync --extra all              # include Tier-2 grammars + tokenizer
uv run pytest                    # test suite
uv run ruff check .              # lint
uv run ruff format --check .
uv build                         # sdist + wheel into dist/
```

A plain `uv sync` installs only the Tier-1 grammars; the Tier-2 and
tokenizer tests then skip (as they do on a default user install). CI
runs the suite across `{ubuntu, macos, windows} × {3.10, 3.13}` —
see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

Releases: pushing a `v*` tag builds and publishes to PyPI via trusted
publishing (`.github/workflows/release.yml`); configure the trusted
publisher for `aahlijia/dekko` on PyPI first. See
[CHANGELOG.md](CHANGELOG.md) for the per-version history.
