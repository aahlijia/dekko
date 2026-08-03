# Changelog

All notable changes to **dekko** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Dates are when the work landed on `develop`; releases are cut by pushing a
`v*` tag.

## [Unreleased]

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
