# Changelog

All notable changes to **dekko** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Dates are when the work landed on `develop`; releases are cut by pushing a
`v*` tag.

## [Unreleased]

## [0.43.29] — 2026-08-28

### Added
- **`dekko query throws`/`catches` gain a `--lang` filter** — cuts
  cross-language noise (e.g. JS catch-alls polluting a Java-only
  query on a mixed-language repo like spring-boot) by restricting
  results to a single language, derived from the same language
  registry `outline`/`search` use. Text and JSON output both disclose
  when results were filtered (`lang_filtered_out` / a mismatch note).
  Also fixes `catches`' default sort to put exact-type matches before
  catch-alls. See
  `.features/plans/round23/28-lang-filter-throws-catches.md`.

## [0.43.28] — 2026-08-28

### Fixed
- **`dekko query callers/callees --sites` footer clarifies callers vs.
  sites** — the text footer's TOTAL used to count call *sites* while
  plain-mode TOTAL counts distinct *callers*, and neither the text
  footer nor `--json --sites`'s `meta.total` exposed both numbers,
  making legitimate divergence (one caller invoking a symbol several
  times) look like a truncation bug. The footer now reports `N
  callers`/`N callees` alongside an explicit sites count, and JSON
  output adds `meta.sites_total`. See
  `.features/plans/round23/26-sites-footer.md`.

## [0.43.27] — 2026-08-28

### Added
- **`dekko sanity` flags receiver-mismatch false confidence** — a
  grep-only hit for a single-repo-candidate method target is now
  classified `likely_unrelated_external` (instead of the misleading
  `CAUSE_TEST_FILTER`/`CAUSE_GENERIC_NAME`) when neither the hit's
  line nor its file's imports mention the target's declaring type,
  fixing the false-confidence case where an unrelated same-name method
  from another library (e.g. spring-boot's `isTrue`/AssertJ) reads as
  a real reference. Scoped to `sanity <target>`; `--all` does not
  apply this cue. See
  `.features/plans/round23/25-sanity-receiver-mismatch-cue.md`.

## [0.43.26] — 2026-08-28

### Added
- **`dekko sanity --all`** — a repo-wide sanity sweep: runs the same
  callers/uses cross-check `sanity <target>` does, but across every
  symbol with nonzero fan-in in the map, not just a human-picked
  target. Supports `--jobs` for thread-pool parallelism, `--max-names`
  to cap sweep size, and `--fail-on-unexplained` as a CI gate that
  exits nonzero if any grep-only miss can't be classified. Mutually
  exclusive with `--unused`. Closes the gap where a classification
  regression (like the multiline-import bug from round 23) could sit
  undetected in `develop` because `sanity` only ever ran when someone
  happened to pick the right target. See
  `.features/plans/round23/24-sanity-all-sweep.md`.

## [0.43.25] — 2026-08-27

### Added
- **`dekko sanity --unused NAME`** — a new sanity mode for the flip
  side of `sanity`'s usual callers/uses cross-check: given a symbol
  `dekko unused` flagged as dead, grep-sweep its bare name and report
  every hit outside its own definition/import/comment as reference
  evidence, classified (spread/typeof/subscript/call/other). Catches
  the class of false positive where a symbol is genuinely referenced
  but not via a shape the call-graph walk recognizes, in any language
  — not just the TS-specific spread/typeof/subscript fix already
  shipped for `unused` itself. Mutually exclusive with `--usages`;
  always exits 0 (advisory). See
  `.features/plans/round23/23-sanity-unused-variant.md`.

## [0.43.24] — 2026-08-27

### Added
- **`dekko unused` now caveats C/C++ results with a note that exported/
  `extern "C"` symbols may be consumed outside this repo's call graph**
  (e.g. via Go/Swift/Python bindings calling through a compiled `.so`)
  and top hits on a public C API should be treated skeptically. Text
  and `--json` (`caveats` field) both carry it. This is layer 1 of a
  two-layer design; a deferred `--exclude-c-abi` flag that actually
  correlates `extern "C"` header declarations to `.cc` definitions is
  not yet implemented. See
  `.features/plans/round23/22-unused-extern-c-caveat.md`.

## [0.43.23] — 2026-08-27

### Added
- **`dekko unused --suspect`** — flags "unused" results that are kept
  alive only by fan-in from a call-graph edge whose bare name also
  collides ambiguously elsewhere in the repo, the exact shape that can
  hide a genuinely dead symbol behind a misattributed call (see the
  round-23 n=1-candidate resolver-confidence findings). Built on a new
  `ambiguous.collision_names()` helper and `unused.find_suspects()`,
  CLI-only for now. See
  `.features/plans/round23/21-unused-ambiguous-crossref.md`.

## [0.43.22] — 2026-08-27

### Added
- **A standing "this repo's ambiguous rate is unusually high" flag** —
  on repos where a large share of call-site resolution collapses to
  "ambiguous" (short/generic names colliding across many candidates),
  dekko now proactively surfaces that instead of requiring a separate
  `dekko ambiguous` call to discover it. A shared
  `ambiguous.cheap_rate()`/`high_rate_note()` helper feeds a line in
  `dekko summary` (and therefore `orient` and the MCP `summary` tool),
  a note in the Claude Code session-start hook preamble, and a new
  advisory finding in `dekko doctor`. The rate is stamped into
  `provenance` at `dekko map` write time so `doctor` can report it
  without loading the full map. See
  `.features/plans/round23/20-standing-ambiguous-rate-flag.md`.

## [0.43.21] — 2026-08-27

### Fixed
- **`dekko status`/`dekko status --json`/`dekko doctor` collapsed
  `tool_version` and `spec_hash` staleness into one generic "stale"
  message**, printing a self-contradictory `built by dekko X, running
  X` when only `spec_hash` had drifted (e.g. a long-lived MCP server
  reinstalled underneath it mid-session) — the MCP `map_status` tool
  already disambiguated this in round 09, but the fix was never
  ported to the CLI surface. All three now share
  `mapfile.describe_version_stale()`, and `dekko status --json` gains
  `version_stale`/`spec_stale`/`built_spec_hash`/`running_spec_hash`
  fields (gated behind `reason == "version"`, so the common-case JSON
  shape is unchanged). See
  `.features/plans/round23/11-cli-status-doctor-staleness-disambiguation.md`.
- **MCP `refresh_map` re-synced a stale server process to its own old
  code, not to what's on disk** — calling `refresh_map` from inside a
  long-lived `dekko serve` process that's stale on `spec_hash` and/or
  `tool_version` re-extracts using that same process's stale
  in-memory extractor code and self-consistently re-stamps "fresh,"
  silently corrupting a map that a fresh CLI `dekko map` may have
  already built correctly. `map_status`'s suggested next step for a
  `reason == "version"` verdict now says "restart the dekko MCP
  server process" instead of "call refresh_map" (which can't fix
  this from inside the same process either way), and `refresh_map`'s
  response now discloses a restart caveat whenever this process's own
  pre-regen freshness check showed it was already the stale party.
  See `.features/plans/round23/12-refresh-map-stale-process-resync.md`.
- **`dekko daemon status` could intermittently (~1/6) report "not
  running" immediately after `dekko daemon start` printed "started"**
  — `start()` returned the instant the child process was spawned,
  before the child had necessarily finished binding its listening
  socket, letting an immediately-following `status` call race
  `transport.exists()` into a false negative. `start()` now polls
  (bounded, ~3s cap) for confirmation the child has actually bound
  before returning; on the rare case the cap is hit, it prints a
  distinct "spawned but unconfirmed" message instead of falsely
  claiming "started," and still returns exit `0`. See
  `.features/plans/round23/13-daemon-status-false-negative.md`.
- **`run_pooled_with_retry`'s one bounded `BrokenProcessPool` retry
  fired with zero delay**, giving it a real chance of landing in the
  same transient window (CPU contention, or a `uv tool install
  --reinstall` shim relink race) that caused the first failure — a
  `dekko map --full --jobs 0` run immediately after a reinstall could
  see the retry itself also fail. Added a fixed 1.5s backoff before
  the retry attempt. See
  `.features/plans/round23/15-brokenprocesspool-transient-crash.md`.
