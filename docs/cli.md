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
dekko query supertypes BufferDiff    # what BufferDiff extends/implements/impl's-for (one hop)
dekko query supertypes BufferDiff --transitive  # full ancestor chain/DAG
dekko query subtypes Serializable    # what directly extends/implements/impl's-for Serializable
dekko query subtypes Serializable --transitive  # every direct + indirect implementor
dekko query subtypes Serializable --relation implements  # filter to one relation kind
dekko context run_map --budget 1500  # minimal context pack for an edit
dekko search "retries failed http requests"  # free-text relevance search
dekko search "..." --scorer embedding        # optional; needs dekko[search]
dekko search "..." --scorer both             # fuses lexical+embedding; needs dekko[search]
dekko workset                        # one bundle for your current change
dekko workset --symbol Config --type-impact  # + type-usage + heritage impact, unioned
dekko affected                       # test files impacted by your changes
dekko diff                           # symbols changed since the map's commit (exit 0/1)
dekko unused                         # symbols nothing calls (dead-code leads)
dekko unused --kinds types           # unused types only (heritage + type-usage aware)
dekko unused --kinds all             # callables + types, unioned
dekko ambiguous                      # resolver-trust report: where resolution was ambiguous
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

`query supertypes`/`subtypes` cover declared heritage — `extends`/
`implements` for Python, JavaScript, TypeScript, and Java; `impl`
(Rust's `impl Trait for Type`, plus `trait Sub: Super` supertrait
bounds as `extends`) and `extends` (C++'s `class Foo : public Base`,
access specifiers stripped from the base name) for Rust and C++. Go
struct embedding (`embeds`) is not extracted — deliberately deferred,
not just unimplemented: it only answers struct *composition*, not
"what implements this Go interface" (the actual common Go heritage
question), and Go's structural interface satisfaction has no
declaring syntax to extract in the first place — any type whose
method set matches an interface satisfies it, with no `implements`
keyword anywhere. Answering that for real needs a method-signature
type-checking pass, a different feature in kind, not a language gap
in this one. `--transitive` walks the full ancestor (`supertypes`) or
descendant (`subtypes`) DAG — Python/C++ multiple inheritance and
multi-interface implementation both fan out, not a single chain —
deduplicating diamond-inheritance repeats to each node's shallowest
depth. `--relation {extends,implements,impl}` filters to one relation
kind (Java/TS distinguish extends/implements; Python and C++ have only
`extends`; Rust's `impl Trait for Type` is `impl`, its supertrait
bounds are `extends`); `embeds` is accepted as a valid value for
forward compatibility with a possible future Go pass but never appears
in current output. An external base class or trait (`class MyModel
(BaseModel):` from a third-party package, `impl std::fmt::Debug for
Foo`) shows as a labeled `(external)` row in `supertypes` output
rather than being silently dropped — a type extending/impl'ing a
framework base is a common, expected case, not a corner case worth
hiding. Rust's `impl` heritage resolves the `Type` side by same-file
name lookup (an `impl` block's own file, not cross-file) — an `impl`
block for a type defined in a different file, or a same-named type
appearing twice in one file (two `mod` blocks), produces no heritage
edge rather than a guess.

`workset --symbol NAME --type-impact` widens the touched set beyond
the target symbol's own direct callers: when the target is a
class/interface/struct/trait, every type-usage site (`query type`) and
every transitive implementor (`query subtypes --transitive`) is
unioned in too — the full blast radius of changing a shared type's
shape, not just its call sites. It's a no-op (not an error) on a
non-type target — the widened set just equals the base set — and it
requires `--symbol`: combined with a rev diff (or with no `--symbol`
at all, which defaults to a rev diff) it's rejected with an error,
since a changed-files diff has no single target type. Text output gets
one extra `blast radius: N direct target, M type-usage sites, K
implementors` line under the manifest; JSON output gets a
`seed.blast_radius` object (`direct`/`type_usage`/`heritage` counts)
plus an optional `seed.blast_radius_note` when the target's heritage
has ambiguous inbound edges dekko couldn't resolve — the disclosed
counts are then a conservative undercount, never an overcount, since
ambiguous and external matches are excluded rather than guessed at.

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

`--kinds {callables,types,all}` (default `callables`, matching the above
unchanged) controls which symbol kinds are scanned. `types` restricts the
scan to classes/interfaces/enums/structs/records/traits and additionally
weighs heritage (`heritage_in` — implemented/extended by something else)
and type-usage (used as a parameter/return type elsewhere, the same
matching `query type` does) as evidence a type is alive — a called
function's `calls_in`/`referenced_in` entry, which a type-definition
rarely accumulates directly, isn't the only signal that matters for a
type. `all` scans every symbol kind with every evidence source unioned
in, and shows a per-kind subtotal in its header
(`dekko: 8 unused symbols (5 callables, 3 types)`); JSON output always
carries a `"kind_totals"` field alongside `"results"`/`"meta"`. Type-mode
inherits `unused`'s existing blind spots (dynamic dispatch/reflection,
now also for types) plus `query type`'s own disclosed gap: a type used
only as a struct/class **field**'s type — not a function parameter or
return — is invisible to type-usage matching, since struct/class fields
aren't extracted as their own symbols with a type. An exported/`pub`
type with zero in-repo implementors or usages (a plausible public library
surface) is still excluded via the same root check every other symbol
kind already gets, not flagged as dead just because `--kinds` widened
the scan.

## Interpreting `dekko ambiguous`

`ambiguous` aggregates every call site where a bare name matched 2+
repo-wide candidates and couldn't be resolved — a low ambiguous rate
means the call graph is trustworthy as-is; a high one concentrated in
a few files or names means those spots are worth a manual check before
trusting `query callers`/`callees`/`workset`/`impacted_tests` output
there for an impact-analysis decision:

```sh
dekko ambiguous                    # summary: totals + top-N by name + top-N by file
dekko ambiguous --by name          # every colliding name, ranked by occurrence
dekko ambiguous --by file          # every caller file, ranked by ambiguous-site count
dekko ambiguous --name Generate    # drill down: every caller site + full candidate set for one name
```

Counts here are **distinct `(caller, name)` collisions, not physical
call-site counts**: the resolver keys its ambiguous-call accumulator on
`(caller, name)`, not `(caller, name, line)`, so a caller that
references the same colliding name at 3 different lines counts once in
this report — the same granularity limit `query symbol`'s
"N additional call site(s) resolved ambiguously" disclosure already
has. `--name` reuses `query`'s own ambiguous-candidate rendering, so a
very-high-cardinality collision (a bare `main`/`New`/`Generate`
matched against dozens of same-named repo-wide candidates) truncates
the same way an unresolved-target error does, rather than dumping every
candidate unconditionally.

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
