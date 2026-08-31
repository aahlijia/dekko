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
dekko query importers os.path        # what files import a source matching os.path?
dekko query peers load_config        # symbols sharing 2+ callees with load_config
dekko query supertypes BufferDiff    # what BufferDiff extends/implements/impl's-for (one hop)
dekko query supertypes BufferDiff --transitive  # full ancestor chain/DAG
dekko query subtypes Serializable    # what directly extends/implements/impl's-for Serializable
dekko query subtypes Serializable --transitive  # every direct + indirect implementor
dekko query subtypes Serializable --relation implements  # filter to one relation kind
dekko query throws load_config       # what load_config's own body raises (one level, not transitive)
dekko query throws load_config --transitive --depth 3  # + everything its callees raise, depth-capped
dekko query catches ConfigError      # every catch clause that would handle ConfigError
dekko query catches ConfigError --lang java  # ...scoped to one language, cuts cross-language noise
dekko query env DATABASE_URL         # every statically-known read site for this env var
dekko query env --list               # every distinct env var read anywhere, ranked by read-site count
dekko query cohesion src/app.py      # intra-file connected-components (weak signal, not clustering)
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
dekko unused --suspect               # + flag excluded symbols whose name is a proven collider
dekko unused --dispatch              # + list flagged symbols that are unresolved dispatch candidates
dekko ambiguous                      # resolver-trust report: where resolution was ambiguous
dekko deps                           # module-level dependency graph: edge/file counts, cycle count
dekko deps src/app.py                # one file's resolved imports/importers/external sources
dekko deps --cycles                  # every detected circular-import cluster
dekko export --format html           # interactive single-file browser
dekko status                         # is the map still fresh? (exit 0/1)
dekko doctor                         # diagnose install/environment issues (PATH shadowing, hooks, MCP, ...)
dekko sanity resolve                 # cross-check `callers resolve` against a targeted grep sweep
dekko sanity Path --usages           # cross-check `uses Path` instead of `callers`
dekko sanity --all                   # repo-wide sweep: every fan-in symbol, not just one target
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