- **Concurrent bare-CLI commands against the same repo silently
  serialized with zero feedback** — a command waiting on another
  process's advisory regen lock (`.dekko/regen.lock`), and the
  fallback path that launches an independent regen after the wait cap
  is hit, both printed nothing, reading as indistinguishable from a
  hang on a large repo. Both paths now print a one-line `note:` to
  stderr (mirroring round 15's `_maybe_warn_sequential` pattern) —
  pure disclosure, no behavior change. The exact tensorflow-scale
  timing this was found against involves an unconfirmed, separately
  unlocked cold-rev-cache path (`diff.py::old_snapshot`) that this fix
  does not change; see
  `.features/plans/round23/14-concurrent-cli-silent-serialization.md`
  for what's confirmed vs. deferred.

## [0.43.20] — 2026-08-27

### Fixed
- **`dekko unused` false-flagged Rust trait-dispatched methods
  (`Display::fmt`, `From::from`, `Iterator::next`, operator overloads,
  etc.) as dead code** — implicit trait dispatch (`{}`/`.to_string()`,
  `.into()`/`?`, `for`, `+`/`==`/indexing) never produces a
  `call_expression` node, so these methods always had zero explicit
  callers. `unused.py` now consults the already-resolved
  `heritage_external_out` evidence: a Rust method whose enclosing type
  implements a curated standard-trait allowlist (`_RUST_STD_TRAIT_
  NAMES`) is treated as a root. Type-level, not per-impl-block —
  a genuinely dead inherent method sharing a type with a std-trait
  impl can still be missed; see
  `.features/plans/round23/03-rust-trait-dispatch-unused-false-
  positive.md`.
- **`dekko unused` false-flagged TypeScript `const`s referenced only
  via object-spread (`{...x}`), a `typeof` type query
  (`type X = typeof y`), or bracket subscript (`obj[x]`)** — none of
  the three shapes were covered by `_JS_REFERENCE_BASE`. Added
  `(spread_element (identifier) @ref)` and `(subscript_expression
  object: (identifier) @ref)` to the shared JS/TS/TSX base, and a
  TypeScript/TSX-only `(type_query (identifier) @ref)` fragment (kept
  separate since `type_query` doesn't exist in the plain JS grammar).
  See `.features/plans/round23/06-ts-unused-spread-typeof-subscript.md`.

## [0.43.19] — 2026-08-27

### Fixed
- **Resolver's single-repo-wide-candidate fast path guessed a
  builtin/stdlib/third-party method call into a same-named repo
  symbol's fan-in with no arity/receiver check** — confirmed live as
  ~1,100x fan-in inflation on spring-boot's AssertJ `.isTrue()` calls
  (1,103 reported vs. 1 real) and cline's `Date.now()`/`Map.has()`
  calls (404/436 misattributed sites vs. 0 credible). Extended
  `_is_noise_call`'s denylist mechanism: added `has`/`now` to
  `_BUILTIN_METHOD_NAMES`, added a new `_JAVA_ASSERTION_METHOD_NAMES`
  set (AssertJ/JUnit/Hamcrest chain terminals: `isTrue`, `isEqualTo`,
  `hasSize`, `contains`, ...), and a new `_BUILDER_METHOD_NAMES` set
  scoped to just `build` (the confirmed spring-boot repro) rather than
  the originally proposed broader `of`/`from`/`with` set, dropped
  after finding real collisions in this repo's own test fixtures and
  because `from` in particular is Rust's own `impl From<X> for Y`
  convention name, too common a legitimate repo-defined method to
  safely denylist repo-wide. A structural arity-aware layer (comparing
  candidate parameter count against call-site argument count) remains
  a documented follow-up — the extractor doesn't capture argument
  counts yet, which is a larger, separate change (round23 issue 01,
  see `.features/plans/round23/
  01-resolver-single-candidate-false-confidence.md`).
- **`dekko subtypes` left ~41% of Rust `impl Trait for X` clauses
  stuck in "ambiguous," and a same-crate-named collision (a real
  in-workspace crate plus an unrelated same-named vendor/fixture
  directory elsewhere in the repo) resolved a coin flip's worth of the
  time to the *wrong* crate's same-named symbol, silently** —
  confirmed live against zed's `Render` trait: `crates/gpui` and a
  synthetic lint-test-fixture directory both named `gpui` collide on
  the crate-name convention `_rust_crate_roots_index` uses, and which
  one won was a genuine 50/50 split across process hash seeds (167
  vs. ~1 correctly-resolved `Render` impls depending purely on
  `PYTHONHASHSEED`). Two fixes: (a) `_import_match` now also tries a
  Rust heritage clause's bare `receiver` segment directly as a
  crate-name hint when the ordinary `file_imports`-derived hint list
  comes up empty, covering the fully-qualified `impl gpui::Render for
  X` spelling (no `use` statement to build a hint from, previously
  never reaching the crate-root fallback at all); (b) a new
  collision-aware `_rust_crate_roots_index_all` (crate name → every
  matching root directory, not just the last one indexed) used only
  by heritage resolution, converting the previous silent coin flip
  into a deterministic, honest `heritage_ambiguous` for a genuine
  same-named-crate collision — trading the coin flip's lucky-draw
  resolved count for eliminating its unlucky draw's silent wrong
  answers, matching the resolver's own "report as ambiguous rather
  than guessed" design philosophy.
  `resolve_imports()`'s unrelated `use`-resolution path keeps the
  original single-root `_rust_crate_roots_index` unchanged (round23
  issue 09, see `.features/plans/round23/
  09-subtypes-ambiguous-resolution-rate.md`).

## [0.43.18] — 2026-08-27

### Fixed
- **`dekko sanity`'s multi-line destructured-import detection defeated
  by any earlier import statement in the 20-line lookback window** —
  `_looks_like_multiline_import_member`'s flat `any()`/`any()` scan
  for an "opener anywhere" and a "closer anywhere" let an unrelated,
  already-closed earlier import's `}` falsely "close" a genuinely
  still-open block sitting directly above the hit, as soon as the
  window contained both — the common case on any real, import-heavy
  file, not the edge case (13 of 21 grep-only rows misclassified as
  `CAUSE_UNEXPLAINED` on claude-buddy). Replaced with a single
  backward walk from the hit toward the top of the window that
  answers based on the *nearest* brace-relevant line only, checking a
  `}` before an opener match on the same line so a complete
  single-line import isn't misread as a dangling opener (round23
  issue 04).
- **`dekko sanity --json` silently truncated its `matches`/
  `dekko_only`/`grep_only` row arrays at `DEFAULT_REPORT_LIMIT` (200)
  with no disclosure anywhere in the output** — unlike `query --json`,
  which already surfaces a `meta` block (`Meter.as_dict()`) whenever a
  budget/limit cap trims a result set. `_fit_rows()` now returns the
  `Meter` it was already discarding instead of a bare `int`, threaded
  through into a new top-level `meta` object (one `Meter.as_dict()`
  per bucket, same shape `query --json` already uses). `counts` is
  unchanged and still present for back-compat; `meta` is purely
  additive (round23 issue 05).

## [0.43.17] — 2026-08-27

### Fixed
- **`dekko query type --exact`'s not-found path echoed the query
  itself back as its own "closest match" suggestion** — a verbatim
  (case-sensitive) self-match offered nothing new, since it's exactly
  the string that already failed to match. `_close_names()` gained an
  opt-in `exclude_verbatim` guard, now used by the four not-found
  paths (`type`, `env`, `uses`/`external`, `importers`) whose needle
  is the literal failed query string; the general symbol-not-found
  suggester (`_suggest_symbols`) keeps the old behavior, since its
  needle is a *derived* bare qualname where a verbatim match is the
  intended "right name, wrong path" suggestion, not an echo (round23
  issue 16).
- **`dekko query env --list`'s text footer TOTAL was off by one**
  when results were truncated — the summary header line was folded
  into the counted/droppable row list instead of passed through
  `_emit_lines()`'s `prefix` parameter, inflating `Meter.total` by
  one. Routed through `prefix=header` like every other call site in
  `query.py` already does; JSON output was already correct and is
  unchanged (round23 issue 17).
- **`dekko query callers/callees --json --sites` dropped per-site
  line numbers for module-level pseudo-callers** that text output
  already shows — `module_level` was a flat `list[str]` of bare
  paths, never consulting the recorded `edge_lines` the text renderer
  (`_module_rows`) already used. `module_level` is now a `list[dict]`
  (`{"path": ..., "lines": [...]}`, `"lines"` omitted when no site
  line was recorded), built via new shared helpers
  `_module_site_lines()`/`_module_level_entries()`. This is a
  breaking JSON schema change for any external consumer of
  `module_level`. `sanity.py`'s `_dekko_hits_callers()` was updated
  to fold lined module-level entries into its `hits` set instead of
  leaving them in the line-less "no line info" bucket, fixing the
  cascading false "unexplained miss" this caused in `dekko sanity`
  (round23 issue 10).

## [0.43.16] — 2026-08-27

### Fixed
- **MCP `get_context_pack` silently dropped the "N ambiguous, not
  counted" disclosure** that `get_callers`/CLI `query callers` both
  show — `contextpack.py` never read `index.ambiguous_in`/
  `ambiguous_out`, so a caller list could look fully resolved when
  hundreds of same-named call sites were actually dropped (round23
  issue 07).
- **`dekko query symbol` mislabeled its ambiguous-call note as
  outgoing when it was incoming, and never showed the real outgoing
  count** — the fan-line's `(+N ambiguous call sites not counted)`
  note was always the *incoming* ambiguous count but read as
  qualifying `fan-out`; it now attaches to `fan-in`, and the real
  `fan-out`-qualifying outgoing-ambiguous count is computed and shown
  alongside it, in both text and JSON (round23 issue 08). Both fixes
  share a new `ambiguous_counts()` helper in `query.py` so the
  incoming/outgoing counts are computed identically everywhere.

## [0.43.15] — 2026-08-25

### Added
- **`dekko unused --top`** — alias for `--limit`, matching the
  `--top` flag `stats`/`ambiguous`/`deps` already use for a
  ranked-list size, so the habit carries over to `unused` too.

### Changed
- **`dekko query importers`'s not-found message now hints at `deps
  --file`** when the needle looks like a file path rather than an
  import-source string (`org.foo.Bar`, `./utils`) — `importers`
  matches the latter, not the former, and the two commands were easy
  to reach for interchangeably.

## [0.43.14] — 2026-08-25

### Fixed
- **`dekko unused` false-flagged Python callback/dispatch-table
  values as dead code** — Python had no `reference_query`, so a
  function passed by bare name and never itself called at that site
  (a keyword-argument value, positional call argument, dict/list/
  tuple/set element, assignment/default-parameter right-hand side, or
  bare `return` value — e.g. `check_success=valid_ndk_path`) was
  structurally invisible to the call-expression-only `call_query`
  (round 22 tensorflow.md §6). A new `_PY_REFERENCE_QUERY`, mirroring
  `_JS_REFERENCE_BASE`'s identical JS/TS shape, is now wired into
  `PYTHON`'s `LanguageSpec`.

## [0.43.13] — 2026-08-25

### Fixed
- **`query subtypes`/`query supertypes` dropped a Rust trait
  implementor to `heritage_ambiguous` when the trait was only
  reachable through a crate-root re-export** — `impl Render for
  Editor` (`use gpui::Render;`, where `Render` is actually declared
  in `gpui`'s `element.rs` and surfaced at the crate root via `pub
  use element::*;`) fell through `_import_match`'s `_module_matches`
  check, since that check only ever compares an import source
  against a candidate's own declaring-file stem, never the crate it
  re-exports through — round 22 zed.md §3.2. A new
  `_rust_crate_hint_matches`, reusing item 5b's
  `_rust_crate_roots_index`, is threaded through
  `resolve_heritage()` → `_resolve_one_heritage` → `_pick_candidate`
  → `_import_match` as a crate-aware fallback: does the hint's
  leading segment name a known crate, and does the candidate live
  under that crate's root. Scoped to heritage resolution only
  (`resolve()`'s call/ref path is unchanged). One documented residual
  gap: two same-named crates in the repo (an in-workspace one
  shadowed by an unrelated same-named fixture/vendor crate) can still
  collapse onto the wrong one.

## [0.43.12] — 2026-08-25

### Fixed
- **`dekko sanity` misfiled bare names inside multi-line destructured
  imports as `CAUSE_UNEXPLAINED`** — `_looks_like_import_statement`
  only recognizes the single-line `import { X } from "...";` shape;
  a multi-line `import {\n  X,\n  Y,\n} from "...";` block puts the
  bare-name hit on a line with none of `import`/`{`/`from` on it,
  so the anchored check never matched (round 22 claude-buddy.md
  §2.4 — the dominant "grep-only" shape there, 6 of 8 flagged rows).
  A new `_looks_like_multiline_import_member` scans a small window
  above the hit line for an unclosed `import {` opener and routes a
  match to `CAUSE_IMPORT_STATEMENT`.
- **`dekko sanity`'s own-definition-line exclusion only covered the
  query target itself** — `run()`'s `near_own_definition` check used
  a single `own_def_loc`, so a grep hit landing on an unrelated
  same-bare-named symbol's own definition line (e.g. a different
  class's `new_internal`) wasn't excluded and could still misfire.
  `own_def_loc` is now `own_def_locs`, a frozenset covering every
  symbol sharing the target's bare name via `symbols_by_name`.
- **`dekko query`'s module-level pseudo-caller rows lost per-site
  line numbers unless `--sites` was passed** — `_module_rows` gated
  its per-line lookup on the `sites` flag to match `_site_rows`'s
  named-caller default, but a module-level "path (module level)" row
  with several distinct anonymous-callback call sites in the same
  file is ambiguous in a way the named-caller default isn't, and the
  per-line data was already recorded in `index.edge_lines` regardless
  of the flag. `_module_rows` now always attempts the per-site lookup,
  falling back to the bare form only when no site line was recorded.

## [0.43.11] — 2026-08-25

### Fixed
- **`dekko context`/`dekko query`'s importer listing showed the
  resolver-internal `module/name` encoding instead of the real
  import source** — JS/TS multi-name imports (`import { join } from
  "path"`) are encoded internally as `"module/name"` per binding to
  disambiguate named/default/namespace imports during resolution,
  but that encoding was leaking straight into human-facing output.
  `contextpack.py` and `query.py` now derive the bare module
  specifier (`bare_import_source`) for display, threaded through a
  new `Pack.language`/`_importers_row`/`_importers_entry` `language`
  parameter.

### Added
- **`dekko map --force`** — a subpath-scoped `dekko map` run (e.g.
  `dekko map src/`) at the default `.dekko/` location used to
  silently overwrite an existing full-repo map with a narrower one,
  with no warning that most of the repo had just dropped out of the
  map. `dekko map` now refuses that overwrite by default; `--force`
  opts back into the old silent-overwrite behavior for anyone who
  wants it deliberately.

### Changed
- **JS/TS caveat note in `dekko query` output is now conditional on
  the repo actually containing JS/TS** — it previously printed
  unconditionally, showing up (confusingly, with nothing to caveat)
  on Go/Python/C++-only repos.

## [0.43.10] — 2026-08-25

### Fixed
- **Rust resolver couldn't follow `crate::X` into a custom-named
  crate root, or resolve cross-crate `use other_crate::X;` imports at
  all** — `_resolve_import_rust` only tried the fixed `lib.rs`/
  `main.rs`/`mod.rs` index names, so a `[lib] path = "src/gpui.rs"`
  crate's own root-scope items were unreachable via `crate::` (round
  22 zed.md §3.1: `crate::App` never resolved), and any bare crate
  name with no `crate`/`self`/`super` prefix was assumed external by
  construction — true for real third-party dependencies, but also
  swallowing genuine in-workspace sibling-crate imports. A new
  `_rust_crate_roots_index()` builds a repo-wide crate-name → crate-
  root index (reusing round 19's own directory convention), threaded
  into resolution via a new `crate_roots` field on
  `_ImportResolveContext`; `_rust_crate_root_index_names()` extends
  the index-name search with a crate's own custom root filename when
  one exists.

## [0.43.9] — 2026-08-25

### Fixed
- **`dekko ambiguous` misfiled builtin-method noise as genuine
  ambiguity** — `_pick_candidate`'s noise guard (`_is_noise_call`,
  round 21) rejected a receiver-qualified call to a well-known
  built-in method name (`trim`, `describe`, `.then()`, ...) by
  returning `None`, the same value used for "genuinely ambiguous,
  2+ real candidates." The caller couldn't tell the two apart, so a
  noise-suppressed call — even with exactly one real candidate —
  was unconditionally recorded as ambiguous, inflating `dekko
  ambiguous`'s reported rate ~2-3x on JS/TS repos (cline: 1,403
  `trim` sites, 0 real). A new `_NOISE` sentinel now distinguishes
  the two outcomes; noise-suppressed calls route to `external`
  instead. Also widened `_BUILTIN_METHOD_NAMES` with `get`,
  `resolve`, `create` (confirmed leaking through with inflated
  `avg_candidates` — `get` averaged 32.0 in one report, almost
  certainly `Map.get()`/`Promise.resolve()`/`Object.create()` noise).
- **`affected`/`workset` false-positive impacted tests on a Node
  builtin module-name collision** — `_module_matches()` matched a
  bare (non-relative) JS/TS import source against any repo file
  whose stem happened to collide, with no awareness that names like
  `path`, `fs`, `os`, `util` are Node core modules, not local files.
  `import { join } from "path"` was matching a repo's own
  `server/path.ts`, falsely marking every unrelated importer of
  Node's real `path` module as impacted by a change to that file —
  the single most-repeated correctness gap in three consecutive eval
  rounds. A new `_NODE_BUILTIN_MODULE_NAMES` denylist, checked
  against the bare module portion of the import source (accounting
  for `extractor._imports_js`'s `"module/name"` encoding for named/
  default/namespace imports), now excludes this match; genuine
  relative imports (`"./path"`) and non-JS/TS candidates are
  unaffected.
- **Heritage `subtypes`/`supertypes` lost same-named C/C++ base
  classes to `ambiguous`** — `resolve_heritage()` never built or
  threaded a calling file's whole-file `#include` list (`raw_imports`)
  into `_pick_candidate`, unlike `_resolve_files_chunk`'s call-
  resolution path. For C/C++, that whole-file-include fallback is
  the *only* signal available to disambiguate a same-named base class
  in a large tree (e.g. `query subtypes OpKernel` surfaced 1 of
  ~800+ real subtypes on tensorflow, the other 828 dropped into
  `ambiguous`). `resolve_heritage()` now builds `raw_imports` the
  same way call resolution does and threads it through
  `_resolve_one_heritage()` into `_pick_candidate()`.

## [0.43.8] — 2026-08-24

### Fixed
- **`dekko map --jobs 0` hang past the 600s pool-stall timeout under
  concurrent load** — every `ProcessPoolExecutor` call site (four in
  `core/resolver.py`, one in `repo_ops.py`) used
  `with ProcessPoolExecutor(...) as pool:` around a
  `future.result(timeout=POOL_RESULT_TIMEOUT_S)` loop. When that
  `.result()` call timed out on a genuinely wedged worker (e.g. a
  spawned worker that resolved the wrong Python interpreter), the
  `TimeoutError` unwound out through `__exit__`, which
  unconditionally calls `shutdown(wait=True)` — blocking
  indefinitely on the very wedged worker the 600s bound exists to
  stop waiting for. A round-22 7-repo eval reproduced a 14:53
  wall-clock hang (well past the documented 600s bound) under
  genuine concurrent CPU contention. Every call site now owns its
  pool via `pool = ProcessPoolExecutor(...)` / `try`/`finally:
  pool.shutdown(wait=False)` instead of `with`, and a new shared
  `resolver._run_pool_bounded()` helper shuts the pool down without
  waiting and force-kills any still-alive worker on a timeout before
  re-raising, so the documented timeout now actually bounds the
  call's wall-clock time.

## [0.43.7] — 2026-08-24

### Fixed
- **Resolver cross-family false matches** — `_language_filtered()`
  previously fell back to the *full* unfiltered candidate list when
  no same-language candidate existed in the map index (e.g. because
  the true definition lives under an excluded `third_party/`-style
  directory), letting a same-named symbol in an unrelated language
  win a confident, wrong resolution with no ambiguity disclosure
  (e.g. a C++ `InvalidArgumentError` falsely resolving to an
  unrelated Python class). The fallback is now language-family-aware
  (grouping only genuinely-interoperating languages, e.g. c/cpp,
  javascript/typescript/tsx) and can legitimately return empty,
  so a cross-family miss now fails safe into the existing ambiguous
  bucket instead of silently reporting a wrong fan-in. Legitimate
  cross-language cases (a C header used from C++) still resolve.

## [0.43.6] — 2026-08-24

### Fixed
- **Plugin manifest version drift** — `integrations/claude/.claude-plugin/plugin.json`
  and `marketplace.json` had fallen out of sync with `pyproject.toml`
  (stuck at 0.43.3 across the 0.43.4/0.43.5 bumps). Synced both to the
  current version.

### Added
- **`scripts/sync_plugin_version.py`** — syncs the plugin manifests to
  `pyproject.toml`'s version (no-arg mode), or bumps all three plus
  `uv.lock` together (version-arg mode), so manifest drift can't
  recur on a future release.
- **`sync-plugin-version` pre-commit hook** — runs the sync script in
  check mode, mirroring the existing `uv-lock` hook's shape.
- **Release-workflow manifest check** — `release.yml`'s `build` job
  now verifies the plugin manifests match the release tag before a
  release proceeds.

## [0.43.5] — 2026-08-24

### Fixed
- **`dekko daemon stop` false success** — when the daemon is confirmed
  alive and busy, `stop()` no longer falls through to the same
  `"stopped"` message and exit `0` as a genuine stop. It now reports
  that the daemon is still running and busy and returns a new
  `EXIT_DAEMON_STILL_RUNNING` (8) exit code.
- **Resolver cross-language false matches** — `_pick_candidate()` did
  not filter candidates by language, so a same-named symbol in an
  unrelated language could win a confident wrong resolution with no
  ambiguity flagged, corrupting heritage and `query callers` results.
  Candidates are now filtered by language first, falling back to the
  unfiltered set when that would leave nothing (preserving legitimate
  cross-language cases like C headers used from C++).

## [0.43.4] — 2026-08-24

### Fixed
- **`dekko map --jobs 0` worker-pool hangs** — `run_pooled_with_retry`
  now pins `multiprocessing.set_executable(sys.executable)` before
  every pool attempt and bounds each pooled future with a
  `POOL_RESULT_TIMEOUT_S` (600s) timeout, raising a clear
  `PoolStalledError` instead of hanging indefinitely under concurrent
  load. The MCP server catches `PoolStalledError` alongside the
  existing `BrokenProcessPool` handling.
- **`dekko sanity` truncated-grep false verdicts** — a truncated grep
  sweep (>5,000 lines) no longer silently reports a false `dekko-only`
  count of `0`; it now discloses truncation via `grep_truncated` /
  `dekko_only_note` and suppresses that bucket instead of fabricating
  a verdict.
- **`dekko sanity` unbounded snippets** — rendered snippets are now
  capped at 240 characters (classification still runs against the
  full line); pathological lines (>10k chars) are dropped from
  snippets and counted separately.
- **`dekko sanity` missing import-statement classification** — added
  a `CAUSE_IMPORT_STATEMENT` classifier for ESM/Python/CJS import
  lines, so import-only mentions of a symbol no longer fall into the
  generic "unexplained miss" bucket.

## [0.43.3] — 2026-08-21

### Fixed
- **README logo** — the `<picture>`/`<img>` logo at the top of
  `README.md` used relative asset paths (`assets/logo-full-*.svg`),
  which resolve on GitHub but 404 on PyPI's standalone README render
  (no repo context to resolve against). Switched to absolute
  `raw.githubusercontent.com` URLs pinned to `main` so the logo
  renders correctly on both.

## [0.43.2] — 2026-08-21

### Changed
- **`dekko sanity`** — `classify_miss()` now recognizes a bare-name
  mention in a comment/docstring near a symbol's own definition
  (`CAUSE_COMMENT_MENTION`) instead of falling into the generic
  "unexplained miss" bucket. Uses a per-grammar comment-prefix table
  covering Tier-1 and Tier-2 languages (Vue/Svelte/Astro excluded as
  mixed-content SFCs) gated on two independent signals — proximity to
  the definition and comment-line shape — so a line-wrapped
  multiplication or decrement operator near a definition can't
  misfire as a comment mention.

## [0.43.1] — 2026-08-21

### Changed
- **`dekko-verify` skill** — broadened scope from the two specific
  bugs it originally called out (heritage/throws mislabeling, since
  fixed) to the general failure-shape category they belonged to, so
  the skill still applies when a *different* mislabeling bug surfaces
  in the future. Notes that `dekko sanity` now automates the
  call-graph half of this check, but not the heritage/throws half.

## [0.43.0] — 2026-08-21

### Added
- **`dekko sanity <target>`** / **`/sanity`** — cross-checks a
  `callers`/`uses` result against a scoped, word-bounded `grep`
  sweep, diffing hits into matches/dekko-only/grep-only and naming
  the likely cause of any grep-only miss (qualified call, unsupported
  language, test-filter exclusion, generic name). Automates the
  manual spot check `dekko-verify` already documented, so it's cheap
  enough to run habitually instead of only when a result looks
  suspicious.

## [0.42.0] — 2026-08-21

### Added
- **`dekko-review-context` skill** — orchestrates `workset` +
  `impacted_tests` + `check_ambiguous` to give PR-description and
  code-review flows a structural head start on a diff (what changed,
  what calls it, what tests should run, where the resolver itself is
  unsure) ahead of the not-yet-built `dekko review` command (#14).

## [0.41.0] — 2026-08-21

### Added
- **`dekko doctor`** / **`/doctor`** — unified environment and
  install-state diagnostic. Checks for PATH shadowing (a stale
  globally-installed `dekko` binary resolving ahead of the project's
  intended one — the single most-repeated cause of silent
  wrong/empty answers across past eval rounds), map freshness,
  MCP/plugin registration, whether the MCP server actually starts,
  hook install state, and the `CLAUDE.md` policy block. Each check
  degrades independently to "unknown" on its own failure rather than
  aborting the rest.

## [0.40.6] — 2026-08-21

### Fixed
- **`dekko deps`** — Rust crate-root resolution now recognizes crates
  whose `Cargo.toml` `[lib] path` points somewhere other than
  `src/lib.rs`, falling back to matching a `src/<crate-name>.rs`
  layout. Previously undercounted resolved edges on repos using
  non-standard crate roots (216/222 of zed's crates, for example).
- **`dekko unused`** — recognizes Java method-reference syntax
  (`this::method`, `Class::method`) as a use site, so methods only
  reached that way are no longer false-flagged as dead code.
- **`query supertypes`/`subtypes`** — same-file TypeScript type
  aliases used with `implements`/`extends` now resolve correctly
  instead of being mislabeled `(external)`. Adds type-alias
  extraction for TS/TSX and bumps `MAP_DOC_VERSION` 9→10.
- **`--claude-md-uninstall`** — deletes `CLAUDE.md` when removing the
  dekko usage block leaves nothing behind, instead of leaving a
  0-byte file.

## [0.40.5] — 2026-08-20

### Fixed
- **`.h` header files** — now content-sniffed to disambiguate C vs.
  C++ (checking for `class_specifier`/`namespace_definition`/
  `template_declaration` nodes) instead of always parsing as C, which
  silently mis-resolved heritage/call edges on large C++ codebases
  using the `.h` convention (LLVM, gRPC, Chromium-style, TensorFlow).
  Existing `.dekko/` caches self-heal on the next `dekko map` via a
  fingerprint bump, no `--full` required.
- **`dekko unused`** — no longer false-flags module-level `const`
  variables that are read as binary/ternary operands rather than
  called.
- **`query supertypes`/`subtypes`** — in-repo type aliases used with
  `implements` that can't be resolved are now labeled `(unresolved)`
  instead of the misleading `(external)`.
- **`query throws`** — recognizes Java `instanceof`-pattern-bound
  rethrow variables instead of mislabeling them as a fake external
  type.
- **`dekko query catches`** — dropped its hardcoded Rust/Go/C
  exclusion note in favor of reflecting the languages actually present
  in the scanned repo.
- **`mapfile`** — files dropped by the 1MB size cap are now disclosed
  instead of silently omitted from the map.
- **`affected`/`workset`** — fixed a cold-resolve note overstating the
  file count via a git-tracked-vs-mapped count mismatch.

## [0.40.4] — 2026-08-19

### Changed
- **Call/ref/throws/catches resolution parallelism** — `resolver.py`'s
  `ProcessPoolExecutor` passes now use oversubscribed chunking
  (submitted to the pool's task queue for dynamic rebalancing instead
  of exactly one static chunk per worker) plus a shared-index pool
  initializer, cutting run-to-run variance on heterogeneous-core
  machines from ~2.2x-3.9x swings to a tight ~3% spread at a
  consistent ~3.3x-3.5x parallel speedup.

## [0.40.3] — 2026-08-19

### Added
- **`dekko query throws`/`dekko query catches`** — Rust/Go/C exclusion
  is now disclosed in-CLI rather than only in docs: `throws` prints a
  distinct message (and `language_supported: false` in `--json`) when
  the target symbol's language has no syntax-level exception concept;
  `catches` notes how many repo files were excluded from its scan for
  the same reason (`language_coverage` in `--json`).

## [0.40.2] — 2026-08-19

### Fixed
- **MCP server / process pools** — every `ProcessPoolExecutor` call
  site (`repo_ops` extraction; `resolver`'s calls, refs, throws, and
  catches resolution) now retries once at a reduced worker count on
  `BrokenProcessPool` instead of surfacing an opaque crash under
  contended-core load, with a disclosure note on retry and an
  actionable MCP-level error message if the retry also fails.

## [0.40.1] — 2026-08-19

### Fixed
- **`dekko deps`** — self-import false positives, ambiguous `--file`
  matches, and env-write detection (D1, D2, E1); NodeNext/ESM-style
  relative TS imports (specifier carries a compiled `.js` extension,
  source is `.ts`) now resolve correctly.
- **`dekko query importers`** — `--exact` matching fixed; JS/TS
  side-effect and namespace imports now resolved (I1, I2).
- **`dekko query catches`/`throws`** — false positives fixed (T1, T2);
  `--transitive`'s "N of TOTAL omitted" truncation footer no longer
  miscounts header lines as data rows; both passes now resolve in
  parallel across workers like the existing calls/refs passes.
- **`dekko query peers`** — no longer mislabels a symbol as a leaf
  function when its only outgoing call resolved ambiguously rather
  than being genuinely absent.

## [0.40.0] — 2026-08-18

### Added
- **`dekko deps` — module-level dependency graph.** File-to-file
  import graph resolved from raw `import`/`use`/`#include` source
  text (full resolution for Python, JS/TS/TSX, Rust, Java, C/C++; Go
  imports always external, undocumented `go.mod` prefix). `--file`
  shows one file's resolved imports/importers/external sources,
  `--cycles` reports circular-import clusters via Tarjan's SCC,
  `--top` widens the most-depended-on ranking, `--export
  {mermaid,dot}` reuses `export.py`'s existing renderers. CLI-only, no
  MCP tool.
- **`dekko query importers`/`dekko query peers` — shared-dependency
  and co-usage lookups.** `importers SOURCE` is a reverse, raw-import-
  text match (substring by default, `--exact` for the literal
  string) — "what else imports the same thing as X," distinct from
  `deps --file`'s cross-language-resolved answer. `peers SYMBOL` finds
  other symbols sharing at least `--min-shared` (default 2) callees
  with the target, ranked by shared-callee count, each row naming the
  shared callees. CLI-only, no MCP tool.
- **`dekko query throws`/`dekko query catches` — exception/error-flow
  tracing.** `throws SYMBOL` traces raise/throw sites one level deep
  by default, `--transitive --depth N` walks the call graph outward;
  `catches TYPE` scans every catch clause repo-wide for an exact-name
  or catch-all match. A scoped pilot: full support for Python/Java/
  C++, `throws`-only for JS/TS (`catches` is a disclosed weak signal
  there), Rust/Go/C permanently excluded (no syntax-level exception
  concept to extract, not a future gap). CLI-only, no MCP tool.
- **`dekko query env` — static env-var read tracing.** Detects
  `getenv`-shaped call sites (`os.getenv`, `process.env.X`,
  `System.getenv`, `std::env::var`, `os.Getenv`, bare `getenv`) across
  all 9 Tier-1 languages. Exact-match only, no data-flow or config-file
  (YAML/JSON/TOML/`.env`) tracing — explicitly out of scope.
  `--list` ranks every distinct env-var name read anywhere by
  read-site count. CLI-only, no MCP tool.
- **`dekko query cohesion FILE` — intra-file symbol-cohesion
  clustering.** Groups a file's symbols into connected components
  over same-file call/reference edges (Union-Find); isolated symbols
  reported separately. A deliberately weak "mutually reachable"
  signal, not real modularity-style clustering — every run prints a
  non-droppable disclosure note to that effect, since most non-trivial
  files come back as one single connected component with zero useful
  split signal. CLI-only, no MCP tool.
- **`dekko unused --kinds {callables,types,all}` — dead-type
  detection.** Extends `unused` to classes/interfaces/enums/structs/
  records/traits, counting heritage (`extends`/`implements`) and
  type-usage (parameter/return-type) evidence alongside existing
  call/reference evidence, so a class only ever constructed or
  extended isn't misflagged as dead. Default (`callables`) behavior is
  unchanged; `all` unions both kinds with a per-kind subtotal. No MCP
  change (`find_unused` was already CLI-only).
- **`dekko workset --symbol NAME --type-impact` — combined
  blast-radius report.** Widens `workset`'s touched set beyond a
  type target's direct callers to include every type-usage site
  (parameter/return type) and every transitive implementor — the
  union of call-graph, type-usage, and heritage impact in one call.
  No-op on a non-type target; requires `--symbol` (rejected with a
  rev diff). The only feature in this batch exposed via MCP (the
  `workset` tool's `type_impact` boolean).

## [0.31.4] — 2026-08-17

### Fixed
- **MCP server crash on newer `map.json` format made opaque instead
  of clear.** A long-lived `dekko serve --mcp` process running
  pre-id-interning code would raise a bare `TypeError` when it read a
  v5 `map.json` whose `caller`/`callee` fields are now interned ints
  instead of strings, surfaced to callers as an unhelpful "internal
  error". `mapfile.load_map()` now raises `MapFormatTooNewError` when
  the doc's `version` exceeds `MAP_DOC_VERSION`, and
  `server.py`'s `_handle_tools_call()` catches it with a message
  telling the caller to restart the MCP server.
- **Malformed `map.json` `version` field (`null`, string, float,
  bool) fell through the above guard** and still hit the old opaque
  `TypeError`. Added a distinct `MapFormatInvalidError` — "restart
  the server" is the wrong advice for a corrupted doc — pointing the
  caller at `dekko map` instead.

## [0.31.3] — 2026-08-14

### Changed
- **`.dekko/map.json`'s on-disk size cut 5.6-7.9x on large repos**
  (measured: zed 853.5MB→117.2MB, spring-boot 894.2MB→113.9MB,
  tensorflow 1212.4MB→217.6MB) via two changes to `render_json.py`/
  `mapfile.py`: a shared symbol-id interning table
  (`mapfile.build_id_table`) for the `ambiguous`/`edges`/`referenced`/
  `external` fields, which previously spelled out full symbol ids at
  every occurrence instead of referencing them by index; and dropping
  `indent=2` pretty-printing, since `mapfile.load_map()` is the only
  consumer of `map.json`, not a human reader. `MAP_DOC_VERSION` bumped
  4→5, with `load_map()` gaining version-branch handling to keep
  reading pre-v5 map.json files. `repo_ops._map_run_is_noop` and
  `MapIndex` gained a `doc_version` check so an already-mapped repo
  picks up the new format on a plain `dekko map` re-run instead of
  no-op'ing on a stale pre-v5 file.

## [0.31.2] — 2026-08-14

### Changed
- **`cli.py`'s repo-loading/map pipeline extracted into a new
  `src/dekko/repo_ops.py`** (`cli.py` shrank from 2,679 to 1,855
  lines). Purely structural: `hooks.py`, `orient.py`, `server.py`,
  and `daemon.py` now import the pipeline from `repo_ops` directly
  instead of deferred-importing it from `cli` to dodge a circular
  import; `_resolve_workers` moved alongside it since `cli.py` still
  calls it directly. Tests that monkeypatched the moved functions via
  `cli.<name>` were updated to target `repo_ops.<name>`.
- Daemon auth token comparison now uses `secrets.compare_digest`
  instead of `==`, closing a timing side-channel on the local
  loopback auth handshake.
- CI now runs `pytest` with `--cov=dekko --cov-report=term-missing`
  (report-only, no coverage gate yet; baseline is 89%) and a
  non-blocking `pip-audit` job against the locked dependency set.

### Added
- Unit tests for `textutil.py` (`signature`, `oneline`, `dir_of`,
  `estimate_tokens`, `count_lines`, `Meter`, `fit_to_budget`) and
  `source.py`.

## [0.31.1] — 2026-08-14

### Changed
- **`src/dekko/` reorganized from 42 flat modules into six role-based
  subpackages**: `core/` (parsing primitives — `model`, `extractor`,
  `extractor_generic`, `grammars`, `languages`, `walker`, `resolver`),
  `render/` (`mapfile`, `render_html`, `render_json`, `render_lean`,
  `render_md`, `export`), `analysis/` (`query`, `outline`, `search`,
  `affected`, `trace`, `unused`, `stats`, `summary`, `workset`,
  `contextpack`, `diff`, `relevance`), `daemon/` (`daemon`,
  `daemon_transport`), `integrations/` (`cli`, `server`, `hooks`,
  `cline`, `orient`, `claude_md`), and `storage/` (`cache`,
  `revcache`, `filelock`, `notes`, `ledger`, `embedding`).
  `classify.py`/`textutil.py`/`source.py` stay top-level. Purely
  structural — no behavior change; `tests/` partially mirrors the new
  layout (1:1-matching unit tests moved into per-subpackage
  directories, cross-cutting/end-to-end tests stayed flat).
  **Compatibility note:** anything importing dekko internals directly
  (e.g. `from dekko.cli import main` rather than via the `dekko`
  console script or MCP server) must update to the new paths (e.g.
  `dekko.integrations.cli`); the `[project.scripts]` entry point was
  updated accordingly and the built wheel layout was verified
  unaffected otherwise.

## [0.31.0] — 2026-08-14

### Added
- **Stronger dekko-usage enforcement for Claude Code sessions**
  (`.features/plans/usages/enforce-dekko-usage.md`). Real transcripts
  showed Claude falling back to `grep`/whole-file `Read` more than
  reaching for dekko's structural tools even with the existing
  session-start/prompt-submit/pre-read hooks installed, because every
  one of those is per-turn `additionalContext` an agent is free to
  weigh against convenience and ignore. Three additions, in ascending
  order of enforcement strength:
  - Sharper copy in the existing soft-push surfaces — `orient.py`'s
    session preamble, `hooks.py`'s prompt-submit nudge, and the MCP
    tool descriptions for `get_callees`/`find_usages`/`workset`/
    `impacted_tests` — now name the grep/Read alternative explicitly
    instead of only describing what the tool returns.
  - **`dekko --claude-md-install` / `--claude-md-uninstall`**: an
    idempotent, marker-bounded (`<!-- dekko:usage-policy:start -->` /
    `...:end`) usage-policy block written into the project's
    `CLAUDE.md`. Unlike per-turn injected context, `CLAUDE.md` content
    is documented as overriding default agent behavior — a materially
    stronger lever, loaded once per session. Kept as a separate
    top-level flag (not bundled into `dekko hooks install`) since it
    edits a file the user directly owns and reads, unlike
    `.claude/settings.json`.
  - **New `pre-bash` hook event** (`dekko hooks install --enable
    pre-bash`, off by default): a `PreToolUse`/`Bash` hook that matches
    a repo-wide `grep`/`rg`/`ag` search, a `find -name` hunt, or a
    `cat`/`head`/`sed` on a large mapped file, and surfaces
    `permissionDecision: "ask"` with the dekko-equivalent command — a
    real interruption instead of ignorable text. `--strict` escalates
    matches to `"deny"`. Matching is deliberately conservative (a
    targeted single-file `grep` or a `cat` on an unmapped file like
    `package.json` never matches) to keep false positives low.
- **Daemon-mode CLI** (`dekko daemon start/stop/status`). A per-repo
  background process the bare `dekko` CLI talks to over a socket
  (Unix domain socket on macOS/Linux, token-authenticated TCP
  loopback on Windows) so repeated CLI invocations share a warm
  `MapIndex` instead of each one reloading `map.json` from scratch.
  `diff`/`affected` share the same warm cache. Explicit start/stop in
  v1, no auto-spawn; every daemon-routing check fails open to direct-
  process behavior on any daemon absence/error.
- **Two new Claude Code skills.** `dekko-verify`: sanity-check a
  suspiciously low or zero call-graph result (`get_callers`,
  `get_callees`, `find_usages`, `impacted_tests`, `unused`) with a
  targeted grep before concluding "no callers"/"dead code" — codifies
  the known resolver blind spots repeated eval rounds keep finding
  (cross-package/qualified calls, trait/interface dispatch, unparsed-
  language files, the `--no-tests` default, high-symbol-density common
  method names). `dekko-daemon`: when to start the daemon ahead of a
  Bash-CLI-heavy stretch of work, what its warm cache does and doesn't
  cover (the `diff`/`affected` old-side reparse is never covered), and
  how to handle a `--no-daemon`/exit-7 abandoned-request retry.
  Also closed a documentation gap in the existing `dekko-orient`
  skill: `find_usages`, `map_status`, and `refresh_map` are three of
  the MCP server's 14 tools that were never listed there, leaving an
  MCP-only agent (no Bash) with no way to discover them.

### Fixed
- **`resolver.py` could self-resolve a bare-name call to its own
  enclosing symbol instead of the real cross-file target, silently
  dropping the call.** When two symbols in different files share a
  bare method name (e.g. Go's `IDGenerator.Generate` and an imported
  `slug.Generate`), `_pick_candidate`'s same-file candidate step
  could match the call's *own caller* as the sole same-file hit, not
  a genuine same-file target, just a coincidental name collision, and
  return it immediately. `_add_edge`'s self-recursion filter then
  silently discarded the resulting self-edge, so the real, cross-file
  call via an import hint was never tried and the call vanished from
  the graph. `_pick_candidate` now falls through to later ladder
  steps (import hints, in particular) whenever the same-file
  candidate is the caller itself, while a genuine self/this-qualified
  recursive call (already handled earlier via `_container_match`) is
  unaffected. See `.features/plans/round14/
  go-resolver-bare-name-collision-plan.md`.
- **Windows daemon transport (`TcpLoopbackTransport`) could wipe its
  own shared port file, and with it a still-valid daemon connection,
  whenever a status listener simply hadn't been bound yet.**
  `status_client_connect()` treated any `DaemonUnavailableError` from
  reading the status port as corruption and deleted the whole port
  file, including the main `port`/`token` entry
  `is_daemon_reachable()`'s fallback `client_connect()` needs, even
  for the benign case of a daemon started before
  `bind_status_listener()` existed, which simply lacks a
  `status_port` key. `daemon_transport.py` now raises a distinct
  `_StatusPortNotBoundError` for that case so cleanup only fires on
  genuine file corruption (unreadable, malformed JSON, or a
  missing/invalid main entry). Windows-only in origin (macOS/Linux's
  Unix-socket transport has no equivalent cleanup path), diagnosed
  from a Windows CI run failure; see `.features/fixes/
  windows-ci-failure-investigation.md`.
- **`dekko daemon status` could report `running: false` for a daemon
  that was alive but slow to reply, and `stop` could unlink a live
  daemon's transport artifacts on the same false-negative evidence.**
  Under sustained CPU contention, a status round-trip that had
  already connected to a genuine listener could still time out
  waiting for a reply, previously indistinguishable from a plain
  connection refusal, so `status()` folded both into the same "not
  running" report. `stop()`'s forced-fallback path (used when neither
  a graceful-shutdown ack nor a PID lookup confirmed the daemon was
  gone) had the mirror problem: it unlinked the transport
  unconditionally, capable of orphaning a still-listening process.
  `daemon.py`'s `status()`/`_query_pid()` now distinguish a
  post-connect timeout from a genuine absence and report
  `confirmed: false` instead of guessing; `stop()`'s forced-fallback
  now only cleans up when a final reachability probe itself fails,
  positive evidence, not silence. See `.features/plans/round14/
  daemon-lifecycle-fixes-plan.md`.
- **`dekko daemon stop` reported success up to ~1.1s before the
  daemon process had actually torn down.** From a live eval against
  6 of 7 real repos post-round-13 search fix
  (`test-repos/reports/14-tokentest-7repo-postround13searchfix/`,
  `MASTER_REPORT.md`), triple-independently confirmed (cline,
  claude-buddy, claude-code): `_handle_connection` acked a `_shutdown`
  request the instant it arrived, before `serve_daemon()`'s own
  teardown (joining the status-listener thread — bounded by that
  thread's own 1.0s `accept()` timeout, the dominant term in the
  measured lag — closing both sockets, then unlinking their transport
  artifacts) had actually run. A command issued in that window either
  hard-failed (exit 7, misclassifying "daemon just torn down" as
  "daemon still busy," violating the documented fail-open contract) or
  raced a concurrent `daemon start` into spawning a genuine duplicate
  live process. `daemon.py::stop()` now blocks (bounded, 5s cap) until
  the daemon's transport artifacts are confirmed gone — a race-free,
  filesystem-only check, since unlinking them is the literal last step
  of the teardown it's waiting on — before reporting success. This
  also structurally narrows a related daemon `start`→`stop`→`start`
  orphan race a sibling report (tensorflow) found under heavy machine
  contention, though that item stays open pending a contended re-test
  (see `.features/plans/round14/daemon-lifecycle-fixes-plan.md`).
- **Round-13 7-repo eval fixes.** From a live eval against 7 real
  repos post-round-12 (`test-repos/reports/13-tokentest-7repo-postround12fixes/`,
  `MASTER_REPORT.md`):
  - **Session-start hook silently blowing its token budget ~40x.**
    On a very large repo (tensorflow), the hook's path-only backbone
    floor could exceed `SESSION_MAP_BUDGET` with no signal anywhere
    that it happened, unlike the equivalent `dekko lean` CLI path
    which already warns. `hooks.py`'s session-start now discloses
    when this floor is exceeded.
  - **`dekko trace` false "no call path."** A route that exists only
    through ambiguously-resolved edges read as an indistinguishable
    genuine negative (spring-boot), inconsistent with `query callees`'
    own honest ambiguous-edge disclosure. `trace.py` now
    distinguishes the two cases.
  - **`dekko summary`'s "parse errors:" section mislabeling
    no-grammar skips as real failures.** Round-12 fixed this
    conflation in `dekko map`'s own summary but missed `dekko
    summary`'s separate code path (tensorflow, zed);
    `summary.py` and its `--json` fields now share the same
    distinction.
  - **Fuzzy "closest matches" noise.** Single-character symbol names
    could coincidentally surface as a substring match against an
    unrelated, long query (claude-buddy); `query.py`'s suggestion
    ranking now excludes these.
  - **A `FileNotFoundError` race in `dekko map`'s page writer.**
    Seen once right after a full `.dekko/` reset (spring-boot,
    corroborated by a softer non-crashing variant in claude-buddy);
    `cli.py`'s `_write_pages()` now re-asserts its parent directory
    exists immediately before its first write.
  - Documentation clarifications: `diff`/`affected`'s symbol-body-hash
    comparison granularity, and `dekko unused`'s expected
    false-positive shape on reflective/dynamic-dispatch-heavy
    frameworks.
  - **Go cross-package call-resolution gap**, deferred from the
    first pass as design work and closed in a follow-up (awesome-go):
    `resolver.py`'s `_repo_stem()` compared a qualified `pkg.Func()`
    call's import source against the *calling file's own filename
    stem* rather than its package directory, silently dropping every
    cross-package call through an imported first-party subpackage —
    Go packages are directory-scoped, not file-scoped. `_repo_stem()`
    now resolves every `.go` file to its parent directory
    unconditionally.
  - **Two daemon false-negative findings**, also deferred and then
    closed (claude-code, tensorflow): `dekko daemon start` could
    orphan a healthy daemon and spawn a duplicate for the same root,
    and `dekko daemon status` could report `running: false` for the
    full duration of a slow request while the daemon was alive and
    busy — both traced to the same root cause, a deliberately
    single-threaded accept loop that can't answer any request while
    busy on another. Fixed with a dedicated status-only listener
    (separate socket/port, its own background thread) that `daemon
    status` and `is_daemon_reachable()` now probe instead of the busy
    main command socket; `client_connect()`'s stale-artifact cleanup
    was also narrowed so a connect-level timeout (busy daemon) no
    longer deletes a live daemon's transport artifact — only a
    genuine "nothing listening" failure does. Fail-open guarantees
    (silent fallback to direct execution on any pre-request transport
    error) preserved throughout; both fixes have regression tests.
- **Round-13 search-relevance follow-up.** From the master report's
  one remaining open item (`.features/plans/round13/
  search-relevance-tuning-plan.md`), deferred at first because
  round-12's own precedent showed a same-session patch reacting to
  one reported query can regress a different one:
  - **`dekko search`'s relevance score computed inconsistently
    across two differently-sized candidate batches.** `search.rank()`
    filtered to zero-relevance survivors, then re-scored that smaller
    survivor set from scratch for the final blend — since BM25's
    IDF/length-normalization are corpus-relative, re-deriving over a
    different-sized batch produced a genuinely different number,
    occasionally flipping the rank of the query's own correct answer
    (cline: `"cancel task execution"` outranked `cancelTask()` with a
    telemetry method matching only one term). `relevance.
    blended_scores()` gained an optional `precomputed_relevance` param
    so `rank()` now reuses its already-computed full-batch relevance
    instead of re-deriving it; every other caller (workset,
    contextpack, render_lean, hooks) is untouched.
  - **New `--scorer both`.** For a separate, unrelated finding
    (zed: `"save file to disk"` missing `Item.save()`, which
    genuinely has no lexical overlap with the query at all — no
    scoring-weight change could safely fix that without overfitting)
    `dekko search` gained an opt-in third scorer choice that runs the
    lexical (BM25) and embedding scorers independently and fuses their
    rankings by rank position via reciprocal rank fusion, not raw
    score (the two scorers' scores aren't on a comparable scale).
    Requires the same `dekko[search]` extra as `--scorer embedding`;
    `lexical`/`embedding` alone are byte-for-byte unaffected.
  - A 6-fixture golden-query regression corpus was added to
    `tests/test_search.py` (multi-language, including a direct
    invariant test pinning the batch-consistency bug class) so future
    relevance tuning has a fast regression check instead of needing
    live multi-repo re-testing.
- **`dekko search --scorer both`'s fused score was unlabeled and easy
  to misread against `lexical`/`embedding`-only scores.** From the
  round-14 7-repo eval master report (`test-repos/reports/
  14-tokentest-7repo-postround13searchfix/MASTER_REPORT.md`,
  corroborated 2/6 — cline, claude-code): `--scorer both`'s reciprocal
  rank fusion score lands in a much lower, differently-shaped range
  (~0.03 typical) than a blended `[0, 1]` lexical/embedding score, with
  no in-band note explaining the scale changed — expected behavior
  (RRF fuses by rank position, not score magnitude), but a rough edge
  for anything comparing confidence *across* scorer modes. `search.py`
  gained `_scale_note()`, an unconditional `note:`/`"note"` hint on
  every `--scorer both` call (joined with the existing round-08 §2.2
  exclusion note when both fire); `lexical`/`embedding` alone are
  unaffected.
- Documentation: `docs/cli.md`'s daemon-mode section now notes that
  the warm cache's win is specifically about skipping map *loading*,
  not every part of a query's own cost — a query whose per-hit
  rendering dominates (e.g. `get_callers`/`find_usages` on a very
  high-fan-in "hub" symbol) can show little or no measurable
  wall-clock difference between a cold and warm daemon call even
  though `hits`/`misses` correctly show it was served warm (round-14
  eval, tensorflow §4.4 — not a bug, just an under-documented nuance).
- **Windows CI: daemon stale-artifact test hardcoded the Unix-only
  transport.** `test_stale_socket_falls_open` wrote a bogus file at
  `.dekko/daemon.sock` and asserted it got cleaned up — correct on
  macOS/Linux, but Windows selects `TcpLoopbackTransport` (artifact:
  `daemon.port`), so `client_connect()` never touched the irrelevant
  `daemon.sock` file the test wrote, and the assertion failed on
  every windows-latest CI run. Fixed by routing the test through
  `default_transport_for()`, per `test_daemon.py`'s own stated
  convention of never needing a `skipif`. Investigating this surfaced
  a real parity gap alongside it: `TcpLoopbackTransport`'s
  `client_connect()`/`status_client_connect()` read the port file
  *before* connecting, and a malformed/corrupt port file raised
  `DaemonUnavailableError` with no cleanup — unlike
  `UnixSocketTransport`'s stale-socket case (round-13 §2) or this
  same transport's own connect-level `OSError` branch, both of which
  do clean up. A failed port-file read now triggers the same
  cleanup, with a new regression test
  (`test_tcp_client_connect_malformed_port_file_cleans_up`).
- **Round-12 7-repo eval fixes.** From a live eval against 7 real
  repos post-round-11 (`test-repos/reports/12-tokentest-7repo-postround11fixes/`):
  - **Resolver: bare receiverless call misresolved against an
    unrelated same-named method.** A call with no receiver can never
    target a method, so `resolver.py`'s last-resort ladder now prefers
    a lone non-method candidate instead of falling through to
    ambiguous. Live-verified on awesome-go.
  - **`--jobs` was unwired on `diff`/`affected`/`workset`.** The
    library-level parallel-extraction plumbing existed but no CLI flag
    threaded through to it; `--jobs` is now accepted on all three
    subcommands. Confirmed a real ~25-27% wall-clock win on a
    repeated-run benchmark against tensorflow.
  - **`dekko daemon status` false "not running."** `_CLIENT_TIMEOUT`
    was a separate, shorter constant (2s) than the server's own
    request timeout, so a daemon still warming its cache could read as
    down. It now matches `_REQUEST_TIMEOUT` (30s).
  - **Parse-error vs. missing-grammar conflation.** `dekko map`'s
    summary and `outline`'s sparse-file heuristic treated a genuine
    parse error the same as an unsupported/missing grammar. New
    `grammars.is_grammar_unavailable_message()` splits the two so each
    is reported and suppressed correctly. Live-verified against
    spring-boot's Kotlin files and zed's Scheme files.
  - **Non-atomic `map.json`/`cache.json`/rev-cache writes.** A reader
    (daemon, concurrent CLI invocation) could observe a truncated or
    partially-written file mid-save. New
    `mapfile.atomic_write_bytes()` (temp file + `os.replace`) backs
    all four write sites. Confirmed via concurrent-read races against
    a live repo with zero corruption.
  - Documented (no fix needed): the MCP server's warm cache
    (`Context.index_cache`) is independent of the daemon's
    `_WarmCache` by design — noted in `server.py`'s docstring and
    `docs/cli.md` to head off future confusion between the two.
- **Round-12 open-items implementation pass.** From
  `.features/plans/round-12-open-items-implementation-guide.md`,
  design work following the round-12 eval fixes above:
  - **`dekko search` 30-40s+ latency on large repos.** The actual
    bottleneck wasn't BM25/embedding scoring but an uncached
    `classify.is_test_path()` reached millions of times via
    `MapIndex.without_tests()`. `lru_cache` on `is_test_path()` cut
    search on zed from ~30s to single-digit seconds.
  - **Silent local re-work on an abandoned daemon request.**
    `daemon.try_daemon()` now raises `DaemonRequestAbandonedError`
    instead of returning `None` once a request has actually been
    sent and no response arrives, so `cli.main()` reports a clear
    message and a distinct exit code (`EXIT_DAEMON_ABANDONED = 7`)
    instead of silently duplicating the work locally.
  - **Concurrent CLI invocations racing to regen the same stale
    `.dekko/` state.** New `filelock.py` (POSIX `fcntl`/Windows
    `msvcrt`, fail-open) wired into `cli.py`'s stale-map path so a
    second invocation waits for and reuses an in-flight regen
    instead of re-parsing the repo a second time.
  - **`--json` on the ambiguous-symbol error path.** Formalized the
    existing plain-text-on-stderr behavior as documented, tested
    contract rather than an inconsistency to fix.
  - **Search relevance ties favoring a generic term over a more
    specific one.** New IDF-weighted term coverage in `BM25Scorer`/
    `search.py`'s `_CoverageAdjustedScorer` breaks coverage-fraction
    ties toward the rarer, more distinctive term. Live-verified
    fixed on spring-boot's reported case; claude-buddy's case
    remains unresolved (corpus-relative IDF cuts the wrong way
    there) and is documented as still open.
  - **`referenced-by` noise on generic bare identifiers, phase A.**
    Scoped `languages.py`'s JS/TS shorthand-property reference query
    to object shorthand properties specifically. Live-verified as a
    no-op on the pinned tree-sitter grammar (already splits the
    conflated node types) — the real noise source is JSX/template-
    literal reads of shadowed locals, needing lexical scope tracking
    (phase B, deliberately deferred as a larger design effort).
  - Independently re-verified: all of the above except phase B hold
    up against live repro on the relevant `test-repos/` targets; see
    `.features/plans/round-12-implementation-verification.md`.
- **`dekko daemon start` false success on an oversized socket path.**
  When the daemon's Unix socket path exceeded `AF_UNIX`'s `sun_path`
  length limit, `daemon start` reported success (exit 0) and the
  failure only surfaced later on a `daemon status` call. New
  `DaemonTransport.preflight_check()` runs before the daemon process
  is spawned, so an oversized path now fails `daemon start` itself
  with exit 1 and a clear message.

## [0.30.1] — 2026-08-07

Fixes the 5 follow-up issues from round 09's re-evaluation, documented
in `.features/plans/investigation-09-round09-followups.md`.

### Fixed
- **zed-class call-edge gaps in `resolver.py`.** An explicit
  `Type::method()`/`Type.staticMethod()` receiver (the type's own bare
  name, not a variable of that type) is now resolved directly via new
  `_receiver_type_match`, ahead of the typed-parameter step — closing
  a gap where such calls fell through to the generic ladder and landed
  ambiguous whenever the repo defined the method name more than once
  elsewhere. Live-verified on zed: `BufferDiff.new` went from 0 to
  12/13 callers. `_pick_candidate`'s self/this step was also split out
  into `_container_match` to keep the growing ladder readable.
- **Noise-call guard missing Rust std/prelude methods.** New
  `_RUST_STD_METHOD_NAMES` (`then`, `iter_mut`, `unwrap`, `clone`,
  etc.) closes the same false-positive shape `_BUILTIN_METHOD_NAMES`
  already covers for JS/TS, but for Rust — a receiver-qualified call
  not provably typed as an in-repo class was being misattributed to
  an unrelated same-named repo method. Live-verified on zed.
- **`query callees` didn't disclose dropped ambiguous calls** the way
  `query callers` already discloses `ambiguous_in`. New
  `MapIndex.ambiguous_out` (caller → names it called ambiguously) and
  a matching stderr note / `ambiguous_out` JSON field on the callees
  side, so a low `calls_out` count can be qualified instead of read as
  exhaustive.
- **`dekko lean --budget` silently overriding a too-tight request.**
  `effective_cap` never lets the cap fall below the repo's path-only
  floor, but this was invisible to the caller — a `--budget 500` on a
  large repo could render identically to an unbudgeted run with no
  indication why. `render_lean.run()` now prints a stderr note when
  the floor overrides the requested budget.
- **Hardcoded `file.py` in the ambiguous-candidates hint.** `query.py`'s
  "qualify with `file.py:name`" hint used a literal placeholder instead
  of an actual candidate path; now uses the first ranked candidate's
  real path.
- **MCP `map_status` stale message didn't distinguish version vs. spec
  staleness.** A long-lived `dekko serve` process can have an
  identical `tool_version` string on both sides while still running
  stale extractor code underneath it (a reinstall doesn't change the
  version every release), which read as a self-contradictory "built by
  dekko 0.21.3, running 0.21.3" with no explanation. `Freshness` now
  carries `version_stale`/`spec_stale` flags and the raw built/running
  values; `tool_map_status` names the actual differentiator and flags
  the long-lived-process case explicitly.
- Confirmed (no code fix needed): the resolved/ambiguous edge-count
  shift observed on cline between rounds 08 and 09 was traced to round
  08's already-documented stale-binary baseline issue, not a
  regression introduced by round 09's fixes — `resolver.py`/
  `extractor.py` are byte-identical between the compared commits.

## [0.30.0] — 2026-08-07

Two 7-repo evaluation rounds (`test-repos/reports/07-tokentest-7repo-fixcycle/`,
`08-tokentest-7repo-fable5/`, `09-tokentest-7repo-postfix/`) against
real-world repos (awesome-go, claude-buddy, claude-code, cline,
spring-boot, tensorflow, zed) drove a two-cycle fix pass. Round 09
confirms the O(N^2) `lean` hang and the spring-boot vendored-dir bug
are fixed, disambiguation is a clean win across all 6 re-tested repos,
and token savings remain strong (12x-765x vs. Read/grep).

### Added
- **`dekko search "<query>"` / `search_code` MCP tool (semantic
  search, Phase 1).** Free-text relevance search that ranks every
  symbol in the map by BM25-style lexical scoring (name/qualname/
  signature/doc), for when you know what code should do but not its
  name — no new dependencies. New `relevance.BM25Scorer` (alongside
  the existing `LexicalScorer`, which is unchanged) and new
  `search.py` module. Options: `--limit`, `--budget`, `--kind`,
  `--include-tests`, `--json`, `--no-regen`. See
  `.features/plans/SEMANTIC-SEARCH-PLAN.md` for the design and
  implementation notes.
- **`--scorer embedding` for `dekko search` / `search_code` (semantic
  search, Phase 2), opt-in via `pip install dekko[search]`.** A
  deterministic hashing-trick embedding scorer (character n-gram
  feature hashing + signed random projection, `numpy`-only — no
  pretrained model, no download, fully offline), with a new
  `embedding.py` module: `EmbeddingScorer` (implements the same
  `relevance.Scorer` protocol as `BM25Scorer`) and `EmbeddingCache`
  (mirrors `cache.IncrementalCache`'s reuse/invalidate pattern),
  persisted to `.dekko/embeddings.json`. The default scorer stays
  `lexical` (BM25, unflagged, always available) — a base install and
  every existing `dekko search`/`search_code` call are unaffected.
  Requesting `--scorer embedding` / `scorer: "embedding"` without the
  extra installed fails with a clear error rather than silently
  falling back. Deviates from the plan's original `sentence-
  transformers` sketch — see `.features/plans/SEMANTIC-SEARCH-PLAN.md`
  §8 and "Implementation status" for why.
- **`dekko[fastjson]` extra** (`orjson`-backed JSON read/write, falls
  back to stdlib `json` when not installed) and a validated
  `.dekko/provenance.json` sidecar so `dekko status` can skip a full
  `map.json` parse.
- **`query`'s `:LINE` disambiguation qualifier.** `resolve_target`
  accepts a trailing `:LINE` (e.g. `path:qualname:line`) to pick
  between overloaded symbols that collide on `(path, qualname)`;
  `report_unresolved` now hints at the `:LINE` form when candidates
  share a name and path.
- `note remove` alias; `--dry-run` for `--claude-install` /
  `--claude-uninstall`; `DEFAULT_BUDGET` caps for bare `dekko summary`
  (5000) and `dekko affected` / `impacted_tests` (6000), so neither
  can render an unbounded report by default.

### Fixed
- **O(N^2) hang in `dekko lean`** on large repos — `_shed_symbols`'s
  linear `fits()` walk replaced with a binary search (`_bisect_shed`).
- **Vendored-dir false positives.** JVM-style source roots
  (`src/main|test/<lang>/...`, e.g. Spring Boot's
  `org.springframework.boot.build`) are exempted from
  `_VENDORED_DIRS` matching so a package literally named `build`
  isn't mistaken for build output; the no-`.git/` walker fallback now
  prunes against the same exclude-dir list as the git-aware path
  instead of leaving the "vendored (<dir>)" skip reason dead code.
- **Search relevance.** A shared term-coverage discount in
  `BM25Scorer`/`LexicalScorer` stops partial matches from rescaling
  to a false `1.00` score, surfaces an "N test-file symbols excluded"
  hint when the top score is weak, and (via a scorer-agnostic
  `_CoverageAdjustedScorer` wrapper in `search.rank()`) stops a common
  term from crowding out a more distinctive one under BM25 or
  embedding scoring alike.
- **Resolver false positives on built-in/global calls.** Calls like
  `.trim()`, `expect()`, or a global `String` reference were
  silently attributed to a same-named repo symbol whenever a repo
  happened to define exactly one, inflating fan-in and polluting
  hotspot rankings. New `_is_noise_call` rejects calls shadowed by an
  external import, calls to curated ambient globals, and
  receiver-qualified calls to curated built-in prototype methods
  (self/this receivers exempted). Live-verified on cline: `trim`
  fan-in 1404 -> 2, `expect` 603 -> 5, `String` 548 -> 3.
- **C++/C `#include`-based call disambiguation** and a fix so a
  header's own stem (not the generic extension-only fallback) is
  used as its `Import.name`, which was silently colliding across
  nearly every include in a file.
- **Zod `.describe()` fan-in collision** — added to the built-in
  schema-builder-method denylist alongside C++/Go reference-tracking
  fixes for dead-code false positives (Go value-typed struct usage,
  JSX-referenced components).
- **`query.py` resolution/reporting.** `_resolve_exact` merges
  qualname/name symbol pools instead of or-short-circuiting (bare-name
  collisions no longer masked by a qualname hit); unresolved rows show
  `path:start_line  signature(sym)`; `_close_names`' fuzzy tier
  requires `len(name) >= 3` and raises its cutoff to 0.72 to suppress
  single-letter junk suggestions.
- **Change-analysis correctness.** `diff.snapshot` reuses an
  already-loaded `MapIndex` (freshness-gated) instead of re-parsing;
  the old-side file list now comes from `git ls-tree` instead of
  `walker.discover`'s gitignore-reapplying fallback, which produced
  phantom "added" symbols for already-tracked files an unanchored
  `.gitignore` pattern happened to match; `affected._test_hint`
  replaced the pytest-only hint with per-language grouping
  (pytest/cargo test/go test/npm-bun-pnpm-yarn/gradlew-mvn);
  `impacts_from_symbol` gained an import-tier fallback for languages
  without per-symbol import bindings (e.g. C++).
- **`affected.render()`** now surfaces the same vendored-exclusion
  coverage caveat `query` already carries when a diff touches only
  vendored-excluded files (e.g. tensorflow's `third_party/xla`), so
  "no impacted tests" no longer reads identically to a genuinely safe
  change dekko never looked at.
- **`dekko orient`'s preamble** no longer tells an agent to use
  `search` when the subcommand isn't actually available in-process.
- Unbounded `dekko summary` parse-error output capped at 15 with a
  per-language collapse footer.

### Performance
- **Server-side `MapIndex` caching.** The MCP server now caches the
  in-process `MapIndex` per session and reuses it while
  `mapfile.check_freshness` still reports it fresh, skipping
  redundant JSON parse/rebuild on repeat calls in the same session.
- **`revcache.py`**, a disk-backed cache of resolved historical git
  revisions (mtime-evicted, `MAX_ENTRIES=20`), shared between
  `diff.run` and `affected.changes` instead of each re-exporting/
  re-parsing the old revision independently.
- BM25 term tokenization (`_raw_terms`/`_stemmed_terms`) is now
  `lru_cache`-wrapped, and `search._build_candidates` caches its
  built candidate list on the `MapIndex` instance, eliminating
  redundant re-tokenization on repeat `search` calls against an
  already-loaded index.

## [0.21.3] — 2026-08-03

### Added
- **README logo.** A small eye mark (light/dark SVG variants under
  `assets/`, swapped via `prefers-color-scheme`) now heads the
  README — a nod to the name itself ("dekko" is British slang for a
  look or glance).
- **MCP Registry metadata.** Added `server.json` and an `mcp-name:
  io.github.aahlijia/dekko` marker in the README so dekko can be
  published to the official [MCP
  Registry](https://registry.modelcontextprotocol.io).

## [0.21.2] — 2026-08-03

### Changed
- **Claude plugin files moved into `integrations/claude/`.**
  `.claude-plugin/`, `commands/`, `skills/`, and `.mcp.json` were
  cluttering the repo root; they're now grouped under
  `integrations/claude/` (`integrations/claude/.claude-plugin/`,
  `integrations/claude/commands/`, `integrations/claude/skills/`,
  `integrations/claude/.mcp.json`), leaving room for other editor
  integrations alongside it under `integrations/`. Pure source-tree
  reorg — the wheel's `dekko/_plugin/` layout (what
  `dekko --claude-install` actually uses) is unchanged, so installed
  users see no difference.

## [0.21.1] — 2026-08-03

### Fixed
- **Documented why `dekko://summary` stays unbounded.** An audit of
  `skills/`/`commands/` flagged the MCP resource's uncapped output as
  an apparent inconsistency with the `summary` tool's ~2000-token
  default. It's intentional, not a bug: a resource is fetched by
  reference on demand rather than re-sent as cache on every
  conversation turn like a tool result, so the token-bloat concern
  that justified capping the tool doesn't apply. Added a comment on
  `_handle_resources_read` explaining the asymmetry; no behavior
  change (see `test_mcp_summary_resource_stays_unbudgeted`).

## [0.21.0] — 2026-08-03

### Added
- **Persistent `.dekko/.dekkoignore`.** `dekko map --exclude GLOB`
  now also appends each new pattern to `.dekko/.dekkoignore` (created
  and tracked alongside `notes.json`), so exclusions survive as
  project state instead of shell history — a bare `dekko map` with no
  flags honors patterns persisted by an earlier `--exclude` run.
  `.dekkoignore` is hand-editable gitignore syntax (comments,
  negation, `**`, trailing-slash dir patterns), parsed with
  `pathspec`/`gitwildmatch` — a different matching engine than
  `--exclude`'s plain `fnmatch`, so files it skips are reported under
  a distinct `"ignored"` skip reason (`--exclude` keeps `"excluded"`).
  Note the resulting matching-semantics divergence for
  extension-filtered directory patterns: `--exclude 'dir/*.py'`
  reaches into `dir/sub/nested.py` today (fnmatch isn't slash-aware),
  but the identical string persisted to `.dekkoignore` only matches
  the direct child once re-parsed as gitwildmatch. `regen_map`/
  `--if-stale` auto-regen never re-persist; staleness from a hand-edit
  falls out of the existing freshness check with no new provenance
  field.
- **Community health files**: `CODE_OF_CONDUCT.md`, `SECURITY.md`,
  `.github/ISSUE_TEMPLATE/` (bug report, feature request), and
  `.github/PULL_REQUEST_TEMPLATE.md`.
- **README**: a "Why dekko?" section with the headline benchmark
  numbers and a comparison to `ctags`/`gtags`-style navigation, plus
  downloads/ruff badges.
- `pyproject.toml` keywords extended with `llm-agents`,
  `codebase-indexing`, `model-context-protocol` to match the GitHub
  topics used for discovery.

## [0.20.0] — 2026-08-02

### Changed
- **MCP agent surface trimmed 18 → 13 tools.** `trace_path`,
  `find_unused`, `stats`, `lean`, and `ledger` are CLI-only now: every
  MCP schema is paid in context tokens each session (~2.8k → ~2.1k),
  and live agent transcripts (2026-07-10 A/B eval) never reached for
  them. The CLI commands are unchanged.
- **`summary` and `outline` MCP tools default to a ~2000-token budget**
  (override with `budget`). An un-capped `summary` on a large monorepo
  rendered ~30k chars as the session's first call and was re-read as
  cache every turn. `dekko summary` gained a matching `--budget` flag;
  the `dekko://summary` resource stays uncapped.
- **Symbol targets accept `::`** (`file.py::name`, `Class::method`) —
  the Rust/C++ habit agents fall into — retried as both grammar
  readings instead of dead-ending.
- **`dekko map` takes a true no-op fast path.** When the incremental
  cache determines nothing needs re-parsing, no file was added or
  removed, and the on-disk map already matches this dekko build,
  `map` now skips `resolve()`/render/write entirely and prints
  `dekko: unchanged (N files, commit X) — nothing written` instead of
  unconditionally re-serializing MAP.md/map.json/shards on every
  invocation. `--full` always bypasses this path.
- **`get_context_pack`'s tool description** now shows a worked
  `target`/`task` example (`target="awardXp", task="who calls
  this"`), the one parameter agents most often guessed wrong on.
- **CLI help documents the `--root` split**: `dekko map [DIR]` takes
  its root positionally; every other subcommand uses `--root DIR`.

### Added
- **`--cline-install`/`--cline-uninstall`** register/remove the MCP
  server in Cline's `cline_mcp_settings.json` (`--cline-scope
  vscode|global`, `--cline-config PATH` to override auto-detection,
  `--cline-force` to reset a malformed existing file instead of
  aborting). `dekko serve --mcp` needed no changes — it's a
  client-agnostic stdio JSON-RPC server; only Claude Code's install
  path (`.mcp.json`, `claude mcp add`) was ever Claude-specific. Cline
  has no plugin system, so there is no `/map`-equivalent for it — only
  the MCP tools are installed.
- **Near-miss suggestions on failed lookups.** `query symbol` (and the
  MCP relation tools) list the closest symbols when a target resolves
  to nothing; `query uses` suggests close external names. Keeps agents
  inside the map instead of ejecting them to grep/full reads.
- **Top-level `const`/`let` exports indexed as symbols** (new
  `kind="variable"`) in JS/TS/TSX. `export const jobs = [...]` was
  previously invisible to the symbol table — only arrow-function/
  function-expression values were captured — so `query symbol
  jobs`/`get_callers` returned "no symbol matches" even for exported
  data. `dekko summary`/`MAP.md`/the HTML map now report a `variables`
  count alongside functions/methods and classes.
- **`interface`/`enum`/`struct`/`record`/`trait` kinds.** TS
  interfaces/enums, Go structs/interfaces, Java interfaces/enums/
  records, and Rust structs/enums/traits used to all come back as
  `kind: class` from `query_symbol`/`outline`. Every renderer that
  counted `kind == "class"` (`dekko summary`, `MAP.md`, the HTML map)
  now counts the full set so the "classes" total doesn't undercount.
- **`ambiguous_in` counts on `query symbol`/`get_callers`.** A call
  whose name matches more than one repo-wide candidate was already
  recorded in map.json but never loaded back for reading — a low
  fan-in can now be qualified as "+N ambiguous call sites not counted"
  instead of read as exhaustive.
- **Unparsed-language coverage note on "not found" replies.**
  `query_symbol`/`outline`/`get_context_pack`/`get_callers`/
  `get_callees` now attach the same "N files unparsed" note
  `summary`/`status` already show when a target resolves to nothing,
  so a symbol that only exists in an unsupported file (e.g. `.astro`)
  doesn't read as a confident "doesn't exist."
- **Anonymous-callback callers in `get_context_pack`.** A call site
  with no named enclosing function used to be demoted to a terser,
  line-number-less `module_callers` summary line; it now also appears
  in the main `callers:` list with a real line number
  (`module_callers` is kept for backward compatibility).
- **`referenced` edges (map.json schema v3 → v4).** A function passed
  *by reference* (an object-literal property value, array element,
  bare call argument, or assignment/declarator right-hand side in
  JS/TS/TSX) is now tracked separately from calls via a new
  `RawRef`/`referenced`/`referenced_in`/`referenced_out` table —
  deliberately never merged with `edges`/`calls_in`/`calls_out`, so
  "wired up as a callback" stays distinguishable from "invoked here."
  `query symbol` reports a `referenced-by: N (not called)` line next
  to fan-in/fan-out; `get_callers` on a reference-only symbol prints a
  `referenced (not called):` section instead of the bare `(no callers
  of X)` line. Old (pre-v4) maps simply have empty referenced tables.

### Fixed
- **Stale map/cache could silently serve outdated extraction results
  forever.** `.dekko/cache.json` and `.dekko/map.json` now carry a
  `spec_hash` fingerprint of the extraction queries, invalidating a
  cached extraction (or flagging a map as stale) on *any* extractor
  change — not just a released version bump. `dekko status`/
  `map_status` report why a map is stale (`reason: "version"` vs.
  `"content"` vs. `"missing"`) with an actionable "built by dekko X,
  running Y" message for a version mismatch, instead of a silent
  false "fresh."
- **Nested closures no longer inherit the enclosing class's
  qualname/kind.** A `const helper = () => {}` (or Rust nested `fn`)
  declared inside a method body was reported as `Class.helper`, kind
  `method` — a closure-local helper mislabeled as a class member. The
  container-qualification climb now stops dead at the first enclosing
  function/method/closure in JS/TS/TSX and Rust.
- **`dekko unused` no longer flags pass-by-reference callbacks as dead
  code.** A callback wired up by name (JS/TS/TSX) and never itself
  called was invisible to `get_callers`/fan-in/`unused` entirely — the
  new `referenced_in` table (see Added) now counts it as used.
- **Every MCP reply built from a default (unspecified) `root` now
  echoes the resolved path.** Omitting `root` silently answered
  against the server's cwd — often the wrong repo in a multi-project
  session — with no visible sign anything was off. A reply built this
  way now opens with `(root: /resolved/path — no 'root' argument was
  given; pass one to target a different repo)`, so a wrong-repo answer
  is visually distinct from a correct one instead of looking identical.
- **`get_callers`/fan-in undercounted calls made through a typed
  variable, a function's own typed parameter, or `new X()`
  construction.** The resolver's ladder now also matches a call
  through one of the calling function's declared-typed parameters, and
  credits a class's own constructor (JS/TS `constructor`, Python
  `__init__`, Java's same-named constructor declaration) when a call
  resolves to that class — `new Controller(...)` no longer leaves
  `Controller.constructor`'s fan-in at 0. Scope note: this covers
  declared parameter types only; local-variable type inference outside
  a parameter list still isn't tracked.
- **`find_usages` gives no caveat when a shadowing in-repo symbol
  returns a wrong or incomplete external-reference result.** It
  already refused cleanly when a query matched *only* an in-repo
  symbol; now any query whose name collides with an in-repo symbol —
  even one that still returns some external hits — carries a "this
  result may be incomplete" caveat, in both text and JSON
  (`shadow_warning`) output.
- **`get_context_pack`'s budget trimming could zero out the very
  callers/callees a task asked about while its import list survived
  untouched.** Imports are now trimmed to empty first; callers/callees
  are never the first thing cut under a tight budget.
- **`workset`'s impacted-tests listing ignored `--budget` entirely.**
  The pytest-command hint now caps at 20 paths (`+N more impacted test
  files not shown`), and the JSON output reports a budget-fitted
  `impacted_tests` list alongside a separate `impacted_tests_total` so
  the two are never conflated.
- **`outline` gives no signal when a file's shape looks anomalously
  thin for its size** (e.g. a file built mostly from anonymous-
  callback registration, which has few named symbols to hang an
  outline row on). A file at least ~500 tokens with ≤8 named symbols
  covering ≤15% of its full length now carries a caveat that the
  outline may be missing most of the file's real content.
- **Ambiguous-symbol lookups dumped every candidate unconditionally**
  (a bare `main` in a Rust workspace with ~90 binaries listed all of
  them). The candidate list is now capped at 20 with a "+N more
  (qualify with `file.py:name` to narrow)" note.
- **Type symbols (class/struct/interface/enum/record/trait) with zero
  call/reference edges read as "unused" even when heavily referenced
  as a parameter, field, or return type.** `query symbol` now attaches
  a caveat to a zero-fan type symbol explaining that call/reference
  edges only track invocations, not type usage.

### Documentation
- **Documented the MCP server staleness gotcha.** A running `dekko
  serve --mcp` process holds its Python modules in memory for its
  whole lifetime — restarting it is required to pick up any dekko code
  change, and `uv tool install --reinstall` alone does not affect an
  already-running server process. See README's "MCP server" and
  "Development" sections for the full restart-required workflow.

## [0.12.0] — 2026-06-16

### Added
- **CI matrix** (`.github/workflows/ci.yml`): on every push/PR to
  `develop`/`main`, the suite runs across
  `{ubuntu, macos, windows} × {3.10, 3.13}` with `ruff check`,
  `ruff format --check`, and `pytest`. Windows is `continue-on-error`
  (best-effort for 1.0.0); Linux/macOS are blocking. This turns
  cross-platform correctness from opinion into a checked fact.

### Changed
- **Tier-1 grammars now install offline; Tier-2 moves behind
  `dekko[all]`.** A default `pip install dekko` ships the nine Tier-1
  languages (C, C++, Go, Java, JavaScript, Python, Rust, TypeScript,
  TSX) as individual, pinned grammar packages, so mapping them makes
  **no network call** — no more runtime grammar download, offline
  failure, or supply-chain surface from the catch-all pack. The ~55
  generic Tier-2 languages now require `pip install dekko[all]`, which
  pulls in `tree-sitter-language-pack`; without it, a Tier-2 file is
  skipped with a "needs `dekko[all]`" note rather than parsed. Grammar
  resolution moved behind a new `grammars.get_grammar` seam (cached, so
  each grammar loads once). Map output for any installed grammar is
  unchanged.
- **Release workflow is hardened around the version tag.** `release.yml`
  still fires only on a `v*` tag, but now rejects a tag whose version
  does not match the built wheel (catches a forgotten version bump), and
  the publish job carries an explicit `refs/tags/v*` guard so it can
  never run off a non-release ref. The workflow header documents the
  gating and the one-time PyPI trusted-publisher prerequisite.
- **`dekko diff` no longer shells out to `tar`.** The earlier-rev export
  now captures `git archive --format=tar` and extracts it with the
  stdlib `tarfile` module instead of piping to an external `tar`
  binary, removing an undocumented POSIX dependency (a step toward
  Windows support). Extraction refuses path traversal (the `data`
  filter on 3.12+, an explicit guard on 3.10/3.11). Map output is
  unchanged.

### Fixed
- **Windows: the `claude` CLI is now invoked by its resolved full path**
  rather than the bare name, so the plugin/MCP install and uninstall
  commands launch a `claude.cmd` shim that `subprocess` would otherwise
  fail to start. No change on macOS/Linux.
- **Windows: the session ledger now finds its transcript.** The
  `~/.claude/projects` directory key now encodes backslashes and the
  drive colon (not just POSIX `/`), matching Claude Code's per-platform
  naming. Still best-effort — a miss degrades to an empty ledger.

### Documentation
- **README install & platform pass**: the install section now states the
  offline Tier-1 footprint, points to `pip install dekko[all]` for the
  Tier-2 languages, and notes the tested-platforms line (macOS/Linux;
  Windows best-effort via CI). The "Language support" and "Development"
  sections match the new packaging.

## [0.11.0] — 2026-06-16

### Active Context Layer

dekko grows from a *pull* context server into a *session-aware push*
layer: it ranks context by the live task, knows what the agent already
holds, and can deliver orientation through opt-in Claude Code hooks.

#### Added
- **Task-aware ranking (`--task`)** on `lean`, `workset`, and `context`
  (and the matching MCP tools): a free-text task description is blended
  with structural centrality and the working diff so the most relevant
  code survives a tight budget. Lexical and dependency-free; output is
  byte-for-byte unchanged when no task is given. New `relevance` module
  with a pluggable `Scorer` (lexical now, embeddings a future drop-in).
- **`dekko lean --dense`** (and MCP `dense`): keeps full signatures only
  on the most central symbols, names for the rest — the tersest
  whole-repo map.
- **`dekko ledger`** (and MCP `ledger`): projects the Claude Code session
  transcript into "what is already in context" — files read, symbols
  seen, and real tokens consumed (from the transcript's usage). dekko
  persists no session state of its own, so it also sees direct reads.
- **`dekko hooks install|uninstall|run`**: opt-in push hooks merged into
  project `.claude/settings.json` — `session-start` (steering preamble +
  budget-capped lean map), `prompt-submit` (relevance-ranked pointer to
  files not yet in context), and `pre-read` (non-blocking advisory to
  outline a large file first, `permissionDecision: "defer"`). Every hook
  is fail-silent and individually toggleable; uninstall touches only
  dekko's entries.
- **Density metric (FR-D3)**: `Meter` and the lean report now expose
  `signals` and tokens-per-signal, so output cost can be measured against
  coverage. A `benchmarks/` harness records the baseline reduction (dekko
  mapping its own source: ~92% fewer tokens than whole-file reads).

## [0.10.0] — 2026-06-16

Context & token management for agents: every list-shaped command can now
be held to a token budget, and new commands (`outline`, `lean`,
`workset`, `orient`) let an agent orient and scope a change without
reading whole files.

### Added
- `dekko lean`: a budget-capped, whole-repo navigation map for agents —
  the middle ground between `dekko summary` (~400 tokens) and `MAP.md`
  (tens of thousands). Every in-scope file with its purpose, each
  symbol's name (signatures on the most central, by fan-in × churn),
  the coarse module-dependency edges, and an optional architecture
  diagram, all shed in a fixed priority order to fit a hard token cap
  that scales with repo size. The header reports what was elided and the
  command to recover it. Prints to stdout, writes a file with
  `--output` (e.g. `.dekko/LEAN.md`, gitignored like other maps), or
  emits `--json`; also an MCP `lean` tool.
- Universal token budgeting across `query`, `unused`, `affected`, and
  `context`. Each command now ranks its rows by relevance (production
  before tests, more-connected before leaves), keeps as many as fit, and
  self-meters: text output carries a `(~N tokens · M of T omitted ·
  raise --budget)` footer and JSON carries a matching `meta` object. A
  `--budget` flag caps `query`/`unused`/`affected`; the relation MCP
  tools gained an equivalent `budget` argument.
- `dekko outline <path|dir>`: a file's (or directory's) structure —
  module purpose, each symbol's signature, doc first line, and line
  number, with no bodies — at roughly a tenth the cost of reading the
  file, plus a `full ≈ X · outline ≈ Y (P%)` size frame. Exposed as an
  MCP tool whose description steers agents to prefer it before reading a
  file.
- `dekko workset [REV] | --symbol NAME`: one budgeted bundle for a whole
  change — the impacted test files (with a ready-to-paste `pytest` hint),
  outlines of the touched files, and context packs for the most central
  touched symbols. A single shared budget (default 6000) trims
  detail-first so breadth survives a tight cap; `--packs` controls how
  many symbols get a pack. Also available as an MCP tool.
- `dekko orient`: an opt-in orientation layer. With no arguments it
  prints a steering digest (a budgeted repo summary plus pointers to the
  query surface); with `--read PATH` it emits a one-line nudge to outline
  a file before reading it, but only when the file is large enough to be
  worth it, and never blocks. Ships with a `dekko-orient` skill and
  documented (opt-in) `SessionStart` / `PreToolUse` hook snippets.
- Optional accurate token counting for every `--budget` cap and the lean
  map: `pip install dekko[tokenizer]` adds `tiktoken` (o200k_base) and
  dekko uses it automatically, replacing the default `~4 chars/token`
  estimate (which systematically under-counts code). The default install
  is unchanged — no dependency, byte-stable output. `DEKKO_TOKENIZER=
  chars4` forces the estimate back on for reproducible output even when
  the extra is installed.

### Changed
- Internal: shared helpers were promoted for reuse by the lean map —
  `textutil.dir_of`, `summary.file_churn`, and `export.dir_graph` (the
  directory-level graph behind both MAP.md's diagram and the lean map's
  module edges). No user-visible change.

## [0.9.0] — 2026-06-14

Track B: the human-readable map. `MAP.md` is now a navigable document —
an overview with rankings and an architecture diagram, sharded pages for
large repos, hotspots and a freshness line — plus a standalone
interactive HTML export.

### Added
- `MAP.md` now renders purpose lines from the v3 schema's `doc`
  fields: the Contents index shows each file's module purpose after
  its symbol count, file section headers carry the same purpose, and
  each symbol block shows its docstring first line under the
  signature. Files with no doc, and parse-error files, render cleanly
  with no placeholder noise.
- `MAP.md` now opens with an `## Overview` section: a per-directory
  rollup table (files, symbols, internal vs. cross-directory call
  edges, purpose), linked load-bearing and orchestrator rankings,
  entry points, and parse errors. It is the markdown skin of
  `dekko summary` — one computation, two renderings — so the digest
  and the document always agree. Cross-directory edge counts are the
  new "coupling at a glance" number.
- The `MAP.md` Overview now embeds a `mermaid` architecture diagram,
  rendered natively by GitHub (no toolchain or network). A scale guard
  tiers it down as the repo grows: the file-scope graph while it fits
  under `--max-nodes` (300), then a directory-scope collapse, then a
  one-line pointer to `dekko export --format mermaid`. MAP.md and
  `dekko export` share one graph generator.
- `dekko map --shard auto|always|never` (default `auto`): large maps
  split into per-directory `map/<dir-slug>.md` pages with `MAP.md` as
  the index (Overview + linked TOC); `auto` shards once the single
  document would exceed ~4,000 lines or 200 KB. Anchor ids are global,
  so a symbol's link is identical in either shape. Stale pages from a
  previous run (e.g. a renamed directory) are cleared before writing.
- The `MAP.md` Overview gained a **Largest files** list (linked, by
  symbol count; also shown by `dekko summary`) and a best-effort
  **Hotspots** table — recent git churn weighted by fan-in, surfacing
  the files where a change spreads furthest. The hotspots section is
  omitted silently on non-git roots or any git failure.
- The `MAP.md` header now carries a freshness/trust line —
  `Mapped N files in T ms (cache: X reused / Y parsed)` — so a reader
  can see at a glance how the map was built.
- `dekko map --order path|name|fan-in` (default `path`): order the
  `MAP.md` file sections by path (today's walk order), base filename,
  or fan-in (most depended-on first). `fan-in` also orders the symbols
  within each file by inbound degree — load-bearing first.
- `dekko export --format html`: a single self-contained, interactive
  HTML file (default `.dekko/map.html`) — collapsible directory tree,
  client-side substring search over names/qualnames/paths, and a symbol
  pane with signature, doc, and clickable callers/callees showing
  call-site lines. Test symbols are de-emphasized; the header carries
  the summary stats. No dependencies, no network, no build step; a size
  guard refuses maps too large to inline (exit 2, like `--max-nodes`).
- `dekko export --output PATH` writes any format to a file instead of
  stdout (html defaults to `.dekko/map.html`).

### Changed
- `signature()` moved from `render_md` to `textutil` so renderers and
  the summary/overview share it without an import cycle. Internal
  only; output is unchanged.
- `--output` and `--shard` interact: an explicit `--output FILE` forces
  `--shard never` (one file as asked); `--output DIR` shards into
  `DIR/map/` under the usual rules.
- The `MAP.md` Contents index is quieter: files with no symbols, doc,
  or parse error collapse into a per-directory `also present:` line
  instead of empty sections; test files move into a collapsed
  `<details>tests (N files)</details>` block; and the redundant
  `(parse error)` marker is dropped (the Overview's parse-error list
  already carries it).

## [0.8.0] — 2026-06-13

### Added
- The generated `MAP.md` now opens with a one-line note steering agents
  to `dekko summary` and the `query`/`context`/`affected` commands (or
  the MCP tools) instead of reading the whole file.
- An optional `PostToolUse` hook snippet in the README keeps the map
  refreshed as you edit, made cheap by the freshness fast path below.
- Symbol-anchored **notes** — durable, committed annotations keyed by
  symbol id. `dekko note add <symbol> "<text>"`, `note list [<symbol>]`
  (with `--orphaned` to find notes whose symbol moved), and
  `note rm <symbol> [INDEX]`. Notes live in `.dekko/notes.json` and are
  shown inline by `dekko query symbol` and `dekko context` (toggle with
  `--notes/--no-notes`, default on). Exposed over MCP as `add_note` and
  `list_notes` (14 tools total). The plugin ships a `dekko-notes` skill
  telling Claude Code to consult notes before editing, write them after
  non-obvious changes, and re-anchor them after a rename.
- `dekko summary` — a ~40-line repo digest meant to be read whole:
  file/symbol/edge counts, language mix, a per-directory rollup (file
  and symbol counts, internal vs cross-directory coupling, and a
  purpose line from the directory's index/module docstring), the
  load-bearing (fan-in) and orchestrating (fan-out) symbols, likely
  entry points, and parse errors. `--json` and `--no-tests` like the
  other read commands. The `/map` plugin command now prints this digest
  instead of a raw byte count, and points the agent at the query
  surface rather than the full `.dekko/MAP.md`.
- The MCP server now serves resources: `resources/list` /
  `resources/read` expose `dekko://summary`, and a matching `summary`
  tool covers clients that only call tools (12 tools total).
- `dekko affected [REV]` — the test files a runner should exercise
  after a change. Combines two kinds of evidence: reverse call-graph
  reachability from every added/changed symbol (`direct` at one hop,
  `transitive` beyond), plus an always-on import-edge fallback
  (`import`) that catches tests touching changed *files* through
  fixtures, references, or deleted symbols where no call edge
  survives. Prints a ready-to-paste `pytest …` line; `--json`,
  `--limit`; exit `0` none / `1` impacted / `2` bad rev. Exposed over
  MCP as the `impacted_tests` tool (the server now has 11 tools).
  Static analysis can't see fixture injection or dynamic dispatch, so
  the report is a set of strong leads, not a proof of completeness.
- Context packs (v2): the target and every neighbor now carry their
  doc first line; new strictly-opt-in `--with-source` inlines the
  target's body plus the exact call-site lines (`> line: code`) of
  hop-1 callers. Source counts against `--budget` and is truncated
  from the bottom (with a marker) after neighbors are trimmed — the
  target's signature and location always survive. The MCP
  `get_context_pack` tool accepts a matching `with_source` flag.
  JSON output gains `doc` on symbols, `sites` on neighbors, and
  `source`/`source_truncated` when source is requested.
- `dekko query callers|callees X --sites` — one row per call site
  (`path:line` of each call expression) instead of one per related
  definition. The MCP `get_callers`/`get_callees` tools accept a
  matching `sites` flag.
- `dekko query uses NAME` — list every symbol that references an
  external (out-of-repo) name such as `Path` or `run`, with call
  sites; exposed over MCP as the new `find_usages` tool (the server
  now has 10 tools).
- `--no-tests` on `query`, `context`, `trace`, `unused`, and `stats` —
  excludes test files' symbols and edges from results entirely (a
  bare-name query that collided with a test fixture now resolves).
- Text output of `query` and `context` ends with a `(~N tokens)`
  self-metering footer (never present with `--json`).
- `map.json` doc version **3** (older documents still load, with
  defaults for the new fields):
  - Call edges carry `lines` — the sorted, deduplicated 1-based lines
    of every call site backing the edge. External calls do too.
  - Symbols carry `doc` — the first line of the symbol's docstring or
    doc comment, extracted best-effort per language (Python
    docstrings; `///`/`//!` for Rust; `//` blocks for Go; `/** */`
    and `//` for JS/TS/Java/C/C++; preceding comments for Tier-2
    grammars). Files carry a module-level `doc` the same way.
  - Symbols carry `test` — whether the defining file is test code
    (path-based: test directories and filename patterns).
- New `classify` module hosting the shared test-path classifier
  (moved from `unused`, which now imports it).

### Changed
- Freshness checks are faster on large repos: provenance records an
  `(mtime, size)` signature per file, and a file whose signature is
  unchanged is no longer re-hashed. The content hash still decides for
  any file whose stat moved, so verdicts are unchanged; maps written
  before this release fall back to hashing every file.
- External calls in `map.json` always name their caller: module-level
  calls use the `path::<module>` convention instead of `null`, and
  every entry records its call-site lines.
- The `.dekko/` directory now governs its own ignores via an inner
  `.gitignore` (`*`, `!.gitignore`, `!notes.json`) and dekko no longer
  adds a blanket `.dekko/` entry to the repository `.gitignore` —
  generated maps and the cache stay ignored, while `notes.json` is
  trackable. (A repo whose `.gitignore` already excludes `.dekko/` from
  an earlier version must drop that line for notes to be committable.)

## [0.7.1] — 2026-06-12

### Added
- `dekko --claude-uninstall` — reverses `--claude-install`, removing the
  bundled plugin and its marketplace registration.
- `dekko --mcp-uninstall` — reverses `--mcp-install`, removing the
  standalone MCP server (`claude mcp remove dekko`).

### Changed
- Renamed from **lidar-map** / `lidar` to **dekko** / `dekko` before the
  first PyPI release. The PyPI package, CLI command, Python import package,
  cache directory (`.dekko/`), and MCP server name all changed; no published
  packages were affected.
- `MAP.md` and `map.json` are now written into the `.dekko/` directory by
  default (alongside the cache) instead of the repository root; `--output`
  still overrides the location.
- The gitignore wiring (the inner `.dekko/.gitignore` and the `.dekko/`
  entry in the repo `.gitignore`) is now written only when a run actually
  creates the `.dekko/` directory. If `.dekko/` already exists, gitignores
  are left untouched — removing either entry is no longer undone on the
  next run.

### Fixed
- `install.sh` invokes the freshly installed CLI by absolute path — a
  repo-local `.venv/bin/dekko` could shadow it on `PATH` and break
  `--claude-install` — and forces a rebuild with `--refresh-package`, so a
  re-install at the same version no longer reuses a stale cached wheel.

## [0.7.0] — 2026-06-12

Close out the roadmap backlog: path tracing, a complete MCP surface, and
extractor/resolver correctness and performance work.

### Added
- `dekko trace FROM TO` — shortest call path(s) between two symbols over
  the resolved graph (`--max-paths K`, `--json`). "No path" is a clean
  exit `1`, not an error; unknown/ambiguous endpoints exit `3`/`4` like
  the other read commands. It auto-regenerates a stale map.
- Three new MCP tools so the server now mirrors the whole read surface
  (nine tools): `trace_path`, `find_unused`, and `stats`.
- `dekko map --jobs N` — parallel extraction across a process pool
  (`0` = all cores; sequential by default). Cache hits stay in-process and
  results re-assemble in discovery order, so output is identical to a
  single-worker run.

### Changed
- The `.dekko` extraction cache is now tagged with the `dekko`
  version and discarded on a version change, so an upgrade re-parses once
  and always reflects extractor changes (no manual `--full`).
- Resolver same-file and self-container checks use a pre-built
  `(name, path)` bucket instead of rescanning every repo-wide candidate,
  cutting the worst case for very common names. Resolution results are
  unchanged.

### Fixed
- Relative-import sources no longer double the leading dot
  (`from . import x` rendered as `..x`); they now read `.x` / `..x` /
  `.pkg.x` correctly in context packs.

### Documented
- A "Limitations" section in the README: calls inside Rust macro bodies
  are invisible to tree-sitter token trees, and dynamic dispatch has no
  static call site.

## [0.6.0] — 2026-06-12

Graph analysis: turn the map into a source of code-health insight.

### Added
- `dekko unused` — symbols with no inbound calls, minus roots (`main`,
  test files, decorated/annotated symbols, the language's public surface
  — Rust `pub`, Go capitals, Java `public`, JS/TS `export` — Python
  dunders and `__init__.py` re-exports, plus `--roots GLOB`). A class is
  kept when any of its methods is called. `--limit`, `--json`; exits `1`
  when any are found. It is call-graph based, so it reports leads, not
  verdicts.
- `dekko stats` — file/symbol/edge totals, language mix, top fan-in/out
  hotspots, and largest files (`--top`, `--json`).
- `dekko export` — render the call graph as `--format mermaid|dot`, at
  `--scope symbol|file`, with a `--max-nodes` guard.
- `Symbol` now records `decorated` and `exported` facts (Python
  decorators, Rust attributes/`pub`, Java annotations/`public`, JS/TS
  decorators/`export`), serialized into map.json.
- A test asserting the four declared version strings (pyproject, both
  plugin manifests, uv.lock) agree.

## [0.5.0] — 2026-06-12

Expose the map to agents over the Model Context Protocol.

### Added
- `dekko serve --mcp` — a hand-rolled MCP server speaking
  newline-delimited JSON-RPC 2.0 over stdio, with **no SDK dependency**.
  Six tools mirror the read surface: `query_symbol`, `get_callers`,
  `get_callees`, `get_context_pack`, `map_status`, `refresh_map`.
- The plugin ships an `.mcp.json` (with `cwd` set to
  `${CLAUDE_PROJECT_DIR}`), so `dekko --claude-install` wires the server
  automatically.
- `dekko --mcp-install` registers the server for non-plugin setups via
  `claude mcp add dekko -- dekko serve --mcp`.

### Changed
- Map regeneration was factored into a reusable `regen_map` helper so the
  server can force a full rebuild.

## [0.4.0] — 2026-06-12

Change-awareness and incremental mapping.

### Added
- `dekko diff [REV]` — symbols added/removed/changed since a git rev
  (default: the commit the map was generated at), each with its impacted
  callers. Compares the working tree against `git archive` of the rev;
  "changed" means the symbol's source text differs. `--limit`, `--json`;
  exits `0` (no differences) / `1` (differences) / `2` (bad rev).
- A per-file extraction cache under `.dekko/`, keyed on the provenance
  content hash, so re-mapping only re-parses files whose contents
  changed. `dekko map --full` forces a cold rebuild.

### Changed
- The first time the cache is written, `.dekko/` is made self-ignoring
  and appended to the repository `.gitignore`.

## [0.3.0] — 2026-06-12

From a one-shot generator to a queryable context service.

### Added
- A subcommand CLI: `map`, `query`, `context`, `status`. The v0.2 flags
  (`--map`, `--claude-install`, `--version`) keep working as aliases.
- `dekko query` — `callers`, `callees`, `symbol`, and `file` lookups
  against map.json, with exit codes `3` (not found) and `4` (ambiguous).
  Targets accept `name`, `Class.method`, or `file.py:name`.
- `dekko context` — a minimal signature neighborhood for editing a
  symbol, with `--hops N` and a `--budget TOKENS` trimmer.
- `dekko status` — freshness report from the provenance stamp; exits `0`
  (fresh) / `1` (stale).
- map.json provenance (document version 2): tool version, git commit,
  discovery options, and per-file content hashes.
- Read commands auto-regenerate a stale map (`--no-regen` to opt out);
  `dekko map --if-stale` short-circuits when the map is already fresh.

## [0.2.0] — 2026-06-11

Packaged for distribution.

### Changed
- Converted from a `uv`-run script into a pip-installable package:
  `tool/` → `src/dekko/`, a hatchling build, and a `dekko` console
  script. Distributed on PyPI as **dekko**.
- The Claude Code plugin is embedded in the wheel and installed with
  `dekko --claude-install`.

### Added
- `--map [DIR] [SUBPATH]`, `--output`, `--claude-install`, and
  `--version` flags.
- A GitHub Actions release workflow using PyPI trusted publishing.

## [0.1.1] — 2026-06-11

### Fixed
- `/map` permission failure caused by command substitution in the
  command preamble.
- A Python 3.11+ f-string that failed to compile on the declared 3.10
  floor.
- Repeated tree-sitter query recompilation (now cached), cutting a
  representative run from ~0.26s to ~0.17s.

### Added
- A test that compiles every tool module against the declared Python
  floor.

## [0.1.0] — 2026-06-11

Initial release: the **dekko** Claude Code plugin.

### Added
- A `/map` command that scans the repository with tree-sitter and writes
  `MAP.md` (files, functions, parameters with types, return types, and
  bidirectional call links) plus a machine-readable `map.json` — without
  spending model tokens on parsing.
- Tier-1 languages with full type fidelity (Python, Rust, C, C++,
  JavaScript, TypeScript/TSX, Go, Java) and a generic Tier-2 fallback for
  every other grammar in the language pack.
- Best-effort static call resolution (same container → same file →
  imports → unique repo-wide match); ambiguous calls are marked, never
  guessed.

[Unreleased]: https://github.com/aahlijia/dekko/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/aahlijia/dekko/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/aahlijia/dekko/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/aahlijia/dekko/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/aahlijia/dekko/releases/tag/v0.7.1
[0.7.0]: https://github.com/aahlijia/dekko/releases/tag/v0.7.0
[0.6.0]: https://github.com/aahlijia/dekko/releases/tag/v0.6.0
[0.5.0]: https://github.com/aahlijia/dekko/releases/tag/v0.5.0
[0.4.0]: https://github.com/aahlijia/dekko/releases/tag/v0.4.0
[0.3.0]: https://github.com/aahlijia/dekko/releases/tag/v0.3.0
[0.2.0]: https://github.com/aahlijia/dekko/releases/tag/v0.2.0
[0.1.1]: https://github.com/aahlijia/dekko/releases/tag/v0.1.1
[0.1.0]: https://github.com/aahlijia/dekko/releases/tag/v0.1.0
