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
dekko query type Config              # what takes/returns Config? (--exact for literal match)
dekko context run_map --budget 1500  # minimal context pack for an edit
dekko search "retries failed http requests"  # free-text relevance search
dekko search "..." --scorer embedding        # optional; needs dekko[search]
dekko search "..." --scorer both             # fuses lexical+embedding; needs dekko[search]
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

`diff`/`affected` compare at symbol-body-hash granularity, not a whole-file
diff: an edit outside every symbol's body span (a trailing comment after the
last function's closing brace, a blank line, a module-level comment) doesn't
change any symbol's hash, so it won't show up as a changed symbol or trigger
an impacted-test report. This is deliberate — comment/whitespace noise
shouldn't spuriously flag every test in a file as impacted — but it's worth
knowing before assuming a "no changes detected" result means the file itself
is byte-identical to the compared rev.

`query type` only covers what tree-sitter extracts a type from:
function/method parameter and return-type annotations. It does not see
struct/class **fields** typed with the target type — those aren't
extracted as their own symbols with a type at all, so a clean result
set from `query type` doesn't mean the type is otherwise unused.
Default matching is identifier-token based (`Config` matches
`Optional[Config]`, `Vec<Config>`, `Config | None`, but not
`ConfigManager`); pass `--exact` to match the stored type text
verbatim instead.

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

## Interpreting `dekko unused`

`unused` finds symbols with no *statically resolvable* inbound call — it
cannot see reflective or dynamic-dispatch invocation, so frameworks that
call code by convention or configuration rather than a direct source-level
call are a predictable source of false positives: Gradle/Maven
plugin-action callbacks invoked through reflective wiring, Rust trait
methods invoked only through `format!`/`.into()`/other trait-dispatch
machinery, and similar "called by the framework, not by name" patterns.
This isn't a bug in the detector — it's an inherent limit of static
call-graph analysis — but treat a raw `unused` count as a set of leads to
spot-check, not a list to delete from unread, especially on
framework-heavy or trait-heavy codebases.

## Daemon mode

`dekko daemon start` spawns a small per-repo background process that
keeps a warm, in-memory index across CLI calls instead of every
invocation reparsing `map.json` from scratch:

```sh
dekko daemon start                   # spawn it, returns immediately
dekko daemon status                  # running? pid, uptime, busy, cache hits/misses
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
transport error *before* a request is sent falls back silently to
normal direct execution, so a dead or never-started daemon is never a
hard failure. It self-shuts-down after 30 minutes idle by default
(`dekko daemon start --idle-timeout SECONDS` to change it), and
re-validates its cached index on every read the same way a direct
invocation would, so a working-tree edit or an out-of-band `dekko map`
is never served stale.

One case is deliberately *not* a silent fallback: if a request has
already been sent to the daemon and no response comes back in time
(a slow request outlasting the connection's own timeout, or the
connection dropping mid-wait), the CLI does **not** transparently
re-run the command locally — the daemon's accept loop is
single-threaded and has no notion of "the client gave up," so it
keeps computing the abandoned request in the background regardless;
silently duplicating that same work locally would waste CPU racing
against its own orphaned daemon-side copy. Instead this prints a
message to stderr and exits with a distinct code (`7`) so the
difference from every other daemon-unavailable case is visible. Retry
with `--no-daemon` to force a fresh local run, or just retry normally
once the daemon has had time to finish.

`diff` and `affected`'s dominant cost — re-parsing and resolving the
*old* side of the comparison (the git rev being diffed against) — is
**not** covered by the daemon's warm cache at all: that cache only
ever holds the current working tree's index. Only the separate,
on-disk `.dekko/rev-cache/` (shared by daemon-routed and direct calls
alike, keyed by resolved commit SHA) makes a *repeat* comparison
against the same rev faster. A daemon-routed `diff`/`affected` against
a rev it hasn't seen before pays the same old-side reparse cost a
direct invocation would — daemon routing speeds up the current-tree
side only.

Even for the current-tree side, the warm cache's win is specifically
skipping map *loading* (re-parsing `map.json` into an in-memory
index), not every part of a query's own cost. A query whose per-hit
rendering dominates — `get_callers`/`find_usages` on a very
high-fan-in "hub" symbol, where formatting hundreds or thousands of
fan-in rows costs more than the index load ever did — can show little
or no measurable wall-clock difference between a cold and warm daemon
call even though `dekko daemon status`'s `hits`/`misses` counters
correctly show the request was served warm. That's expected, not a
sign the cache isn't working.

`dekko daemon status` answers off a dedicated status-only listener
(a second socket, separate from the one routed commands use), not the
daemon's main accept loop — which is deliberately single-threaded and
can't answer anything while busy on another request. So `status` stays
fast and honest even while the daemon is mid-request on a slow query:
it replies immediately with `busy: true` instead of blocking until the
other request finishes or timing out and falsely reporting "not
running."

Under sustained CPU contention on the host machine (many competing
processes, or an unset-`--jobs` cold resolve on a huge repo pegging a
core), even the status-only listener's own reply can be delayed by
GIL/OS scheduling, independent of the main loop being busy. `status`
distinguishes this from a genuinely dead daemon: a connect that
succeeds but doesn't get an answer within a short probe window (2s)
reports `{"running": true, "confirmed": false, "note": "..."}` instead
of lying with `"running": false` — a live-but-momentarily-unanswering
daemon is never misreported as not running.

All three subcommands take `--root DIR` (default: cwd) for a repo
other than the current directory. Transport is a Unix domain socket at
`.dekko/daemon.sock` on macOS/Linux (with a second, status-only socket
alongside it), or a token-authenticated TCP loopback connection on
Windows (likewise a second port for status).

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
