# CLI usage

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
dekko search "retries failed http requests"  # free-text relevance search
dekko search "..." --scorer embedding        # optional; needs dekko[search]
dekko workset                        # one bundle for your current change
dekko affected                       # test files impacted by your changes
dekko diff                           # symbols changed since the map's commit (exit 0/1)
dekko unused                         # symbols nothing calls (dead-code leads)
dekko export --format html           # interactive single-file browser
dekko status                         # is the map still fresh? (exit 0/1)
dekko daemon start                   # warm-cache background process (see below)
```

Symbol targets accept a bare `name`, `Class.method`, or a qualified
`file.py:name` — ambiguous names list their candidates instead of
guessing. Every read command takes `--json` for structured output.
Most also regenerate a stale map automatically (`--no-regen` to fail
instead) — `diff`, `affected`, `status`, and `ledger` don't accept
`--no-regen` at all: `status`/`ledger` never regenerate regardless,
and `diff`/`affected` always re-parse the current tree in memory
rather than writing a fresh `map.json` to disk, so `dekko status`
right after a `dekko diff`/`dekko affected` on a fresh edit can still
report the map as stale.

`--json` governs the shape of *successful* (exit 0) output only. Any
error — an ambiguous match, a not-found symbol, a stale map under
`--no-regen`, an invalid argument — is always reported as a plain-text
message on stderr with a distinct nonzero exit code, regardless of
`--json`. This is deliberate and consistent project-wide, not a
per-command gap: check the exit code first, and only parse stdout as
JSON when it is 0.

Run `dekko <command> --help` for the full flag list, or see
`dekko --help` for every subcommand (`trace`, `stats`, `lean`, `note`,
`ledger`, `orient` cover more specialized workflows; hooks are
documented in [claude-code.md](claude-code.md#push-hooks-opt-in)).

## Excluding files

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

## Notes

Anchor a durable, committed note to a symbol — it shows up in
`query symbol` and `context` automatically:

```sh
dekko note add resolver.py:resolve "ambiguous calls are marked, never guessed"
dekko note list resolver.py:resolve
```

## Daemon mode

`dekko daemon start` spawns a small per-repo background process that
keeps a warm, in-memory index across CLI calls instead of every
invocation reparsing `map.json` from scratch:

```sh
dekko daemon start                   # spawn it, returns immediately
dekko daemon status                  # running? pid, uptime, cache hits/misses
dekko daemon status --json           # structured form of the above
dekko daemon stop                    # graceful shutdown
```

Once running, every read-only subcommand (`query`, `search`,
`workset`, `diff`, `affected`, `outline`, `context`, `trace`, `stats`,
`summary`, `lean`, `unused`, `status`, `note list`, `export`)
transparently routes through the daemon: identical output and exit
code, just without the reload. Write-path commands (`map`, `note add`/
`note rm`, `hooks ...`) always run directly, sidestepping
write-concurrency entirely. Pass `--no-daemon` on any command to force
direct execution for that one call regardless of whether a daemon is
running.

The daemon fails open: a stale socket, an unreachable process, or any
transport error falls back silently to normal direct execution, so a
dead or never-started daemon is never a hard failure. It self-shuts-
down after 30 minutes idle by default (`dekko daemon start
--idle-timeout SECONDS` to change it), and re-validates its cached
index on every read the same way a direct invocation would, so a
working-tree edit or an out-of-band `dekko map` is never served stale.

All three subcommands take `--root DIR` (default: cwd) for a repo
other than the current directory. Transport is a Unix domain socket at
`.dekko/daemon.sock` on macOS/Linux, or a token-authenticated TCP
loopback connection on Windows.

`dekko serve --mcp` (the MCP server, see below) does **not** talk to
the daemon — it keeps its own independent in-memory index for the
lifetime of that MCP session instead. Running both a daemon and an MCP
session against the same repo works fine, but they don't share a warm
cache or invalidation with each other: `dekko daemon status`'s cache
hit/miss counters only reflect daemon-routed CLI calls, never MCP tool
calls.

## Language support

Tier 1 (full fidelity, offline): Python, Rust, C, C++, JavaScript,
TypeScript/TSX, Go, Java. Tier 2 (generic fallback — names and calls,
no types): everything else `tree-sitter-language-pack` supports (Ruby,
PHP, C#, Kotlin, Swift, Lua, and more), via `pip install dekko[all]`.
