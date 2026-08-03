# dekko

[![CI](https://github.com/aahlijia/dekko/actions/workflows/ci.yml/badge.svg)](https://github.com/aahlijia/dekko/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dekko)](https://pypi.org/project/dekko/)
[![Downloads](https://static.pepy.tech/badge/dekko)](https://pepy.tech/project/dekko)
[![Python](https://img.shields.io/pypi/pyversions/dekko)](https://pypi.org/project/dekko/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**dekko** is a fast, offline, dependency-free **static code map
generator** and **codebase indexer for LLM coding agents**. It scans a
repository with [tree-sitter](https://tree-sitter.github.io/) (no model
tokens spent parsing) and writes:

- **`MAP.md`** — a human-readable map: a per-directory overview, an
  embedded architecture diagram, load-bearing/orchestrator rankings,
  then every file's functions/methods with signatures, doc lines, and
  who calls and is called by whom.
- **`map.json`** — the same graph in machine-readable form.

On top of the map, dekko gives an agent a token-cheap way to answer
questions like "what does this file contain," "who calls this function,"
and "what do I need to safely change this" — without reading whole files.
It ships as a **CLI**, a **Claude Code `/map` plugin + MCP server**
([Model Context Protocol](https://modelcontextprotocol.io/)), and
works with **Cline** too.

## Why dekko?

Most agent workflows gather context by reading whole files or grepping
across a repo — expensive, and it throws away structure (who calls
what, what a function's fan-in/fan-out looks like). dekko instead
parses the repo once into a call graph and answers targeted questions
against it. Measured across 7 real, unmodified open-source repos
(Go, TypeScript, Java, Rust, Python/C++ — up to 14k files), dekko's
structured queries used **3x–200x fewer tokens** than the equivalent
`Read`/`Grep` workflow for the same task (repo orientation, outlining a
large file, tracing a symbol's callers/callees).

| Task | Example repo (scale) | dekko | Read/Grep | Savings |
|---|---|---:|---:|---:|
| Repo orientation (`summary`) | awesome-go (10 files) | 308 tok | ~15,271 tok | ~50x |
| Repo orientation (`summary`) | cline (2,730 files) | 1,202 tok | ~4,020 tok | ~3.3x |
| Outline a large file | claude-code `main.tsx` (4,683 lines) | 1,017 tok | 200,981 tok | ~197x |
| Outline a large file | zed `editor.rs` (12,554 lines) | 1,996 tok | 115,109 tok | ~58x |
| Symbol lookup (`query_symbol` + callers/callees) | tensorflow `Graph` class | ~811 tok | 61,656 tok | ~76x |
| Symbol lookup (`query_symbol` + callers/callees) | spring-boot `prepareContext` | 759 tok | ~18,460 tok | ~24x |
| Bundled context (`workset`) | zed | 2,984 tok | ~5,903+ tok (targeted) / ~164,571 tok (whole file) | ~2x / ~55x |
| Bundled context (`workset`) | awesome-go | 617 tok | ~6,136 tok | ~10x |

dekko's cost stays roughly flat per query while `Read`/`Grep` scales
with file/repo size, so the ratio grows with scale. The win isn't
universal — small,
self-contained files and already-grep-friendly local symbols see
little to no benefit, and a few cases in the raw data are void because
the cheap answer was also an incomplete one. See
[`benchmarks/real-world-repos/`](benchmarks/real-world-repos/README.md)
for the full per-task breakdown, methodology, and correctness caveats.

Compared to tag-index tools like `ctags`/`gtags`, dekko resolves actual
call edges (not just definitions), ranks files by load-bearing-ness,
and speaks directly to agents over MCP or the CLI — no editor plugin
required.

## Installation

```sh
uv tool install dekko     # or: pip install dekko / pipx install dekko
```

The default install bundles nine Tier-1 languages (Python, Rust, C, C++,
JavaScript, TypeScript/TSX, Go, Java) as offline grammar packages — no
network call at parse time. For ~55 additional languages (parsed
generically), add the extra:

```sh
pip install dekko[all]
```

Then add the `/map` command + MCP server to Claude Code:

```sh
dekko --claude-install     # restart Claude Code afterward
```

### From a local clone

```sh
git clone https://github.com/aahlijia/dekko.git
cd dekko
./install.sh               # installs the CLI and registers the plugin
```

### Uninstall

```sh
dekko --claude-uninstall   # remove the /map plugin (and its MCP server)
uv tool uninstall dekko    # or: pip uninstall dekko / pipx uninstall dekko
```

## Quick start

```sh
cd my-project
dekko map                  # writes .dekko/MAP.md + .dekko/map.json
dekko summary               # ~40-line digest: dirs, hotspots, entry points
```

`.dekko/` is git-ignored by default; the map regenerates on demand, so
you rarely need to run `dekko map` again by hand.

## Core CLI commands

```sh
dekko map                            # (re)generate the map
dekko map src                        # ...restricted to a subtree
dekko summary                        # repo digest: dirs, hotspots, entry points
dekko outline src/server.py          # a file's signatures + docs, no bodies
dekko query symbol run_map           # signature card: doc, location, fan-in/out
dekko query callers resolve --sites  # who calls resolve, with call sites
dekko query callees main             # what does main call?
dekko query uses Path                # who references the external name Path?
dekko context run_map --budget 1500  # minimal context pack for an edit
dekko workset                        # one bundle for your current change
dekko affected                       # test files impacted by your changes
dekko diff                           # symbols changed since the map's commit
dekko unused                         # symbols nothing calls (dead-code leads)
dekko export --format html           # interactive single-file browser
dekko status                         # is the map still fresh? (exit 0/1)
```

Symbol targets accept a bare `name`, `Class.method`, or a qualified
`file.py:name` — ambiguous names list their candidates instead of
guessing. Every read command takes `--json` for structured output and
regenerates a stale map automatically (`--no-regen` to fail instead).

Run `dekko <command> --help` for the full flag list, or see
`dekko --help` for every subcommand (`trace`, `stats`, `lean`, `note`,
`ledger`, `orient` cover more specialized workflows; `hooks` is
documented below).

### Excluding files

`--exclude GLOB` (repeatable) skips extra files for `dekko map`,
matched against both the basename and the full relative path:

```sh
dekko map --exclude 'fixtures/*' --exclude '*.generated.py'
```

Every pattern is also persisted to `.dekko/.dekkoignore` (tracked, not
git-ignored), so a bare `dekko map` afterward keeps honoring it without
retyping `--exclude`. That file is directly hand-editable too,
gitignore-style (comments, negation, `**`). The two sources are
additive — a file is skipped if either matches — but use different
matching engines (`--exclude` is plain `fnmatch`; `.dekkoignore` is
gitignore syntax), so an identical pattern can occasionally match a
slightly different set of nested paths depending on which file it's
in; run `dekko map --help` for the details. Skips are reported
separately in the run summary: `excluded` for `--exclude`, `ignored`
for `.dekkoignore`.

### Notes

Anchor a durable, committed note to a symbol — it shows up in
`query symbol` and `context` automatically:

```sh
dekko note add resolver.py:resolve "ambiguous calls are marked, never guessed"
dekko note list resolver.py:resolve
```

## Using the Claude Code plugin

```sh
/map            # map the whole repository
/map src/       # map a subtree only
```

`dekko --claude-install` wires up both the `/map` command and the MCP
server (see below); the plugin just runs the installed `dekko` CLI, so
install the package first.

### Push hooks (opt-in)

Everything above is *pull* — it only helps once the agent knows to
ask. `dekko hooks` adds an opt-in *push* layer: three Claude Code hook
events, enabled individually, that inject context automatically:

```sh
dekko hooks install                        # session-start only (the default)
dekko hooks install --enable session-start --enable prompt-submit --enable pre-read
dekko hooks uninstall                      # remove all dekko hooks
```

- **`session-start`** — a steering preamble plus a budget-capped `lean`
  map, so the first turn already has a navigation map.
- **`prompt-submit`** — for the new prompt, a short pointer to the most
  task-relevant files not already read, so the agent doesn't `grep` blind.
- **`pre-read`** — a non-blocking advisory to `outline` a large file
  first, before a whole-file `Read`.

Installing writes to `.claude/settings.json` (restart Claude Code to
activate). Every handler is fail-silent — a stale map or hook error
never blocks a session or a tool call, it just produces no output.

## Using the MCP server

`dekko serve --mcp` speaks the Model Context Protocol over stdio
(newline-delimited JSON-RPC, no SDK dependency), so an agent can ask
"who calls X?" with a tool call instead of reading `MAP.md`. It exposes
13 tools:

| Tool | Backs |
| --- | --- |
| `query_symbol` | signature, doc, fan-in/out, notes |
| `get_callers` / `get_callees` | callers/callees, with call sites |
| `find_usages` | references to an external name |
| `get_context_pack` | a symbol's neighborhood, budget-capped |
| `outline` | a file's structure without bodies |
| `workset` | one bundle for a change (`rev` or `symbol`) |
| `summary` | repo digest |
| `impacted_tests` | test files impacted by changes |
| `add_note` / `list_notes` | symbol-anchored notes |
| `map_status` / `refresh_map` | freshness check / regenerate |

`dekko --claude-install` registers this automatically for Claude Code.
For a standalone registration: `dekko --mcp-install` (runs
`claude mcp add dekko -- dekko serve --mcp`).

**Note:** a running `dekko serve --mcp` process holds its code in memory
for its whole lifetime — restart it after any dekko upgrade or source
change, or its output can silently disagree with the CLI.

### Cline

```sh
dekko --cline-install      # merge dekko into cline_mcp_settings.json
dekko --cline-uninstall    # remove just the dekko entry
```

Cline has no plugin system, so only the MCP tools are available (no
`/map`-equivalent slash command). See `dekko --cline-install --help`
for scope/config overrides if auto-detection picks the wrong file.

## Language support

Tier 1 (full fidelity, offline): Python, Rust, C, C++, JavaScript,
TypeScript/TSX, Go, Java. Tier 2 (generic fallback — names and calls,
no types): everything else `tree-sitter-language-pack` supports (Ruby,
PHP, C#, Kotlin, Swift, Lua, and more), via `pip install dekko[all]`.

## Learn more

- [CHANGELOG.md](CHANGELOG.md) — per-version history
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, testing, releasing
- [benchmarks/](benchmarks/) — token-efficiency measurements, including
  a 7-repo real-world comparison against a plain Read/Grep workflow