`query importers` and `query peers` are **CLI-only** (no MCP tool),
like `deps`/`throws`/`catches`/`env`/`cohesion` — they answer "what
else imports/uses the same thing as X", a reverse-import lookup and a
shared-callee peer lookup, respectively. `importers <source>` matches against each
file's raw, unresolved import-source text (`os.path`, `../utils`,
`std::collections::HashMap`) — no cross-language module-path
resolution is attempted, so it's a text match, not a "does this
resolve to a real file" answer; default matching is substring
(`os.path` matches both `import os.path` and `from os.path import
join`), `--exact` requires the literal source string (trailing slash
normalized for relative sources). For JS/TS, `--exact` matches the
bare module specifier, not a particular named/default import binding
(`--exact react` matches `import React from "react"` and `import
{ useState } from "react"` alike) — Import.source stores JS/TS names
with an arbitrary local binding name appended internally, which
`--exact` strips back off before comparing, so two different named
imports from the same package both satisfy the same `--exact` match.
`peers <symbol>` finds other symbols
whose outgoing calls overlap the target's by at least `--min-shared`
callees (default 2 — a single shared callee, like both calling
`print`/`log`, is usually noise); results are ranked by shared-callee
count, and each row lists the shared callee names so it's clear *why*
two symbols are peers without a second lookup. A symbol with zero
callees has no peers by construction (a clean empty result, not an
error); a symbol with fewer callees than `--min-shared` gets a hint to
lower the threshold. Small/sparse repos often need `--min-shared 1`
to find any peers at all under the default threshold of 2.

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
edge rather than a guess. When a Rust trait/type name collides across
two same-named crates in the workspace (e.g. a real crate plus a
same-named `test_fixture`/`vendor`-shaped directory used to test
tooling), dekko resolves the collision only when exactly one crate's
own path avoids that synthetic-looking marker — a narrow,
convention-based tiebreak, not a guess between two equally plausible
real crates, which still resolves ambiguous. A `note: N heritage
edge(s) repo-wide resolved by preferring a non-test-fixture/vendor
crate root...` on `supertypes`/`subtypes` output (repo-wide, not
scoped to that one query) discloses when this tiebreak fired anywhere
in the map, so a caller can double-check results against the real
source if the repo genuinely has two workspace crates sharing a name.

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
`ledger`, `orient`, `deps` cover more specialized workflows; hooks are
documented in [claude-code.md](claude-code.md#push-hooks-opt-in)).

## Diagnosing environment issues

`dekko doctor` reports, in one shot: whether a bare `dekko` on `$PATH`
resolves to the binary you think it is (the single most-repeated
friction point across past eval rounds — a stale globally-installed
`dekko` silently answering from the wrong build), whether the map is
fresh, and the install state of every opt-in Claude Code layer (MCP
registration, plugin, hooks, the `CLAUDE.md` policy block). Each row is
`ok`/`missing`/`stale`/`unknown`, with the exact fix command named for
anything not `ok`; `--json` for scripting. It never regenerates the map
and never auto-fixes anything — reporting only. Full check list and the
Claude Code `/doctor` slash command:
[claude-code.md](claude-code.md#the-doctor-command).

## Sanity-checking a low/zero call-graph result

`dekko sanity <target>` automates the `dekko-verify` skill's "reach
for one targeted grep" check: it runs the same `query callers <target>`
dekko would answer with (or `query uses <target>` under `--usages`),
runs one scoped `grep -rn <bare-name>` across the repo (excluding the
same directories `dekko map` already excludes), and diffs the two hit
sets by `(file, line)` into three buckets:

- **matches** — both found it.
- **dekko-only** — dekko resolved a call grep's literal pattern
  didn't match (an alias, a multi-line call) — informational.
- **grep-only** — grep found a line dekko's answer missed. Each entry
  is labeled with a likely cause: a cross-package/qualified call
  (`pkg.Func(`, `Type::method(`, `Type.method(`), a bare
  import/require statement naming the symbol (`import { X } from
  '...'`, `from x import X`, `const { X } = require('...')` — not a
  call site), a file in a language dekko can't parse, a likely
  unrelated external-library method sharing the target's bare name
  (see "Receiver-mismatch detection" below), a test-only call
  site (tests are excluded from the dekko-side query by default here,
  unlike the plain `query callers` default — see `--include-tests`), a
  short/generic target name (resolver precision degrades in a dense
  repo), or "unexplained" when none of those fit.

**Receiver-mismatch detection.** When the target is a method (not a
free function) with exactly one repo-defined symbol sharing its bare
name and its declaring type resolves unambiguously, `sanity` checks
each grep-only hit for whether the declaring type's own simple name
appears on the hit's line or in the first few lines of its file
(where imports/`using` live). If neither does, the hit is classified
`likely an unrelated external-library method sharing this bare name`
instead of a generic/test/unexplained miss — the case where a
same-named method from a completely different library (e.g. AssertJ's
`isTrue()` colliding with a repo's own `isTrue()`) reads as a real
grep-only miss when it isn't one. This is a cheap textual proxy, not
real type inference (no alias tracking, no import resolution) — false
positives (missing the real evidence) are the accepted failure mode,
not false confidence. Gated to `sanity <target>`; `--all` does not
apply this check (it has no single target to resolve a declaring type
against).

The grep sweep itself has two safety caps: it stops reading raw grep
output past 5,000 lines, and drops any single raw line over ~10,000
characters (a binary/data blob grep's own binary-skip heuristic didn't
catch, not real source — e.g. a stray minified/cache file). A snippet
shown in the report is also capped at ~240 characters. If the 5,000-line
cap is hit, that's disclosed explicitly (`grep_truncated` in `--json`,
a `note:` line otherwise) and the **dekko-only** bucket is reported as
inconclusive rather than a count — a truncated sweep can't rule out
that grep would have matched a dekko-resolved location past the
cutoff, so a "dekko-only" number under truncation would be false
confidence, not a finding.

```sh
dekko sanity resolve                 # cross-check `callers resolve`
dekko sanity Path --usages           # cross-check `uses Path` instead
dekko sanity resolve --include-tests # include test files in the dekko-side query
dekko sanity resolve --group-by-file # roll up grep-only rows by file
```

`--group-by-file` rolls up the **grep-only** bucket by file (count and
cause breakdown per file, largest cluster first) instead of listing
individual match rows — useful for spotting a single file that
accounts for a large share of a big "unexplained" count instead of
reading past `--limit`'s default truncation or reaching for `--json`
and aggregating by hand. Grouping happens over whatever rows already
survived `--limit`/`--budget` fitting, not the pre-truncation total —
pair it with a raised `--limit`/`--budget` when chasing a suspected
large cluster, or the rollup only reflects a truncated sample. Scoped
to the grep-only bucket in single-target text mode; has no effect
under `--json` (already fully groupable by an external consumer) or
`--all` (which rolls up by symbol, not by file).

Always exits `0` on a completed comparison — a nonempty `grep-only`
bucket is a finding to relay, not itself an error; this is a spot
check, not a re-verification. See the Claude Code `/sanity` slash
command: [claude-code.md](claude-code.md#the-sanity-command).

### `dekko sanity --all` — sweeping every symbol instead of one target

`dekko sanity <target>` only ever checks the one symbol a human or
agent happens to pick. `dekko sanity --all` removes that selection
bias: it runs the same callers/grep cross-check over *every* in-repo
symbol with at least one caller (nonzero `calls_in` fan-in), so a
classification bug in `sanity` itself (or a genuine grep-vs-dekko
disagreement) can't hide behind whichever target nobody happened to
try. It's callers-mode only — `--all` combined with `--usages` or
`--unused` is a CLI error, not a silently different mode.

```sh
dekko sanity --all                        # sweep every fan-in symbol, jobs=4
dekko sanity --all --jobs 1                # sequential (no thread pool)
dekko sanity --all --jobs 0                # use all cores
dekko sanity --all --max-names 500         # cap unique bare names swept
dekko sanity --all --fail-on-unexplained   # CI gate: nonzero exit if any unexplained miss
```

The grep sweep is deduped by bare name, not by symbol: two symbols
sharing a name (an overload set, same-named methods on different
types) produce byte-identical grep sweeps and classifications, so
`--all` runs one `grep -rn` per *unique* bare name among fan-in
symbols, not one per symbol — the same expensive-subprocess-per-name
work `sanity <target>` already does, just amortized. `--jobs N`
(default `4`; `0` = all cores, `1` = sequential) parallelizes the
remaining per-name sweeps across a thread pool, since each is I/O-bound
(waiting on a `grep` subprocess), not CPU-bound.

Output is a triage summary, not a full per-symbol report: an aggregate
histogram of grep-only causes across the whole sweep, then the symbols
with a nonzero *unexplained* miss, sorted by count. Re-run `dekko
sanity <target>` on any flagged symbol for the full match/dekko-only/
grep-only detail. `--json` carries the full per-symbol breakdown (not
just the flagged subset) for programmatic/CI use.

A safety cap (`--max-names`, default 2,000) bounds how many unique
bare names get swept on a very large repo; names are swept in a fixed
alphabetical order, and hitting the cap is disclosed (`names_truncated`
in `--json`, a `note:` line otherwise) rather than silently covering a
partial, unlabeled subset.

Exits `0` regardless of findings by default, matching `sanity
<target>`'s "advisory, not an error" contract. `--fail-on-unexplained`
exits `3` when the aggregate unexplained-cause count is nonzero — opt
in for CI, so adopting `--all` in a pipeline can't surprise-break it on
first use.

This sweep only re-checks symbols dekko already believes have callers
— it does not find a resolver false-negative that undercounts a symbol
to zero callers in the first place (a different, already-tracked
class of gap).

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

## Interpreting `--sites` call-site counts

`query callers`/`query callees --sites` expands each caller/callee
into one row per call site instead of one row per entity, since a
single caller can invoke the target more than once in its own body.
Without `--sites`, both counts agree (one row per caller); with it,
they can legitimately diverge — a caller invoking the target three
times contributes one caller but three call-site rows. This looked
like a truncation bug before both numbers were disclosed together:

```sh
dekko query callers resolve --sites
# (~340 tokens · 15 callers · 19 sites)
```

The footer's `N callers`/`N callees` clause is the distinct-entity
count; the row/omission total (`X of Y sites omitted`, when capped)
is the call-site count. `--json` mirrors this: `meta.related_total`/
`meta.related_label` carry the distinct-entity count, and
`meta.sites_total` (only present under `--sites`) carries the
pre-cap call-site total. If `sites_total` is higher than the number
of `results` entries, that's the same expected divergence, not a sign
rows were dropped — check `meta.related_total`/the omission fields
for whether anything was actually capped.

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
scan to classes/interfaces/enums/structs/records/traits/type-aliases
(TS/TSX `type X = ...`) and additionally
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

**C/C++ caveat.** When results include at least one C or C++ symbol,
`unused` prints an extra advisory line after the summary footer (text
mode) or a non-empty `"caveats"` list (JSON mode, `[]` otherwise):
`note: exported/extern "C" symbols may be consumed outside this repo's
call graph — treat top hits on a public C API skeptically`. A C/C++
codebase commonly exports a public ABI (`extern "C"` functions called
from Go/Swift/pip bindings through a compiled `.so`) that no in-repo
call graph can see, so a top hit on a `TF_*`-shaped or similarly
ABI-flavored name deserves more scrutiny than a same-shaped internal
helper. This is advisory only — it does not change which symbols are
reported, and it's gated on the actual results (not just "this repo
has a `.c` file somewhere"), so a repo whose C/C++ files happen to
produce zero unused hits stays silent.

**`--suspect` (off by default).** `unused` trusts inbound call-graph
fan-in as proof a symbol is alive — but that fan-in can itself be a
resolver misattribution: a bare-name call site resolves to *some*
repo-defined symbol whenever exactly one shares that name, with no
arity or receiver-type check (see "Interpreting `dekko ambiguous`"
below). `--suspect` cross-references every symbol `unused` excluded
via direct fan-in against `dekko ambiguous`'s collision list — names
`ambiguous` independently proved collide across 2+ repo-defined
candidates somewhere else in the repo. A hit means the name is a
proven collider *somewhere*, not that this specific symbol's credited
calls are wrong; treat it as a lead worth a `dekko ambiguous --name
<name>` glance, not a verdict. Coverage is inherently partial: a name
colliding with exactly one non-repo builtin/library method and nothing
else repo-wide never produces an `ambiguous` triple, so it never
becomes a suspect either — this closes the loop only for names that
also collide 2+ ways somewhere else in the repo. `--suspect` adds a
`"suspects"` JSON key / text section without changing the existing
unused-list output at all when omitted.

**Dispatch-candidate caveat (always on) and `--dispatch` (off by
default).** The mirror-image case of `--suspect`: a symbol *is*
reported unused, but its own id is one of the unresolved candidates of
some ambiguous call site elsewhere in the repo (`MapIndex.ambiguous_in`
keyed by candidate id, the same table `--suspect`/`dekko ambiguous`
already read). This is exactly the shape an OOP hierarchy produces
when an abstract base calls its own virtual method (`this.method()`/
`self.method()`) and 2+ concrete subclasses override it: the base
never defines the method itself, every override is a same-named
candidate, none can be picked over the others, and the resolver can
never attribute the base's call to any single override — each
override then shows up in `unused` with zero direct fan-in, even
though every one of them is genuinely called through the base class.
Unlike `--suspect`'s bare-name collision check, this is a
same-symbol-id match, not a same-name match, so it's meaningfully more
precise (though not perfectly so — a symbol's id can land in
`ambiguous_in` for an unrelated collision that has nothing to do with
polymorphic dispatch; the recommended check is still correct
regardless of the exact reason). Because the check is cheap (one
`dict.get()` per already-computed result row, no extra resolver pass),
an advisory count is always printed the moment any exist — no flag
needed — mirroring the C/C++ ABI caveat's "silent unless relevant"
behavior:

```
note: 2 of these are unresolved-ambiguous-call candidates elsewhere in
the repo -- may be reached via this.method()/self.method()
polymorphic dispatch the resolver can't attribute. Run `dekko sanity
--unused <name>` before deleting any of them (see --dispatch for
which ones).
```

`--dispatch` additionally lists which flagged symbols these are, one
row per candidate with the exact `dekko sanity --unused <qualname>`
command to run before deleting it — `"dispatch_candidates"` JSON key /
text section, independent of and composable with `--suspect`. As with
`--suspect`, this is a lead, not a verdict: cross-check with `dekko
sanity --unused` before deleting any flagged symbol this catches,
especially on inheritance-heavy OOP codebases.

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

**Standing high-ambiguous-rate flag.** A repo whose repo-wide
ambiguous rate is **30% or higher** doesn't stay silent until you think
to run this command — that threshold (`ambiguous.HIGH_AMBIGUOUS_RATE`,
calibrated against a 7-repo eval spread ranging from 0% to 57%) is
surfaced proactively in three places so an agent can't accidentally
trust `query callers`/`workset` fan-in numbers without knowing a large,
invisible slice of the call graph never became a resolved edge at all:

- `dekko summary` / `dekko orient` (and the `summary` MCP tool/
  resource, which render the same digest) print a `note: this repo's
  call resolution is N% ambiguous (M sites) — treat query
  callers/workset fan-in counts as a floor, not exact; run \`dekko
  ambiguous --by name\` to see what's colliding.` line right after the
  coverage note.
- The `session-start` hook (see `docs/claude-code.md`) injects the same
  line into its preamble on every session where the hook is installed —
  the one channel here that's genuinely proactive rather than
  pull-based.
- `dekko doctor` reports an `ambiguous-rate` finding: `ok` below the
  threshold, an `advisory` status at/above it (`fix: dekko ambiguous
  --by name`), or `unknown` when the map predates this field (rerun
  `dekko map` to pick it up — the rate is stamped into `provenance` at
  map-write time so `doctor` stays fast, no full `map.json` parse).

## Interpreting `dekko deps`

`deps` reports the **module-level dependency graph**: which files
import which files, resolved from every extracted `import`/`use`/
`#include` statement's raw source string to the in-repo file it
actually names (or left external when it names a stdlib/third-party
source, or dekko can't confidently place it). This is a distinct
question from the call-graph views `export --granularity file`/
`--granularity symbol` already answer ("which files call into which
files" — a runtime
question) — the two usually roughly agree but can diverge: a file
can import a module purely for a type annotation or a side effect
(`import "./polyfill"`) with zero calls ever crossing that edge:

```sh
dekko deps                           # summary: file/edge counts, cycle count, top-N most-depended-on
dekko deps src/app.py                # this file's resolved imports/importers + external sources
dekko deps --file src/app.py         # same as above -- explicit/scriptable spelling of FILE
dekko deps --cycles                  # every detected circular-import cluster, one block per cycle
dekko deps --top 20                  # widen the most-depended-on ranking in the default summary
dekko deps --export mermaid          # emit the module graph via `export`'s existing renderers
dekko deps --export dot --output deps.dot
```

`dekko deps FILE` and `dekko deps --file FILE` are equivalent — the
bare positional is a convenience alias for interactive use, `--file`
remains the explicit spelling for scripts. `FILE`/`--file`,
`--cycles`, and `--export` are mutually exclusive — give at most one,
the same "one, not several" rule `ambiguous`'s `--by`/`--name` already
follows.

Cycle detection groups files into strongly-connected components
(Tarjan's SCC): a reported cycle is every file mutually reachable from
every other file in that group via resolved imports, not necessarily
a single walked chain — a group of 2+ files means those files can't be
split apart without addressing the cycle first. A file that imports
itself (a re-export pattern gone wrong, or simply unusual code) is
reported as its own distinct 1-file cycle, labeled `(self-import)`,
never merged into a real multi-file group's count. On Rust repos
specifically, an inline submodule referencing an earlier item in the
same file (`mod tests { use crate::Foo; }`, or any non-test inline
`mod` doing the same) also shows up as `(self-import)` — this is
ordinary, extremely common Rust, not a re-export smell; the label
means "this file's own `use` graph has a cycle," which for Rust's
per-file-module convention is frequently harmless rather than a sign
of anything to fix.

**Per-language resolution coverage** — a source string is matched
against the repo's real file layout, not guessed when more than one
file could plausibly be meant:

- **Python**: relative imports (leading dots) resolve against the
  importing file's own directory; absolute imports resolve against the
  repo's real package layout (a directory with its own `__init__.py`),
  found by package name regardless of whether it sits at the repo root
  or nested under `src/`. A dotted import's last segment is tried both
  as a submodule file and, when no such file exists, as a symbol
  defined in the parent module — real Python import semantics have no
  actual ambiguity here once the submodule file is checked for.
- **JavaScript/TypeScript/TSX**: `./`/`../`-prefixed sources resolve
  relative to the importing file, trying `.ts`/`.tsx`/`.js`/`.jsx` and
  `index.*` in turn. Bare specifiers (`"react"`, `"lodash"`) are
  external by construction; a `tsconfig.json` path-alias/`baseUrl`
  absolute import is not resolved (out of scope — would need
  `tsconfig.json` parsing).
- **Rust**: `crate::`/`self::`/`super::` paths resolve against a
  best-effort crate-root/module-tree walk (the nearest ancestor
  directory with `lib.rs`/`main.rs`, absent any `Cargo.toml` parsing).
  A bare crate name (`use serde::Deserialize`) is external.
- **Java**: `import com.foo.Bar;` maps mechanically to `.../com/foo/
  Bar.java`, searched against the repo regardless of whether sources
  sit at the repo root or nested under a Maven/Gradle `src/main/java`
  (or `src/test/java`) module directory — confirmed against
  `spring-boot`'s real multi-module layout.
- **C/C++**: `#include` resolves by filename search (no
  package-qualified path the way Java's `import` has); two headers
  sharing a basename in different directories are left external rather
  than guessed. Quoted (`"local.h"`) and angle-bracket (`<system.h>`)
  forms are **not** distinguished — that information doesn't survive
  extraction — so this is a filename search for both forms alike, not
  the "angle brackets are always external" shortcut a compiler gets;
  in practice a system header only resolves in error if the repo
  happens to have its own same-named file, which the basename-
  uniqueness check above already guards against for the common case.
- **Go**: not resolved. Go's import paths need the module's own
  declared prefix (`go.mod`), which dekko does not parse — every Go
  import is reported external rather than guessed from a bare
  directory-name match. A documented limitation, not a silent gap:
  `dekko deps` on a Go-heavy repo will show every Go file as
  external-only rather than a misleadingly sparse (but wrong) graph.

`dekko deps` is **CLI-only** (no MCP tool) — a repo-architecture report
for refactor/circular-import planning, not a per-turn lookup an agent
reaches for mid-edit, the same call `stats`/`unused` already make.

## Interpreting `dekko query throws`/`catches`

`throws`/`catches` trace exception/error flow: what can calling a
function raise, and who catches a given exception type. This is a
**scoped pilot, not a general feature** — exception handling isn't a
uniform language feature, so coverage is deliberately narrower than
every other `query` action:

- **Python, Java, C++**: full support. `throws` reads `raise`/`throw`
  sites; Java additionally reads a method's own declared `throws
  IOException` checked-exception clause as an independent signal (a
  method that only propagates a caught exception, with no `throw`
  statement of its own, still shows up). `catches` reads `except`/
  `catch` clauses, typed or catch-all.
- **JS/TS**: `throws` works fully (`throw` sites are ordinary syntax).
  `catches` is a **weak signal** — JS/TS never type-discriminate a
  caught value at the syntax level, so every plain JS `catch` and
  every untyped TS `catch (e)` extracts as a catch-all that matches
  any query; only a rare `catch (e: SomeType)` TS annotation is a real
  typed match. `dekko query catches` always prints this caveat in its
  own output (not just here), so a near-empty result on a JS/TS-heavy
  repo isn't mistaken for "nothing catches this."
- **Rust, Go, C: not supported, permanently** — not a coverage gap
  waiting on a future pass. Rust's `Result<T, E>`/`?` propagation and
  Go's returned-`error`-value convention are type-inference questions,
  not syntax a tree-sitter query can point at; C has no exception
  concept to extract at all. `throws`/`catches` against a Rust/Go/C
  file report nothing, the same as if the file weren't parsed for this
  feature at all — and unlike a silent gap, both commands now disclose
  this directly in their own output rather than only here: `throws`
  against a Rust/Go/C target prints a distinct "not tracked for
  `<language>`" message (and `--json` sets `"language_supported":
  false`) instead of the generic empty-result text; `catches` prints a
  `note:` line (and `--json` adds a `language_coverage` object) whenever
  the repo has any Rust/Go/C files, giving the excluded/total file
  count so a partial-coverage repo doesn't read as "nothing catches
  this."

```sh
dekko query throws load_config              # what load_config's own body raises (one level)
dekko query throws load_config --transitive # + everything its callees raise, up to --depth hops
dekko query throws load_config --transitive --depth 4  # widen the walk (default depth: 2)
dekko query catches ConfigError             # every catch clause that would handle ConfigError
```

`throws <symbol>` (a function/method target, resolved the same way
`callers`/`callees` resolve one) defaults to one level — only the
target's own raise/throw sites (plus, for Java, its own declared
`throws` clause). `--transitive` walks the call graph outward instead,
unioning every throw found along the way, up to `--depth` hops
(default 2) — call-graph reachability can be very deep, and "everything
this function's entire call tree might raise" degrades toward "every
exception type in the repo" on a well-connected codebase, so the walk
is hard-capped rather than unbounded. Hitting the cap with callees
still unwalked prints a `note:` disclosing the truncation rather than
silently under-reporting — raise `--depth` to widen. Output always
states how many throw sites are repo-defined vs. external (`N throw
site(s): M repo-defined, K external`) since the overwhelming majority
of real raised types are stdlib/third-party (`ValueError`,
`IOException`, `std::runtime_error`) — this is expected, not a sign
the feature isn't working. A bare re-raise (Python bare `raise`, C++
bare `throw;`) can't be resolved to a specific type — its actual type
depends on the enclosing handler, a data-flow fact this pass doesn't
track — so it's counted separately and disclosed (`note: N re-raise
site(s) omitted`), never silently dropped or miscounted as "no throw."

`catches <type-name>` (a bare raised-type name, like `ConfigError` or
`ValueError` — not a resolved symbol lookup, since the common case is
a stdlib/third-party type never extracted as a repo symbol at all)
scans every catch clause repo-wide and reports every one that would
handle that type: an exact name match, or a catch-all (which always
matches, regardless of type). **This is exact-name matching only** —
catching a *superclass* of the queried type (`except Exception:`
catching a raised `ConfigError` that extends `Exception`) is **not**
detected as a match in this version; a real, disclosed precision gap,
not an assumed-away one. **Default sort puts exact matches ahead of
catch-alls** — always on, no flag needed — so a real typed match (rare
on a JS/TS-heavy repo per the caveat above) always ranks before the
catch-all noise instead of being interleaved with it by path
alphabetization; within each group, rows still sort by path/line/
caller. This matters most when `--limit`/`--budget` truncates: the
higher-signal exact matches are now what survives the cap.

**`--lang <language>` scopes `throws`/`catches` to one language** —
cuts cross-language noise on a multi-language repo, e.g. a 99%-Java
repo carrying a handful of vendored JS files whose untyped catch-alls
(see the JS/TS weak-signal caveat above) would otherwise dominate a
`catches` result for a Java-only exception type:

```sh
dekko query catches ConfigurationPropertiesBindException --lang java
dekko query throws handleRequest --transitive --lang java
```

`--lang` accepts any language `throws`/`catches` extracts data for
(currently `cpp`, `java`, `javascript`, `python`, `tsx`, `typescript`
— derived from the language registry, so it stays in sync with
coverage automatically); an unsupported value (`rust`, `go`, ...) is
rejected by the CLI with a clear error rather than silently accepted
and producing an always-empty result. For `catches`, filtering is by
each catch clause's own file language; the excluded count and its
per-language breakdown are disclosed (`note: --lang java filter
applied — 30 catch clause(s) in another language excluded (28
javascript, 2 typescript)`; `--json` adds `lang_filter`/
`lang_filtered_out`). For `throws`, one-level filtering is by the
target symbol's own language — a `--lang` that disagrees with the
target's language necessarily empties the result, and a distinct
`note:` calls this out as a filter mismatch rather than reading like
"target throws nothing"; `--transitive` filtering additionally applies
to whatever the call-graph walk reaches, with the same excluded-count
disclosure as `catches`.

`throws`/`catches` are **CLI-only** (no MCP tool) — given the scoped-
pilot framing and the JS/TS caveat above, this doesn't yet clear the
bar `get_callers`/`find_type_usages` cleared for always-loaded MCP
schema rent; revisit once real usage confirms the signal is worth it.

## Interpreting `dekko query env`

`env` finds statically-known environment-variable **read** call sites
— "where is `DATABASE_URL` read" — across a small, hand-curated
allowlist of known `getenv`-shaped call idioms per language: Python's
`os.getenv(...)`/`os.environ.get(...)`/`os.environ[...]`, JS/TS's
`process.env.X`/`process.env["X"]`, Java's `System.getenv(...)`,
Rust's `std::env::var(...)`/`env::var(...)`/their `_os` variants, Go's
`os.Getenv(...)`/`os.LookupEnv(...)`, and C/C++'s bare `getenv(...)`.
All Tier-1 languages are covered (unlike `throws`/`catches`, there's
no Rust/Go/C exclusion here — an env-var read is just a call/member
expression, not a language feature some languages structurally lack).

This is a **detector, not a resolver** — it is explicitly **not**:

- A general string-literal search (`search` already covers free-text
  lookups, imperfectly; this doesn't replace or extend that).
- Assignment/data-flow tracking. "Where does the value read from
  `DATABASE_URL` end up, and does anything override it" is out of
  scope — only the read call site itself is indexed.
- Config-*file* tracing (YAML/JSON/TOML/`.env` key lookups) — scoped
  to in-source `getenv`-shaped call expressions only.

```sh
dekko query env DATABASE_URL   # every read site for this exact env-var name
dekko query env --list         # every distinct env-var name read anywhere, with read-site counts
```

`env <NAME>` is an **exact-match** lookup (no loose/token matching,
and no case-folding — `PORT` and `port` are genuinely different keys,
matching how environment variables actually behave on POSIX systems).
`--list` is the aggregate view: every distinct key read anywhere,
ranked by read-site count descending. `TARGET` may be omitted only
with `--list`; every other action still requires it.

Only a **literal string** key argument produces a match —
`os.getenv(some_var)` (a variable) and `os.getenv(f"APP_{suffix}")`
(an f-string/template-literal key) are correctly invisible here, not
a bug: a dynamically-constructed key name is genuinely unknowable
without running the code, so no attempt is made to guess it. A
default-value second argument, when present (`os.getenv("PORT",
"8080")`), is never captured or shown — only the key matters for this
command's question. The same key read via two different call idioms
in one file (`os.getenv("PORT")` in one function, `os.environ["PORT"]`
in another) correctly surfaces as two distinct rows, so an agent can
see the idiom difference without a second lookup.

`env` is **CLI-only** (no MCP tool) — an even narrower, single-purpose
scope than `throws`/`catches`'s own CLI-only pilot; revisit if a
recurring "which env vars does this service read" need surfaces for
an MCP-only agent session.

## Interpreting `dekko query cohesion`

`cohesion` answers "if I'm splitting this file in two, which symbols
naturally group together" — but only partially, and it says so in its
own output. It groups a file's symbols into **connected components**
over intra-file calls/references only (edges where both the caller/
referrer and the callee/referenced symbol are defined in the same
file); this is **connectivity, not clustering** — a deliberately weak
signal, not a "which functions belong together" recommendation.

```sh
dekko query cohesion src/app/big_module.py         # intra-file coupling summary
dekko query cohesion src/app/big_module.py --json
```

`FILE` is matched the same way as `query file` — exact repo-relative
path, or any unambiguous trailing path suffix. Symbols with **no**
intra-file call/reference edge to any other symbol in the file are
reported separately as `isolated`, not as their own one-member
"component". Every run always prints this disclosure, in both text
and JSON output, never dropped by `--budget`/`--limit` capping:

> note: this groups symbols that are mutually reachable, not symbols
> that are tightly coupled vs. loosely coupled — a file that's one
> connected component (the common case) gets no useful split
> suggestion from this view. Real "which functions belong together"
> clustering is not implemented.

That note is load-bearing, not decorative: **most non-trivial files
are a single connected component**, and for those, `cohesion` gives
**zero** useful split signal — this is the expected, common case, not
a bug. Real community-detection/modularity-style clustering (which
would give a genuinely useful answer even for a fully-connected file)
is a materially harder algorithm dekko has no other precedent for and
does not implement; see
`.features/plans/post-indexing-tooling/symbol-cohesion-clustering-design.md`
for the full reasoning and what a future "real clustering" version
would require.

`cohesion` is **CLI-only** (no MCP tool) — this is a human refactor-
planning aid, not something an agent typically needs mid-task, and the
weak-signal caveat above makes it a poor fit for always-loaded MCP
schema rent regardless.

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

Once running, every read-only subcommand (`query`, `deps`, `search`,
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
