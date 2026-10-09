# Changelog

All notable changes to **dekko** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Dates are when the work landed on `develop`; releases are cut by pushing a
`v*` tag.

## [Unreleased]

## [1.8.20] — 2026-10-09

### Fixed
- **`diff` has a budget.** It had no cap: `diff HEAD~500` on cline
  printed 2 MB. `--budget TOKENS` (default 4,000, `0` for no cap) keeps
  changed symbols first, then added and removed, with an `N of M
  omitted` footer; `--json` gets `meta`.
- **`affected` says what it saw when nothing is certain.** With only
  possible impacts it now reads `no certain impacted tests ...; N
  possible`, and after an edit that changed no symbol (a comment,
  whitespace, top-level code) it says how many files changed, instead
  of a bare "no impacted tests" that read as "safe". `--json` gets
  `changed_files` and `changed_symbols`.
- **The symlink note counts code files only.** A symlinked
  `LICENSE-GPL` or `AGENTS.md` can't hold a symbol, yet zed's 262 of
  them put a 600-character note on every empty reply. Takes effect on
  the next `dekko map`.
- **A closed pipe ends the command quietly.** `dekko lean | head`
  printed a `BrokenPipeError` traceback; it now exits 141 with nothing
  on stderr.

## [1.8.19] — 2026-10-09

### Fixed
- **`search` puts an exact name first.** A one-word query that is a
  symbol's name (or a dotted query matching its qualname) now ranks it
  ahead of busier partial matches, exact case first: claude-code
  `search Text` listed the `Text` component 60th because nothing calls
  it.
- **`search --limit 0` no longer prints `(no matches)`** above `N of N
  omitted`; the marker now means nothing scored.
- **`deps` lists missing in-repo imports as `unresolved`**, not beside
  npm packages under `external`: a relative `./types/message.js` whose
  file doesn't exist, or a root-relative `src/...` path. The summary
  counts them separately and `--json` carries both lists.
- **`outline`/`query file` say why an existing path isn't mapped**: no
  grammar for its extension, a directory with no mapped files, a
  symlink, over the size cap, or ignored/vendored/excluded. A missing
  path keeps the plain "no mapped file" line, so a typo and a skipped
  file no longer read the same.

## [1.8.18] — 2026-10-09

### Fixed
- **`query uses <module>` counts named imports as members.** A bare
  `join(..)` after `import { join } from 'path'` is a call of
  `path.join`, but the header's `top members` only counted
  `path.join(..)`-shaped calls: claude-code `uses path` said `join 41`
  for 687 such calls.
- **`query callers --json` counts references in `meta`.** A symbol with
  no callers but some value references (a callback passed by name)
  had `meta.total 0` and its references only under `referenced_meta`;
  `meta.referenced` now carries their total.
- **`--transitive`/`--depth` on `callers` and the other non-walking
  actions say they're ignored**, pointing at `trace` and `workset`,
  instead of answering one hop as if they'd been read.
- **An empty `query type` points at the type's references.** A type
  used only in fields, locals and literals has no signature uses, so
  `query type` found nothing; the reply now says those positions
  aren't recorded and how many callers and references `query callers`
  lists.

## [1.8.17] — 2026-10-09

### Fixed
- **A leading `./` names the same file.** `outline ./src/a.py`, `query
  file ./src/a.py` and `query callers ./src/a.py:helper` used to say
  "no mapped file"; every path and target now drops it.
- **An id's `#N` works after a single colon.** `path:Foo.run#2` used to
  find nothing while `path::Foo.run#2` worked.
- **`query --lang` narrows any symbol target.** It took only the six
  throws/catches languages and only filtered those two actions, so
  `query symbol main --lang python` still listed every `main`. It now
  takes every mapped language, keeps only that language's candidates
  on every symbol action, and says which languages a name does have
  when none is in the one asked for. `throws`/`catches` still exit 2 on
  a language they have no data for.
- **A mistyped command exits 2.** `dekko serach` printed the help and
  exited 0; it now says `unknown command 'serach'`, suggests `search`,
  and exits 2.

## [1.8.16] — 2026-10-08

### Fixed
Four ways the 1.8.14-1.8.15 `sanity` rules could relabel a real missed
call as explained:
- **A Rust turbofish with a nested generic** (`.cursor::<Dimensions<A,
  B>>(..)`) read as a bare identifier, because the turbofish match
  stopped at the first `>`. Any `.name::` or `.name(` now reaches the
  method (zed `SumTree.cursor`: 38 such rows).
- **An assignment that also calls the name** (`command =
  command(xs)`) read as a write. A call of the name after the `=` now
  refuses the assignment label.
- **A Swift line calling a C function** read "dekko never links calls
  across languages", which is false: the resolver links Swift to C
  functions. The other-language rule now asks the resolver's own table
  (`resolver.can_link_across`) instead of keeping a copy, so the two
  can't drift; Groovy and Scala lines against Java now count as other
  languages, as the resolver treats them.
- **A closure, `||` chain or comprehension declared a local for later
  lines.** A Rust closure parameter now binds below its line only when
  the body stays open (`|x| {`), `||` never opens a parameter list, a
  Python `lambda` binds only on its own line, and only a `for`
  statement's target (not a comprehension's) binds.

## [1.8.15] — 2026-10-08

### Fixed
- **A name inside a Rust, Python or Go string reads as a string
  mention**, as it already did in JS/TS and the JVM languages (zed's
  `log::info!("Task trace ...")`, awesome-go's `" **Link consistency**:
  ..."`). Strings are found over the whole file, so the inner lines of
  a docstring or a multi-line raw string count; a Rust `{name}` format
  argument and a Python f-string field stay code.
- **Same-named locals are recognized in Rust and Python.** A `let` or
  `if let` pattern, a closure parameter (on the same line as the use
  too), a `for` pattern, a Python assignment, `for` target, `with` /
  `except ... as` or `lambda` parameter now explains a use below it, as
  in JS/TS. zed: the free function `callback` 603 unexplained rows → 71.
- **TypeScript leftovers.** `import X, { y } from '...'` lines read as
  imports for both names; `function name(` overload heads read as
  signatures; a generic argument in any position of an angle-bracket
  list (`ToolDef<In, Output, Prog>`) reads as a type position.
- **An assignment to the name** (`activityCallback = cb`, `count += 1`)
  in JS/TS, Python or Rust reads `assignment to the name — a write, not
  a call`. claude-code `sanity --all` unexplained: 40 → 21.
- `sanity --all`'s `aggregate_causes` gains the assignment key.

## [1.8.14] — 2026-10-08

### Fixed
- **A bare identifier no longer reads as a missed method call in Rust,
  Python, JS/TS or Go.** Those languages reach a method only through a
  receiver or a path, so on a method target a line whose occurrences
  are all bare (`harden: bool` for a method `bool`, `app_state.client`
  for a method `app_state`) now reads `bare identifier, not the method`.
  zed: `WorkflowInput.bool` 2,394 unexplained rows → 0,
  `Workspace.app_state` 474 → 0. A Python hit inside the method's own
  class is left alone (`@name.setter`), and Java, Kotlin, C++ and C#,
  where a bare call can be a method, are not judged.
- **Rust type positions read as type mentions.** On a Rust struct, enum,
  trait or alias, a line with no `Name {` or `Name(` (`Task<()>`,
  `Task::ready(..)`, `(SharedString, usize)`) now reads `names the type
  without constructing it`, as Java and Kotlin lines already did. zed:
  `scheduler::Task` 989 → 6 (match patterns). Unit structs are skipped.
- **A line in another language says so.** A JS `throw new Error(..)`
  against a Java `Error`, a Python or shell line against a TS method:
  `a <hit lang> line, the target is <target lang>: dekko never links
  calls across languages`, with the two grammars as `languages` on the
  row, and a note that a binding layer (pybind, JNI, FFI) may still
  reach the target.
- Text mode now appends a row's `external_callee` (from 1.8.13) and
  `languages`.
- `sanity --all`'s `aggregate_causes` gains the bare-identifier and
  other-language keys, and the type-mention key's text changed.

## [1.8.13] — 2026-10-08

### Fixed
- **`sanity` no longer calls a comment, a string or an unparsed file a
  resolver blind spot.** The qualified-call rung ran before the
  comment, string and unparsed-language rungs, so `// ccr.close() ran
  first`, `"call x.close() first"` and a README's `conn.close()` all
  read as calls the resolver might have missed. Those facts now come
  first; none of those lines ever reaches the resolver.
- **A qualified call says what the resolver decided.** A grep-only
  `x.name(..)` row now reads the map at that line: `dekko recorded this
  call as external` (with the callee as `external_callee`) or `the
  resolver found 2+ in-repo candidates for a call of this name here and
  picked none`. Only a call the map has no record of keeps the
  blind-spot label, reworded to `qualified call the map has no record
  of` ("cross-package" was wrong for same-package calls). claude-code:
  1,636 of 1,663 such rows in the top targets were recorded.
- **`Cursor.fromText(..)` for target `Cursor`** reads `names the class
  as the receiver of a static member`, not "passed or stored as a
  value".
- `sanity --all`'s `aggregate_causes` gains the keys for the recorded
  external, recorded ambiguous and static-member causes, and the
  qualified-call key's text changed.

## [1.8.12] — 2026-10-08

### Fixed
- **`sanity` on a symbol that only exists in test code checks it.** The
  default run excludes tests, so such a target used to answer "no
  symbol matches" with unrelated suggestions while `query` found it.
  Every caller of a test symbol is test code too, so the run now
  includes tests for it and says so on its first line (`note: '<id>' is
  test code; checked with --include-tests`, and `note` in `--json`,
  whose `include_tests` reports the value used).
- **`sanity --all` names each row by its symbol id.** Rows printed
  `path:qualname`, so a class's overloaded constructors showed twice
  under one string that `sanity <target>` then called ambiguous
  (spring-boot: 95 such pairs). Rows now carry the id
  (`path::qualname#2`), one per overload, each re-runnable as-is.

## [1.8.11] — 2026-10-08

### Fixed
- **`dekko map` no longer hangs at exit after writing the map.** A run
  that built several process pools in a row (adding a file makes every
  resolve pass run pooled) could fork the next pool's workers while the
  previous pool's manager thread still held a lock. Each worker
  inherited the lock held and blocked on it at its own exit, and the
  parent then waited on that worker forever at interpreter exit. Every
  pool is now closed with a bounded join of its manager thread and
  workers (a wedged worker is killed after 30 s), so the process is
  single-threaded again before the next fork. Python 3.12 and newer.
- **The fork-or-spawn choice is made at every pool build.** It used to
  be made once per process, so a later pool forked even with threads
  alive. A thread still alive at a build now sends that pool to
  `spawn`. The CLI keeps `fork` everywhere it had it.

## [1.8.10] — 2026-10-08

### Fixed
- **A Java, Kotlin or C++ field used without `this` reaches its type.**
  `repository.findAll()` inside a class with a `Repo repository` field,
  Kotlin's `repo.findAll()` through a `private val repo: Repo`
  constructor parameter, and C++'s `delegate_->Run()` or
  `owned_->Run()` through a `std::unique_ptr<Repo>` member are walked
  the way `this.repository.findAll()` already was: to the field type's
  member, external when the type is outside the repo
  (`logger.info(..)` on commons-logging, `DEFAULT_DOMAIN.equals(..)` on
  a `String` constant, which used to land on `ImageName.equals`), and
  on through longer chains (`repository.helper.go()`). A parameter or
  local of the same name, lambda parameters included, still wins over
  the field.
- **A bare call to a parameter is not a call to a repo function of that
  name.** `makeC(requestCapability)` returning `() =>
  requestCapability(..)` calls its argument, not the repo's own
  `requestCapability`; Python's `def apply(f): f()` likewise. Those
  calls are external now, in Python, JavaScript and TypeScript, where a
  function's name is a value a parameter shadows. Where the function is
  passed in under its own name (`handler(.., responseStream, ..)`), the
  passing site is still a reference to it. A called local keeps the
  edge it had: it usually holds the function it is named after
  (`const { run } = helpers`).

Against 1.8.9, edge sites and ambiguous rows:

| Repo | Edge sites | External sites | Ambiguous rows |
|---|---|---|---|
| spring-boot | +87 −17 | +604 −18 | 45,821 → 45,316 |
| tensorflow | +2,141 −1,073 | +3,890 −146 | 190,447 → 187,242 |
| cline | +0 −139 | +427 −0 | 3,671 → 3,484 |

tensorflow's C++ gains are typed members (`input_impl_->GetNext(..)` is
`IteratorBase.GetNext`); most of its losses are `std::` containers that
had landed on a same-named repo method (`execution_plan_.size()` on
`TfLiteIntArrayView.size`). Some C++ classes the map doesn't extract
(declared behind a macro, or in a vendored directory) make their fields
read as outside the repo, so a few right edges go external too. cline's
and tensorflow's Python losses are all bare calls to a parameter.
Map time is within 3% of 1.8.9.

## [1.8.9] — 2026-10-07

### Added
- **`map.json` records each type's fields.** Every class, struct,
  interface, enum, record and trait symbol carries `fields`: each
  field's `name`, declared `type`, `line`, and whether the type was
  `inferred` from a construction call or literal initializer
  (`this.client = new Client()`, `self.items = []`). TS parameter
  properties and interface members, Python `self.x` assignments in any
  method, Rust tuple-struct fields (`"0"`, `"1"`), Go embedded fields,
  Java record components and enum constants, Kotlin `val`/`var`
  constructor parameters and C/C++ members are all read. A Go method's
  receiver is now a leading parameter with `receiver: true`; signatures,
  `outline`, arity and `query type` skip it as before.

### Fixed
- **A call through a field reaches the field's type.** `this.mcpHub.callTool()`
  with `constructor(private readonly mcpHub: McpHub)` now has
  `McpHub.callTool` as its target, where it used to have no edge at
  all; so does `controller.mcpHub.getServers()` through a typed
  parameter, `Type.FIELD.m()`, a Go receiver's `s.store.Get()`, an
  inline object-type parameter's `input.client.getSchedule()`, and a
  field declared on a supertype. The walk goes on through a call's
  declared return type (`cx.executor().run_until_parked()` is
  `BackgroundExecutor`'s), through calls that hand back what they're
  called on (`lock()`, `read(cx)`, `borrow()`, `unwrap()`, `.await`),
  through fluent builders returning `Self`, `this` or `SELF`, nested
  types (`ConfigData.Options.of()`), enum constants
  (`PeriodStyle.SIMPLE.parse(..)`) and Java's `Outer.this`.
- **A chained receiver never lands on the enclosing type's own method.**
  `self.center.panes()` inside a type with its own `panes` used to pick
  that `panes`; it now goes to the field's type, and an untyped field
  leaves the call to the rest of the resolver without that pick.
- **A field whose type is outside the repo sends the call external.**
  `this.subscriptions.delete(id)` on a `Map` and `self.items.len()` on a
  `Vec` no longer land on a repo method named `delete` or `len`. A repo
  type with no such member is external too (a Spring repository's
  `findAll()` from `JpaRepository`), except where the type can reach
  members the map doesn't list: a Rust `Deref`, a Python `__getattr__`,
  C/C++, a TS interface an object literal can fill, or a field that is
  itself a callback slot (`this.options.postStateToWebview()`).
- **An interface-typed field dispatches to its implementors**: one is
  the edge, several are an ambiguous row naming only them.
- **An aliased type import types a parameter or field.** `function
  f(v: WD)` with `import { Widget as WD }`, and Python's `from w import
  Widget as WD`, reach `Widget`'s methods.
- **`sanity` names three more causes** for a call through a field: a
  receiver whose declared type is outside the repo, a field with no
  type, and an interface member nothing implements. They replace the
  "cross-package/qualified call" label on those lines.

Against 1.8.8, edge sites and ambiguous rows:

| Repo | Edge sites | Ambiguous rows |
|---|---|---|
| zed | +14,883 −2,516 | 83,424 → 75,139 |
| spring-boot | +13,338 −418 | 61,299 → 45,821 |
| tensorflow | +2,061 −854 | 192,069 → 190,447 |
| cline | +443 −114 | 4,070 → 3,671 |

Most lost sites are calls on std or library types that had landed on a
same-named repo method; on zed about 1,650 moved to another target
(`TestAppContext.run_until_parked` to `BackgroundExecutor`'s).
`McpHub.callTool` gains its caller on cline. Depth-zero calls resolve
as before (`Point.new` keeps 569 callers on zed). Map time and peak
memory stay within 5% of 1.8.8. Limits: extension traits implemented
for every type (`with_rotate_animation` on any element) and a local's
type read off its initializer are not followed yet, and a Python field
built by a native extension (pybind) no longer reaches the C++ class of
the same name. An older dekko can't read a map with a Go receiver in
it (`unexpected keyword argument 'receiver'`): restart long-running
`dekko serve`/daemon processes after upgrading.

## [1.8.8] — 2026-10-07

### Fixed
- **Rust `Self::name`, `<T>::name`, same-file paths and bare calls get
  the type narrowing.** These shapes skipped the rule that keeps a
  `Type::name(..)` path to `Type`'s own members, so a row could carry
  every `new` in the repo. Now:
  - `Self::name` reads as the impl's type: `Self::new(..)` inside
    `impl From<..> for ProcessIdGetter` reaches `ProcessIdGetter.new`,
    a trait default method's `Self::get_global(cx)` reaches the
    trait's, and `Self::Variant(..)` no longer lands on a struct of
    the same name elsewhere.
  - A whole `<T>` or `<T as Tr>` receiver reads as `T`:
    `<Vec<_>>::new()` is external, `<SharedString as
    Element>::paint(..)` reaches `SharedString.paint`.
  - A written path that ends a qualname in the caller's own file names
    that symbol when the rest of the qualname is an inline module the
    caller sits in: `sys::DisplayLink::new` reaches the
    macro-made type's `new` in `mod sys`, and inside `mod tests` a
    `StubAgentServer::default_response()` is the test module's own
    `StubAgentServer`, not `test_support.rs`'s.
  - A bare call never names a method: `new(cx)` next to the file's own
    `fn new` and an `impl X { fn new }` is the free function, and a
    bare `handler()` with only methods of that name is external.

On zed against 1.8.7: edge sites +268 −40, ambiguous rows 83,891 →
83,424; 464 bare call sites that were ambiguous among methods go
external. Of the 15 rows that carried all 1,386 `new` candidates, 4
are left: two cfg-gated aliases of one name, `Rc::new`, and
`NotificationResponseDelegate::new`, whose type `define_class!` makes.
The 40 edges lost are 16 `Self::Variant(..)` and derived
`Self::default()` calls that had landed on an unrelated struct or
`default`, 23 `StubAgentServer::default_response()` calls that moved to
the test module's own type, and one `<fs::Permissions as
..>::from_mode` that had reached terminal's `MouseFormat.from_mode`.
cline is identical; map time unchanged. Limits: a `<T>` whose type is
an associated type (`<T::Summary as Summary>`) stays with the ladder,
and a one-segment type path settles on a same-file member only when
the file declares the type (`String::from(..)` next to the file's
`impl From<X> for String` is std's).

## [1.8.7] — 2026-10-07

### Fixed
- **`uses <crate>` finds a Rust crate called by path.** `dekko query
  uses serde_json` (and MCP `find_usages`) counted a row only when
  `serde_json` was an import binding of the calling file, and a crate
  called as `serde_json::from_value(..)` never is: zed showed 36 sites
  in 3 files. `uses futures` said "not found" and called the 350
  `futures::` sites "local variables, not a module". A Rust row written
  `<crate>::..` now matches as the new `path` kind, a Rust file counts
  as importing the crate when any `use` starts with it (`use
  serde_json::Value;`), and the "local variables" note no longer fires
  on a `::` head (a C++ namespace isn't a local either).

On zed (1.8.6 map): `serde_json` 36 sites in 3 files → 1,963 in 263
(importing 6 → 238); `futures` not found → 341 sites in 129 files;
`smol` not found → 302 in 47; `itertools` not found → 16 in 10;
`anyhow` 231 in 56 → 517 in 171. Every listed site is on a line `git
grep '<crate>::'` finds; a chained call counts each link
(`smol::process::Command::new(..).output()`), as JS chains already
did. `uses` on claude-code (`chalk`, `React`, `path`, `fs`) and
tensorflow (`numpy`, `np`, `absl`) is unchanged row for row.

## [1.8.6] — 2026-10-07

### Fixed
- **A Rust path whose head the repo doesn't have is external.**
  `serde_json::from_value(..)` with no `use serde_json` in the file
  ran the resolver's ladder over every `from_value` in the repo and
  landed on `repl`'s `JsonView.from_value`. A path head with no
  binding in the file is now looked up the way rustc does: an inline
  `mod` or child module of the file, or a `use` reached through its
  globs, keeps it in the repo. A `use super::*` that reaches the
  parent's `use agent_client_protocol::schema::v1 as acp` makes
  `acp::SessionId::new(..)` external, where it had landed on
  `scheduler`'s `SessionId.new`. A head nothing binds that is a
  primitive type (`f32::from(..)`) or names no crate and no module of
  the repo is external. A head naming a crate or module of the repo is
  unchanged, and so is a local variable that shares a crate's name
  (`regex.is_match(..)`).

On zed against 1.8.5: edge sites −374 (serde_json 229, `acp` 85,
primitives 12, the rest single extern crates such as `regex::escape`
landing on `Markdown.escape`), none gained; ambiguous rows 84,858 →
83,891; 1,680 call sites go external across 107 heads (serde_json
636, smol 149, futures 116, `f32` 103). Callers of `scheduler`'s
`SessionId.new` went from 63 to the 1 real one; rows carrying every
`from` in the repo went from 107 to 22. Seven Rust `impl` clauses go
external, five of them `bindgen!`-generated `common::Host`-style
traits that had landed on `recent_projects`' `Host`. The module graph
is unchanged and map time is unchanged. cline is identical; the other
eval repos have no Rust. Limits: only lowercase heads are tested, and
an inline module with no symbols of its own (one a macro fills) reads
as outside.

## [1.8.5] — 2026-10-07

### Fixed
- **A Rust type path is walked to the type it names.** `use
  rope::Point;` then `Point::new(..)` means the `Point` that `rope`'s
  root re-exports (`pub use point::Point;`), but dekko tested the
  path's segments as file stems, so the call stayed ambiguous among
  every `Point.new` in the workspace (gpui's, rope's and terminal's).
  The path is now followed module by module: a crate (its lib root
  from `Cargo.toml`, including a `[lib] path` outside `src/`), a
  module file, then the item, through `pub use` re-exports, renames
  and globs. A path's first segment is looked up in the file's own
  modules and `use`s, then through its globs, before any crate name,
  as rustc does. A member of a type of that name in a crate that
  declares a type of that name of its own is then ruled out. Trait
  members, and
  impls in a crate that declares no such type (`impl ToTsPoint for
  rope::Point`), stay candidates. A derived member the named type
  lacks no longer lands on another crate's type of the same name
  (`sandbox`'s own `SandboxPermissions::default()` reached
  `agent_settings`'s).
- **Rust globs are recorded.** `use a::*;` was dropped at extraction;
  it is now an import (`*`, `a::*`) and a module-graph edge to `a`. A
  `use` written inside an inline `mod tests { .. }` is re-based to the
  file's own module (`use super::*;` there is the file itself, so it
  binds nothing new, and `use super::X;` is `self::X`). Before, a
  `use super::X;` in a test module pointed one module too high.
- **A sole Rust candidate reached through a trait path counts its
  `self`.** `text::ToOffset::to_offset(&anchor, snapshot)` passes the
  receiver as its first argument; read as an associated function it
  had two arguments for one parameter and went external. The
  associated-function reading is still preferred wherever it tells two
  candidates apart.

On zed against 1.8.4: edge sites +1,957 −36, ambiguous rows 85,752 →
84,858. Callers of `rope`'s `Point.new` went from 16 to 569 (617
ambiguous rows named it, 29 still do). The 36 lost are 34 derived
`default()` calls that now go external, plus two moves to the right
target (`RenderOnce::render(self, ..)`, and `GpuiPoint::new` through
`gpui::Point as GpuiPoint`). The module graph gains 955 glob edges
and drops 6, which were wrong (a test module's `use super::X` read as
the parent file's, and one self-edge). `deps --cycles` finds 112
cycles (90 before), each still inside one crate: a root that globs its
modules while they glob it back. Map time unchanged. cline's Rust is
unchanged; spring-boot, tensorflow, claude-code and awesome-go have
none. Limits: only `Type::name(..)` paths use the walk (a bare
call through a re-exported function is unchanged), and an overload set
within one type (several `impl From<X> for T`) stays ambiguous. The
extraction cache refreshes on upgrade, so the first `dekko map` after
it is a full one.

## [1.8.4] — 2026-10-07

### Fixed
- **A Kotlin `f<T>(x)` is a call.** tree-sitter-kotlin parses a
  generic call with exactly one argument and a plain or dotted type
  argument, `runApplication<App>(*args)`, as the comparison
  `(runApplication < App) > (*args)`, so dekko recorded no call: a
  Spring Boot `main` showed nothing calling `runApplication`. That
  shape is now recorded as the call it is, with its receiver
  (`json.decodeFromString<Foo>(text)` → `json`) and one argument, or
  an unknown count for a spread. Kotlin's compiler reads it as a call,
  and as a comparison it would compare a `Boolean` with `>`. Zero or
  several arguments, a trailing lambda, a chained call and a generic
  type argument already parsed as calls and are unchanged.

On spring-boot against 1.8.3: the 33 such sites are recorded. 3 reach
the repo's `TestEntityManager` extensions (`getPage`, `getId`,
`persistAndGetId`), 9 are external (`decodeFromString`,
`testEntityManager.find`), and 21 `runApplication` calls join the 6
already ambiguous between its two overloads (with a spread the count
is unknown, and Kotlin calls aren't judged by count). Tensorflow has
no Kotlin. The extraction cache refreshes on upgrade, so the first
`dekko map` after it is a full one.

## [1.8.3] — 2026-10-07

### Fixed
- **A Java type name the ladder can't decide is its own package's.**
  Java finds a simple type name in the enclosing classes, then the
  single-type imports, then the file's own package, then the on-demand
  imports. No rung read the package, so `new ColorConverter()` with
  one `ColorConverter` beside the caller and another in a different
  package was ambiguous, and so were most constructions in
  spring-boot's duplicated module copies (the `jdbc` and `r2dbc`
  `MySqlEnvironment`, the two `JSONException`). Now a `new X(..)` or
  `X::new` the ladder leaves ambiguous, in a file that imports no `X`,
  takes the one top-level `X` in the caller's package (the directory
  under the source root, so a test's package is its class's). The
  constructor is then picked by count as usual.

On spring-boot against 1.8.2: 1,495 call sites gained, none lost, 4
reference sites gained; ambiguous rows 62,046 → 61,278. Checked over
every pick when this was designed: no in-repo rival is a nested type
inherited from the caller's supertypes (the one way Java's scope
could beat the package), and none of the 3,333 existing cross-package
type edges contradicts the rule, so it only settles ties. `sanity` on
every type a `::new` names, with tests: 0 unexplained rows (3 at
1.8.2). Limits: Java only (Kotlin has the same rule; 19 rows on
spring-boot); a type nested in a supertype the repo can't see could in
principle shadow the package's; a file with no source root has no
package and is not judged. Tensorflow is unchanged in the design
simulation.

## [1.8.2] — 2026-10-07

### Fixed
- **A Java `X::new` is recorded.** The method-reference pattern needs
  an identifier after `::`, and `new` is a keyword token, so a
  constructor reference left nothing in the map: a class built only
  through `.map(TestContentsFilter::new)` had no inbound edge and read
  as dead. `X::new`, `X<T>::new` and `Outer.X::new` are now a
  reference to `X`, the way `Foo::bar` is a reference to `bar`: the
  constructor runs when the functional interface is invoked, and that
  interface picks the overload. When `X` has exactly one constructor
  the line can reach (a record's canonical one included), the
  reference reaches it too. `X[]::new` builds an array and runs no
  constructor of `X`; `a.b.X::new` names a package path a reference
  can't carry. Neither is recorded.
- **`sanity` reads a Java or Kotlin package from the source root.** A
  reference was credited from a same-package sibling only in the same
  directory, so a test in `src/test/java/org/x/` referencing its class
  in `src/main/java/org/x/` read "unexplained". The package is now the
  directory under the source root, for every JVM reference, not just
  `::new`. Go, and a JVM file with no source root, keep the directory.
- **`sanity` names a constructor's own `X::new` lines.** Swept as a
  target, a constructor of a class with several constructors saw its
  class's `X::new` lines as "attributed to a different, same-named
  declaration". They now read `a constructor reference (X::new) —
  recorded on the class, since the functional interface it is passed
  to picks the overload`.

On spring-boot against 1.8.1: 310 reference sites gained, none lost;
call edges, external calls and ambiguous rows unchanged.
`IncludeExcludeContentSelectorTests.TestContentsFilter` has 7
references (was 0), `AnnotationConfigServletWebServerApplicationContext`
28. `sanity` on every type a `::new` names, with tests: `::new` rows
"unexplained" 306 on 1.8.0 → 3 (a same-package tie between same-named types
the reference can't settle yet); on their constructors, 0 unexplained
and 254 rows under the new cause. Limits: a reference is not a call,
so `affected` doesn't follow `X::new`, as it doesn't follow
`Foo::bar`. Tensorflow has no `::new` and is unchanged. The extraction
cache refreshes on upgrade, so the first `dekko map` after it is a full
one.

## [1.8.1] — 2026-10-07

### Fixed
- **A Java record's canonical constructor is a symbol.** `record
  R(int a, String b)` has a constructor `R(int, String)` whether or not
  its body writes one, but dekko only extracted the constructors a body
  declares. A construction from another file was judged against the
  record's own empty parameter list, so `new BundleContentProperty("n",
  f)` read as two arguments for none and went external. The canonical
  constructor is now `R.R`, its parameters the record's components, on
  the header line, or on the compact constructor when the body has one
  (that body's calls are now the constructor's, not the record's). An
  explicit constructor with the components' types is the canonical one,
  so a record never has two. It has the record's access, so a private
  nested record's constructor gets the same access veto as a private
  method.

On spring-boot against 1.8.0: 599 call sites gained and 6 lost (5 are
compact-constructor bodies changing owner), external sites −157/+21.
`BundleContentProperty`'s 16 constructions and
`DockerCliContextResponse`'s 14 had no edge and now reach both the
record and its constructor, and records that also declare other
constructors get a real overload pick. One honest tie appears:
`Instantiator`'s canonical `(ClassLoader, Class<?>)` and explicit
`(ClassLoader, String)` both take the two arguments `new
Instantiator<>(parent, name)` writes, so that row is ambiguous.
Tensorflow has no records and is unchanged. Limits: a declared
constructor that sits after the header in source shifts from `R.R` to
`R.R#2`, so a note anchored to it orphans (`dekko note` sweeps it); a
class's implicit no-argument constructor still has no symbol; records
Spring binds by reflection can now show up in `unused` as uncalled
constructors. The extraction cache refreshes on upgrade, so the first
`dekko map` after it is a full one.

## [1.8.0] — 2026-10-06

Closes round 1.8's fix cycle. The code is 1.7.7; this release is the
version line catching up with the round, per `CONTRIBUTING.md`'s
"Testing rounds and the version line". Round 1.8 evaluated 1.6.22 on
seven real repositories and found two High issues and thirteen Medium
ones, most of them calls landing on an in-repo symbol the call's shape,
imports or access rules say it can't mean. All 7 fixes:

- **1.7.1**: a Python import is in-repo only when its first segment is
  Python the repo has, so `np.array(..)` no longer lands on the repo's
  own `array` because of a C++ `numpy.cc`.
- **1.7.2**: a Python call's shape and scope rule out picks: a bare
  call can't reach a `self`/`cls` method (`list(x)` on tensorflow's
  `Registry.list`), a module-qualified call prefers the import over a
  same-file name, and a bare call needs the name visible in its file.
  `affected` on a one-line `Registry.list` edit: 1,375 test files ->
  87.
- **1.7.3**: a C/C++ `#include` names a file by path, not by stem, and
  calls named like standard-library members (`size`, `ok`, `first`)
  don't fall back to the one in-repo match (`Status.ok` fan-in 2,242 ->
  15, all right).
- **1.7.4**: a Rust argument behind `#[cfg(..)]` leaves the count
  unknown, and a `Type::name` path can't land on another type's member.
- **1.7.5**: a Java or Kotlin call can't land on a method its file
  can't reach (private, package-private, or inside a type that is).
- **1.7.6**: a Java call is picked by the argument count it writes, a
  call no candidate can answer is external, and an ambiguous Java row
  lists only what the call could mean (spring-boot rows 68,764 ->
  62,158).
- **1.7.7**: a Java `new` keeps the type path it writes, and a package
  or `Outer.Inner` path narrows the call to that type. `affected` on a
  one-line `ItemMetadataAssert.extracting` edit: 204 test files -> 5.

## [1.7.7] — 2026-10-06

### Fixed
- **A Java `new` keeps the type path it writes.** The extractor kept
  only the last segment, so `new org.apache.tomcat.util.descriptor.web.
  ErrorPage()` was `new ErrorPage` and landed on the repo's own
  `ErrorPage`, and `new Outer<String>.Inner()` was read as `Outer`. The
  call text is now `new` plus the whole written path, type arguments,
  type annotations and whitespace dropped, and its name is the last
  segment. External rows name the real type (`new com.sun.jna.
  LastErrorException`, `new SslContextFactory.Server`).
- **A JVM call written through a package path reaches only that
  package's type.** `new a.b.C()`, `a.b.C.m()` and Kotlin `a.b.C()`
  name a package and a top-level type: one repo symbol on that path is
  the target outright, several are picked among by the usual rules, and
  none is external. On spring-boot, `new org.springframework.
  integration.context.IntegrationProperties()` no longer reaches the
  repo's `IntegrationProperties` (4 callers to 0), nor `new java.net.
  Proxy(..)` `RemoteDevToolsProperties.Proxy`; the two
  `ControllerEndpointHandlerMapping` classes are each constructed by
  their own FQN.
- **`new Outer.Inner()` reaches only a type nested that way.** `new
  DataRedisProperties.Sentinel()` landed on `DataRedisConnectionDetails.
  Sentinel`, and Jetty's `new HttpCookieStore.Empty()` on the repo's
  `Empty`; calls that were ambiguous now resolve (`new Health.
  Builder()`, `new DockerConnectionConfiguration.Host(..)`, `new Info.
  Builder()`).
- **`sanity` explains the lines this stops matching.** A grep hit that
  names the target through another package or outer-type path reads
  `written through another package's or type's qualified name, so it
  names a different type — not a miss` instead of "qualified call —
  known resolver blind spot".

On spring-boot against 1.7.6, 151 call edges are gained and 11 go, and
ambiguous rows drop from 62,158 to 62,045; a design simulation puts
tensorflow at about +107 −1 (`new Interpreter.Options()`). Not judged:
Kotlin `Outer.Inner()` (no `new` to tell a construction from a call), a
one-segment lowercase head that isn't a known package root, and a
single-letter type segment, which reads as a constant. The extraction
cache refreshes on upgrade, so the first `dekko map` after it is a full
one.

## [1.7.6] — 2026-10-06

### Fixed
- **A Java call no longer lands on a method its argument count can't
  call.** Java has no default arguments, so a method takes exactly its
  parameter count, or at least its fixed ones with varargs. The
  same-file and container picks went by name and file alone:
  `this.deferredLog.debug(msg, t)` landed on a test's own `debug()`,
  `this.repositories.get(0)` on another test's `get()`, `new
  StringBuilder().reverse()` on a same-file `reverse(String)`. Such a
  pick is dropped and the ladder runs again, so `debug(msg, t)` now
  reaches `DeferredLog.debug(Object, Throwable)`.
- **One class's overloads are picked by count.** A call whose
  candidates are all overloads of one class was ambiguous, because the
  sole-candidate pick needs exactly one: spring-boot's 1,594
  `assertThat(ctx).hasSingleBean(X.class)` calls went nowhere, and
  `ApplicationContextAssert.hasSingleBean` now has 1,186 callers. When
  exactly one overload fits the count it is the target; when none does,
  the call is external. A receiver call by a JDK core-type method name
  (`list.add(x)`, `buffer.limit()`) is left ambiguous: one class owning
  every in-repo `add` is no reason `list.add` means it.
- **A Java call no candidate can answer is external, not ambiguous.**
  When every candidate is a Java method and none is both reachable
  from the call and callable with its count, the call goes external:
  `context.getBeansOfType(X.class)` (131 rows to 1), Mockito's `given`,
  `System.setProperty`.
- **A Java ambiguous row lists only what the call could mean.** It
  drops the candidates the call can't reach or call with its count,
  when two or more are left. On spring-boot, ambiguous rows go from
  68,764 to 62,158 and the candidates they list from 2.87 million to
  1.22 million.

On spring-boot against 1.7.5, 2,549 call edges are gained and 719 go;
a design simulation puts tensorflow's small Java tree at about +100
−23. Not judged:
argument types (`s(int)` and `s(String)` stay ambiguous for `s(1)`) and
Kotlin calls, whose default and named arguments `dekko` doesn't record.

## [1.7.5] — 2026-10-06

### Fixed
- **A Java or Kotlin call no longer lands on a method its file can't
  reach.** A `private` method is reachable from its own file only, a
  package-private Java one from its own package only, and a member's
  reach is the narrowest of its own and every enclosing type's, so a
  public method of a `private static class` or a package-private class
  counts too. The name-only picks ignored all of it: AssertJ's
  `assertThat(x).extracting("a")` landed on spring-boot's one in-repo
  `extracting`, a private test helper, from 494 callers (now its own
  file's 12), and `map.keySet()`, Gradle's `getProject()` and
  `Thread.sleep(..)` found same-named private methods across the repo.
  Such a pick is dropped and the ladder runs again, for calls and for
  method references (`X.class::isInstance`) alike. On spring-boot,
  1,541 call edges and 148 reference edges go, and every one read was
  an external API's method; tensorflow loses about 18. Not judged:
  `protected`
  (legal from a subclass), Kotlin `internal`, and the visibility of
  types themselves.
- **`new C(..)` never picks a private constructor from another file.**
  An unreachable overload is no candidate, so a construction it tied
  with now picks the reachable one: `new ApplicationContextRunner(
  supplier)` reaches `ApplicationContextRunner(Supplier)` instead of
  an ambiguous row. On spring-boot, 96 constructor edges are gained
  and 3 go.
- **`sanity` explains the lines this stops matching.** A grep hit
  outside a private or package-private target's reach reads `the
  target is private to its file or package (or sits in a private
  type), so this line names some other type's same-named method — not
  a miss` instead of "qualified call — known resolver blind spot" or
  "unexplained".

### Changed
- map.json symbol rows carry `"visibility": "private"` or
  `"package"` on JVM methods and constructors with that reach (absent
  otherwise). The extraction cache refreshes on upgrade, so the first
  `dekko map` after it is a full one.

## [1.7.4] — 2026-10-06

### Fixed
- **A Rust argument behind `#[cfg(..)]` leaves the argument count
  unknown.** `AnyEntity::new(a, b, #[cfg(..)] c, d, e)` was counted as
  five against four parameters, so the arity check ruled out
  `AnyEntity.new` and a retry handed the call to `Client.new`. The
  count now depends on the build, so only the call's shape is checked.
  `crashes::init(..)` and `pty_options(..)` calls with a gated argument
  reach their in-repo targets instead of going external.
- **A Rust `Type::name(..)` call no longer takes another type's member
  by name alone.** With no evidence beyond the name, the same-file,
  sole-candidate and last-resort picks sent `String::from("..")` to
  the calling file's `impl From<anyhow::Error> for ThreadError`,
  `Vec::from(..)` to `CursorShape.from`, and a generic
  `T::enabled_for_staff()` to a test's `DemoFlag`. Such a pick now
  needs its owner to be the written type (read through an alias or a
  renaming `use`, so `TextBuffer::new` still reaches `Buffer.new`) or
  a trait; otherwise the call is external. On zed, 17 wrong edges go.
- **A Rust path whose own type's member can't take the call has no
  second guess.** Rust prefers an inherent member to a trait's, so
  when the arity check rules out the type's own member, the call is
  external rather than retried onto some trait's same-named method. A
  ruled-out pick of another type's member is still retried, which is
  how `lsp::LanguageServerId::from_proto(id)` reaches its own
  `from_proto`.

## [1.7.3] — 2026-10-06

### Fixed
- **A C/C++ `#include` names a file by path, not by stem.** The
  include rung matched any candidate whose file shared the included
  header's stem, so `#include "tensorflow/core/platform/status.h"`
  sent `s.ok()` to whichever `status.*` file in the repo defined `ok`
  (the experimental C API's `Status.ok`, about 1,500 sites on
  tensorflow). A path include now names a file whose path ends with
  it, or its same-stem pair (`foo.h` with `foo.cc`); a bare
  `#include "util.h"` names a sibling of the including file, or a
  file anywhere when no other directory has a C/C++ file of that
  stem. On tensorflow this trades about 10,300 stem-only pairs for
  about 10,250 the stem test had left ambiguous between two or more
  same-stem files.
- **A C/C++ include binds no name.** The header's stem was entered as
  a local binding, so a variable named like a header (`map.begin()`
  with `#include <map>`) went external as "the import `map`", and
  `status.message()` was hinted to any `status.*` file. Calls in
  C/C++ files now take their import evidence from the include rung
  alone.
- **C++ standard-library member names are noise from a C/C++ call.**
  `vec.size()`, `it.begin()`, `opt.has_value()`, `scope.status()` and
  the other `std::` container, `optional`, `pair` and `absl::Status`
  members took whichever in-repo method shared the name
  (`AttrSlice.size`, `Input.status`). With no structural or include
  evidence such a call is now external. The list applies only to
  calls in C/C++ files; `data()`, `get()`, `DebugString()` and the
  like stay off it, being real in-repo API. `unused` reads the same
  list, so a C++ `size` method those calls might reach is still a
  dispatch candidate.

## [1.7.2] — 2026-10-06

### Fixed
- **A bare Python call can't reach a method that takes `self` or
  `cls`.** A one-argument bare call "fit" `Registry.list(self)`
  because `self` is only stripped from the count for a receiver call,
  so every `list(x)` on tensorflow landed on it (and `enumerate(..)` on
  `DatasetV2.enumerate`): about 2,800 sites. The pick is dropped and
  the ladder runs again. A function nested in a method, which is
  extracted as a method with no `self`, is still reached.
- **A Python receiver call reaches a top-level function only through
  an import.** `constant_op.constant(..)` took the calling file's own
  `constant` at the same-file rung, and `layer.count_params()` on a
  local took a top-level `count_params`. Now `x.f()` can mean a plain
  function only when `x` is an import binding of the file (a rebind
  counts) and the function isn't in the calling file, so
  `constant_op.constant` reaches `constant_op.py`.
- **A bare Python call to another file needs the name in scope.** The
  name-only rungs took `tuple(x)` to tensorflow's lone
  `control_flow_ops.py::tuple` from files that never import it, the
  same for `complex`, `exit`, `eval` and for parameters and locals. A
  Python target in another file now needs an in-repo import of the
  name or a star import of its file; otherwise the call is external.
  A pytest fixture from a `conftest.py` above the test still resolves
  without one. References already had this rule. JS/TS calls keep
  their picks.

## [1.7.1] — 2026-10-05

### Fixed
- **A Python import counts as in-repo only when its first segment is
  Python the repo has.** Any segment matching any file's stem used to
  be enough, so `import numpy as np` was in-repo on tensorflow because
  of a C++ `numpy.cc`. Every `np.array(..)` then ran the ladder and
  landed on the repo's own `np_array_ops.py::array`. Now an absolute
  import has to start with a Python module or package of the repo.
  Relative imports are unchanged.
- **A Python import of a module the repo doesn't have binds nothing.**
  `from tensorflow.python.ops import gen_nn_ops` names a module that's
  generated at build time, so `gen_nn_ops.conv2d(..)` landed on the
  hand-written `nn_ops.py::conv2d` wrapper. An import whose module
  isn't in the repo, under a package that neither defines nor imports
  the name, now goes external, for `gen_x.f(..)` and for a bare `f(..)`
  imported from `gen_x` alike. A name an `__init__.py` re-exports
  still resolves, and so does a name a plain module assigns as a
  variable. TensorFlow's generated public API (`import
  tensorflow.compat.v1 as tf`) is external the same way, so
  `tf.Variable(..)` no longer lands on `variables.py` by name. On
  tensorflow, with the change above: 4,480 edge pairs gone, 575 gained
  from the rebinds below.
- **A Python star import and a module-level rebind of an import are
  recorded as imports.** `from m import *` is kept with the name `*`.
  `floatx = backend_config.floatx` binds `floatx` to the import it
  came from, so `floatx()` resolves on that evidence and not by luck.
  Caches re-extract Python files once.

## [1.7.0] — 2026-10-05

Closes round 1.7's fix cycle. The code is 1.6.22; this release is the
version line catching up with the round, per `CONTRIBUTING.md`'s
"Testing rounds and the version line". Round 1.7 evaluated 1.5.13 on
seven real repositories and found one High issue and eight Medium
ones, mostly calls landing on the wrong same-named symbol across
JS/TS, Rust and Java, plus cold-diff and warm-call cost. All 22 fixes:

- **1.6.1**: a construction is checked against the class's
  constructors, a bare npm package import is external, and an
  `extends` clause edit re-resolves its constructions.
- **1.6.2**: a JS/TS import resolves to its file, and that file
  answers before a name guess (cline +1,333 right pairs).
- **1.6.3**: JS/TS imports are followed through barrel re-exports
  (claude-code: 1,122 wrong `Text`/`Box` pairs fixed), and `deps`
  shows what a barrel re-exports.
- **1.6.4**: a Rust call can't resolve to a function its shape or
  argument count rules out.
- **1.6.5**: a Rust `use` of a crate the repo doesn't have is
  external.
- **1.6.6**: a Rust path is read through its alias, renaming `use`,
  turbofish or crate, and an ambiguous row lists only what the call
  could mean.
- **1.6.7**: a Java constructor overload a literal argument can't be
  passed to is ruled out, and varargs parameters have a type.
- **1.6.8**: every symbol id a command prints works as a target.
- **1.6.9**: C++ classes that share a qualified name pick the
  constructor in their own file.
- **1.6.10**: `sanity` explains a Java/Kotlin line that only names a
  type (spring-boot unexplained rows 18,349 -> 87).
- **1.6.11**: `query callers` lists value references, and `sanity
  <target>` and `sanity --all` agree on every row's cause.
- **1.6.12**: the `prompt-submit` hook pins the file that defines a
  code-shaped name in the prompt.
- **1.6.13**: a cold `diff`/`affected`/`workset` reuses the map's
  cached resolution for the old side (tensorflow cold diff 239 s ->
  69 s).
- **1.6.14**: a daemon-routed command waits while the daemon is busy.
- **1.6.15**: `sanity --all` walks the repo once.
- **1.6.16**: a warm call trusts a recorded file stat instead of
  re-reading every source file (spring-boot 0.87 s -> 0.42 s).
- **1.6.17**: test-excluding reads are memoized, and a note added
  through `add_note` shows in the same session.
- **1.6.18**: a stale read parses `map.json` once, not three times.
- **1.6.19**: an MCP tool call with an argument the tool doesn't take
  is an error.
- **1.6.20**: an explicit rev is checked before the map loads, and an
  option-shaped rev is rejected.
- **1.6.21**: a C++ call keeps its `ns::` when a misparse cuts it off.
- **1.6.22**: a Java/Kotlin import of a nested type or static member
  reaches the file that declares it.

## [1.6.22] — 2026-10-05

### Fixed
- **A Java import of a nested type or a static member reaches the
  file that declares it.** `import a.b.Outer.Inner;` and `import
  static a.b.Outer.helper;` were looked up as `a/b/Outer/Inner.java`
  and `a/b/Outer/helper.java`, which don't exist, so they were listed
  as external and the file edge to `Outer.java` was lost. On
  spring-boot that was about 1,500 imports, and 169 files had no
  in-repo import edge at all, which hid them from `deps` and from
  `affected`'s import tier. Both now resolve to the enclosing type's
  file, at any depth. Kotlin already went one level up and now goes
  as deep as Java. The walk stops at a package (a lower-case
  segment), so `x.y.z.Thing` never lands on an unrelated `x/y.java`.

## [1.6.21] — 2026-10-05

### Fixed
- **A C++ call keeps its `ns::` when a macro breaks the method around
  it.** With a macro before a method's return type (`static
  EIGEN_ALWAYS_INLINE absl::Status Compute(..)`), tree-sitter can read
  the body as a struct's field list, and `return absl::OkStatus();`
  comes out as a bitfield named `absl` whose width is a bare
  `OkStatus()`. With no scope written, the call resolved to the one
  in-repo `tensorflow::OkStatus`. The extractor now puts the cut-off
  scope back for exactly that shape, so the call reads
  `absl::OkStatus` and goes external (3 sites on tensorflow, including
  the two false `OkStatus` edges reported). Caches re-extract C++ files
  once.

## [1.6.20] — 2026-10-05

### Fixed
- **`workset` checks an explicit rev before regenerating a stale
  map.** It loaded (and on a stale map, regenerated) the map first and
  only looked at the rev afterwards, so `workset <a path>` on zed took
  15.8 s and 2.6 GB to say "cannot export git rev". `diff`, `affected`
  and `workset` now resolve an explicit rev up front and fail in a
  `git rev-parse`: `dekko: unknown git rev 'X'`, plus `'X' is a path
  in this repo, not a rev` when it is one, and for `workset` a pointer
  to `--symbol`. Outside a git repo the message says so.
- **An option-shaped rev no longer resolves to a pile of refs.** Rev
  lookups ran `git rev-parse <rev>`, and `git rev-parse --all` exits 0
  printing every ref, which then passed for a SHA. A rev starting with
  `-` (only reachable through MCP's `rev`; the CLI's parser stops it)
  is now unknown, and lookups use `--verify`, which demands exactly
  one object.

## [1.6.19] — 2026-10-05

### Fixed
- **An MCP tool call with an argument the tool doesn't take is an
  error, not a silent answer to a different question.** Handlers read
  only the keys they know, so `impacted_tests {"files": [..]}` dropped
  `files` and reported "no impacted tests" against the default rev,
  and `get_callers {"symbol": .., "bogus": 1}` just succeeded. Now the
  reply is `isError`, names every unknown argument, lists the ones the
  tool advertises, and adds `did you mean 'symbol'?` for a near miss
  like `symbl`. A `null` value is ignored, as it already is for target
  aliases. Knobs some tools honor without listing them (`limit` on
  `get_callers`, `include_tests` on `get_callees`, ...) stay accepted.
  `workset` now takes `name`, `target` or `type` for its `symbol`,
  like every other target tool.

## [1.6.18] — 2026-10-05

### Performance
- **A read on a stale map parses `map.json` once, not three times.**
  Every read other than `diff`/`affected` rewrites a stale map before
  answering, and on the way it parsed the old `map.json` in full just
  to learn it was stale, then again to read four regen options, then
  once more for the map it had just written: 12.8 s of a 52 s
  tensorflow read after a one-line edit, 4.1 of 18 s on spring-boot.
  The first two now read the few-KB provenance sidecar, the way
  `diff` and `affected` already judge a map. A missing or desynced
  sidecar, or a map stale by version (whose format check must still
  run), takes the old path. An outdated long-lived process whose
  delegated regen fails still serves the map it has, parsing it only
  on that branch.

## [1.6.17] — 2026-10-05

### Performance
- **A warm `get_callers` (and every other test-excluding call) no
  longer rebuilds the test-free index each time.** The MCP server and
  the daemon kept the loaded map between calls, but the test-free view
  most tools use by default was rebuilt from it on every call: about
  2 s of a 2.8 s tensorflow `get_callers`, 0.5 s on spring-boot. The
  view is now built once per loaded map and reused; a reloaded map
  starts a new one.

### Fixed
- **A note added through `add_note` shows on that session's later
  calls.** Notes were read when the map loaded, so a long-running MCP
  server or daemon kept serving the notes it started with until the
  map itself reloaded. Each warm call now checks `notes.json`'s
  `(mtime, size)` (one `stat`) and rereads the notes when it moved.

## [1.6.16] — 2026-10-05

### Performance
- **A warm call no longer re-opens every source file to check the
  map is fresh.** Every daemon or MCP call starts with a freshness
  check, and that check ran full file discovery, which opened each
  supported file to sample it for minified content. On spring-boot
  that was 9,182 file opens per call, about 0.7 s of a 0.7 s
  `query_symbol`. A file whose `(mtime, size)` still matches what the
  map recorded is now admitted on that signature, the same signature
  the check already trusted to skip re-hashing, and its `stat` is read
  once instead of twice. New files, touched files and files the map
  skipped take the full checks as before, and path rules
  (`.dekkoignore`, `--exclude`) still apply to every file. On
  spring-boot the content check went from 0.87 s to 0.42 s with the
  same verdict. `dekko map`'s own discovery is unchanged.

## [1.6.15] — 2026-10-05

### Performance
- **`sanity --all` walks the repo once instead of once per name.** It
  ran a full `grep -rn` for every unique bare name, 2,000 by default,
  so a sweep cost 2,000 walks: 879 s on spring-boot, and on zed it
  never finished because an untracked 26 MB one-line cache file was
  read and returned by nearly every grep. Now one `grep -rlI` lists
  the text files in grep's walk order, one read of each file finds
  which names it can hold, and each name's `grep -w -F` runs over only
  those files, in that order. grep still decides every row, so the
  rows, their order, the 5,000-line cap and `sanity <target>`'s output
  are unchanged. A file with a line over 10,000 characters is matched
  in-process once for all names, keeping its short rows and counting
  the long ones as before. The prototype measured 10 s on spring-boot,
  23 s on zed and 15 s on tensorflow for 2,000 names. If the one walk
  fails, `--all` exits 2 as a failed per-name grep did.

## [1.6.14] — 2026-10-05

### Fixed
- **A daemon-routed command waits for as long as the daemon is
  working on it.** The client gave up after a wait estimated from the
  repo's size (at most 300 s), so a request that outran the estimate
  failed with exit 7 and no output while the daemon kept working: a
  cold `diff` on tensorflow whose new side also had to resolve the
  whole repo hit 293 s and lost a result the daemon finished about a
  minute later. The estimate is now only the first wait. Past it, the
  client asks the daemon's status listener whether it is still busy
  and, while it is, keeps waiting, printing `note: the daemon is still
  working on this (N s so far)` the first time and once a minute after.
  It gives up when the daemon says it is no longer busy, two status
  probes in a row go unanswered, or one request passes 30 minutes, and
  the exit-7 message now says which. The probe asks for a brief status
  (`running`, `pid`, `busy`) that skips the cache report's freshness
  check; an older daemon sends its full status, which has `busy` too.

## [1.6.13] — 2026-10-05

### Fixed
- **A cold `diff`, `affected` or `workset` no longer resolves the old
  commit from scratch.** With no rev-cache entry, the old side exported
  the rev and re-resolved every call in the repo, while the working
  tree's `.dekko/` already held the resolution for a tree that was the
  same commit or a few files off it. On tensorflow that was 186 s of a
  239 s first `diff` on a dirty tree, and through the daemon it ran
  past the client's 292 s wait, so the call failed with no output. The
  old side now goes through the same reuse gate an incremental `map`
  uses and re-resolves only what the difference can reach: 239 s to
  69 s on tensorflow, 30 s to 13 s on zed. The gate still refuses a rev
  that adds, removes or renames a file, changes a type, or has a
  different `Cargo.toml`, tsconfig or workspace config, and those
  resolve in full as before, with the same `no rev-cache` note. When
  reuse engages the note no longer prints, since the wait is short.
  The old side's symbols, callers, body hashes and imports are
  identical either way.

## [1.6.12] — 2026-10-04

### Fixed
- **The `prompt-submit` hook lists the file that defines a symbol the
  prompt names.** A file was scored against its path, its doc line and
  only its first 8 symbol names, so a function defined further down
  was invisible: `who calls generateBones?` on a repo whose
  `engine.ts` opens with constants listed three files that merely
  mention "bones". Now a code-shaped identifier in the prompt
  (camelCase, an inner underscore, `Foo.bar`/`Foo::bar`/`Foo#bar`,
  `name(`, or anything in backticks) that exactly names a symbol puts
  the defining file first, marked `(defines <name>)`, production files
  before tests; word-relevant files fill the rest of the list. A plain
  word never pins, a dotted filename like `server.ts` doesn't either,
  and a name defined in more files than fit gets a `... N more define
  <name>` line pointing at `dekko query symbol`. Asking `who calls
  <name>?` for sampled names across seven real repos listed a defining
  file 33-70% of the time before (33% on zed, 39% on tensorflow) and
  100% after.

## [1.6.11] — 2026-10-03

### Fixed
- **`query callers` lists value references next to callers, and
  `sanity` points there.** The `referenced (not called):` section only
  printed when a symbol had no callers at all, so a function with one
  caller and one `setTimeout(fn)` reference had no command that showed
  the reference; `sanity`'s value-reference cause pointed at `query
  uses`, which refuses in-repo names. Now the section follows the
  caller rows whenever the map holds references (JSON
  `referenced_not_called` likewise, with its own `referenced_meta`),
  and it obeys `--limit`/`--budget`, which it used to ignore on a
  references-only answer. The `sanity` cause and the `uses` refusal
  name `query callers`. A function's name mentioned without a call
  inside its own body (`setTimeout(doRefresh, ..)` in `doRefresh`, a
  same-named field in a Rust getter) reads `mention inside the
  symbol's own body, not a call — never a missed caller` instead of
  "unexplained" or "generic name".
- **`sanity <target>` and `sanity --all` give a row the same cause.**
  The two modes ran one classifier with four different inputs, so the
  same line read differently: single-target called 416 rows on
  claude-code and cline "likely an unrelated external-library method"
  and was wrong on 415 (`entries.length`, a same-named local, a test
  file's own helper), and `--all` blamed a call in the target's own
  file on a "cross-file collision". Now both modes classify each grep
  hit from facts about the bare name only, and run the
  target-dependent rungs (the sibling-file collision, the receiver
  check) per target afterwards. The receiver label needs a call of the
  name on the line, a hit in the target's own language where imports
  name types (Java/Kotlin/Scala, JS/TS, Python, C#; not C/C++, where
  it called real `shape->AddDim(..)` misses library methods), and only
  re-decides rows that were unexplained or "generic name". Single-target vs `--all` disagreement goes to 0 on
  every repo measured (claude-code 197, cline 220, tensorflow 1,388,
  zed 5,705 before). Every disagreeing row lands on `--all`'s cause,
  except 43 tensorflow and 203 zed calls in the target's own file that
  `--all` called a "cross-file collision". Unexplained rows go down
  on every repo (spring-boot 87 to 79, tensorflow 10,109 to 10,060,
  zed 11,889 to 11,812), as self-mentions are named.

## [1.6.10] — 2026-10-03

### Fixed
- **`sanity` explains a Java or Kotlin line that names a type without
  constructing it.** Java and Kotlin had no type-position rule, so
  `ConfigurationPropertyName oldName = property.getName();` fell
  through every rung and read "unexplained" on the class and on each
  of its constructors. A class with an explicit constructor was also a
  mixed `{class, method}` name, which switched off the type rules that
  did exist. Now a class and its own constructors count as one type, in
  single-target and `--all` mode alike, and a Java/Kotlin line that
  names that type reads `names the type without constructing it
  (declaration, parameter or return type, generic argument, static
  member access, cast or class literal) — not a call site`. It never
  fires while any occurrence of the name could construct it (`Name(`,
  `Name<..>(`, `Name::new`; in Kotlin also `::Name` and `Name {`), so a
  missed construction stays a miss. A name only inside a Java or
  Kotlin string literal reads as a string mention (a Kotlin `${...}`
  template is code and never does). On a constructor target, a
  construction the map gave to a sibling overload reads `resolved to
  another constructor of the same class`, and one it gave only to the
  class because the arguments fit 2+ overloads reads as that tie,
  instead of blaming "a different, same-named declaration". On
  spring-boot (`sanity --all`, default 2,000 names) unexplained rows
  go from 18,349 to 87 and flagged targets from 2,267 to 59; the
  rows left are `Name::new` references dekko records no edge for, a
  Java record constructed from another file, and text in JS and SQL
  resources. `sanity ConfigurationPropertyName.ConfigurationPropertyName`:
  311 unexplained to 0. No call graph change.

## [1.6.9] — 2026-10-02

### Fixed
- **A constructor whose callers tied between two overloads no longer
  reads "no callers".** When a construction's arguments fit two
  overloads equally, the class gets the edge and no constructor does.
  `query callers` on either constructor printed "(no callers of ..)",
  the same line dead code gets, plus "N additional call site(s)
  resolved ambiguously", which never said the class holds the
  answer. `Graph::Graph` on tensorflow read zero while the class has
  399 callers. Now it prints `(no caller resolved to this constructor
  of Graph: <id>)` and a note: how many callers construct the class
  with arguments that fit this constructor and another, which other
  overloads, and `dekko query callers <class id>` with the class's
  caller count. A call whose name could also mean an unrelated symbol
  is not a tie and keeps its own count and the old "resolved
  ambiguously" note. `--json` adds `overload_ties` (`callers`,
  `class`, `class_callers`, `siblings`); `ambiguous_in` is still the
  total. The `query symbol` fan-in line, `context` and MCP
  `get_callers` split them the same way. These counts were always
  distinct callers, not call sites, so the notes now say "caller(s)".
  Applies to 197 constructors on spring-boot (152 that read zero on
  ties alone) and 486 on tensorflow (365; 347 C++, 18 Java). Where
  several C++ classes share a qualname (a `DummyDevice` in each of a
  dozen test files), the note names the class in the constructor's
  own file, or the header beside it. No call graph changes.

## [1.6.8] — 2026-10-02

### Fixed
- **Every symbol id a command prints now works as a target, and a
  class can be named apart from its constructors.** Ids were only
  reachable by reading `::` as a separator, so an overload's `#N` id,
  or any id whose plain reading was ambiguous, said "no symbol
  matches": 272 of 3,000 sampled ids on spring-boot, 630 on
  tensorflow, 49 on claude-code, 35 on cline. tensorflow's 3,575
  symbols whose qualname holds `::` (`tensorflow.ClientSession::
  Impl.Impl`) couldn't be named by any target string. Now an id is
  looked up first and always names its own symbol; 0 failures across
  every id on all seven eval repos. In `file:name`, a symbol whose
  qualname is exactly `name` now wins over ones that only share the
  bare name, so `ErrorPage.java:ErrorPage` is the class instead of
  "ambiguous" between the class and its three constructors (2,218 of
  2,219 such types on spring-boot, 245 of 676 on tensorflow; the rest
  are true same-qualname duplicates). That includes Kotlin classes
  with a primary constructor, which share the class's line, so even
  `:LINE` couldn't pick them. `sanity --all` dropped those classes
  from its sweep as ambiguous. A bare name shared only by one class
  and that class's own constructors now resolves to the class, with a
  note on stderr saying so and how to name one constructor: the
  class's callers are every construction (2,950 names on spring-boot,
  4,683 on tensorflow stop exiting 4). A class name that also names a
  second class or an unrelated method stays ambiguous. `sanity`,
  `sanity --all` and `unused`'s `sanity --unused` hint now pass the
  symbol's id rather than a rebuilt `path:qualname:LINE` string, and
  an ambiguous list whose candidates share file, name and line names
  their ids, since `:LINE` can't help there. Targets that resolved
  before resolve to the same symbol; no call graph changes.

## [1.6.7] — 2026-10-02

### Fixed
- **A Java constructor overload a literal argument can't be passed to
  is no longer picked.** Where a varargs overload also fit, the
  argument count decided by "declares exactly this many", which says
  nothing there: `new SpringApplication(A.class, B.class)` went to
  `(ResourceLoader, Class<?>...)`, `new TestRestTemplate("user",
  "password")` to `(RestTemplateBuilder, UriTemplateHandler)`, `new
  ServletRegistrationBean<>(servlet, false)` to `(servlet,
  String...)`. Now each construction records what its arguments
  visibly are (a string, a class literal, a boolean, a plain int, a
  char, `null`, a `new T(..)`, a lambda or method reference), and an
  overload one of them can't be passed to is dropped before the count
  decides. Java has no user-defined conversions, so a class literal is
  never a `ResourceLoader`. When every overload is ruled out, the
  count's choice stands. Measured against 1.6.6 on spring-boot: 318
  caller/callee pairs gained and 27 removed. All 27 were read and all
  were wrong. 26 of those sites move to the right overload, and one
  (`new TestConfigurations(Sorter.instance, A.class, B.class)`, two
  varargs overloads left) becomes ambiguous. 305 sites that were
  ambiguous now resolve (`FilteredClassLoader` alone 197), including
  `new ErrorPage(Oops.class, "/500")`. Ambiguous rows fall from
  69,142 to 68,851, and 8 more list fewer overloads. `dekko unused`
  loses 7 constructors that gained real callers. The other six eval
  repos keep the same edges. Only Java constructions are read;
  overloaded Java methods and other languages' constructors are
  unchanged, and an argument that isn't a literal (`HttpStatus.
  NOT_FOUND`, a local) tells nothing, so `new ErrorPage(HttpStatus.
  NOT_FOUND, "/404")` stays ambiguous.
- **A Java varargs parameter now has a type.** `Class<?>... sources`
  was one parameter named `Class<?>... sources` with no type. It is
  now `sources` of type `Class<?>...`. Signatures read `sources:
  Class<?>...` like every other Java parameter, and `find_type_usages`
  sees the type.

## [1.6.6] — 2026-10-02

### Fixed
- **A Rust path through a type alias, a renaming `use`, a turbofish or
  a workspace crate now reaches what it names.** Four shapes hid the
  owner of a Rust call, so the ladder ran over every same-named
  function in the repo. `HashMap::default()` through `collections`'
  `type HashMap<K, V> = FxHashMap<K, V>` landed on whatever `default`
  sat in the caller's file. `TextBuffer::new(..)` through `use
  text::Buffer as TextBuffer` (here or re-exported from another crate)
  never found `Buffer.new`. `Vec::<T>::new()` read `<T>` as the type
  and gave up. And `release_channel::init(cx)` from `main.rs` went to
  `main.rs`'s own `init`, or stayed ambiguous among all 165 `init`
  functions: nothing narrowed a lowercase path to the crate it names.
  Now an alias is followed (a Rust `type` alias records what it names
  in its symbol's `returns`) and a rename read back to the original,
  both landing on that type's members or going external when the repo
  doesn't define it; a turbofish is skipped; and a `some_crate::..::
  name(..)` path, written directly or through a `use` of the crate,
  keeps the candidates in that crate's directories (from its
  `Cargo.toml`), functions first, when it has any. The same crate rule
  applies to `impl some_crate::Trait for X`.
- **A Rust ambiguous row lists only what the call could mean.** A
  dot-call row listed every same-named symbol, free functions and
  associated functions included; it now lists the methods taking
  `self` and the written count, when two or more do. A path on a type
  with no symbol but with members (`String::from` with the repo's
  `impl From<X> for String`) lists those members. Measured against
  1.6.5 on zed: 1,046 caller/callee pairs gained and 167 removed. All
  167 removed were read against the source and all were wrong (46
  `HttpRequest::builder()` calls through `use http_client::Request as
  HttpRequest` on `extension_api`'s `HttpRequest`, about 50
  `HashMap`/`HashSet::default()` calls on a local type's `default`,
  27 `agent_ui::test_support::init_test(cx)` calls on the caller's own
  `init_test`, cross-crate `init`s). 120 of the gained were read, all
  right: `release_channel::init` now has 55 callers (0 before) and
  `text::Buffer.new` 73 (54). Ambiguous rows fall from 87,252 to
  85,757 and the candidates they list from 2,560,667 to 1,425,651
  (median 8 to 4); `new` rows over 1,000 candidates from 225 to 18.
  1,039 calls go external. 5 `impl project::ProjectItem for X` clauses
  move from `workspace`'s `ProjectItem` to `project`'s. `dekko unused`
  on zed gains 3 rows (each kept alive only by a wrong edge) and loses
  2. The other six eval repos are unchanged. Re-pointing a renaming
  `use` (same name, another original) now invalidates the files that
  write that name, and the resolve cache key reads each crate's
  directories as well as its name.

## [1.6.5] — 2026-10-02

### Fixed
- **A Rust `use` of a crate the repo doesn't have no longer counts as
  in-repo because a file shares a segment's name.** Only `std`, `core`
  and `alloc` were recognized as outside crates. Every other `use`
  fell to the file-name test, and zed has `windows.rs`, `image.rs`,
  `http.rs` and `process.rs`, so `windows::core::HSTRING`,
  `image::Frame` and `smol::process::Command` all looked in-repo and
  their calls ran the ladder over every same-named function in the
  repo. Now a `use` whose first segment names no crate in the repo's
  `Cargo.toml` files (package and `[lib]` names, directory names and
  `path =` dependencies, `-` read as `_`) and no module of a repo crate
  is external. A repo with no `Cargo.toml` keeps the old test. Editing
  a `Cargo.toml` now invalidates the resolve cache.
- **A type imported from a workspace crate that only re-exports it is
  treated as an outside type.** `use collections::BTreeMap;` (where
  `collections` is `pub use std::collections::*`) was kept in-repo so
  that a rename like `pub use text::Buffer as TextBuffer` could still
  resolve, and so `BTreeMap::new()` ran the ladder too. Now an in-repo
  `use` of a name no repo symbol carries counts as a rename only when
  some `use .. as` in the repo binds that name; otherwise the call is
  external. Measured against 1.6.4 on zed: 141 caller/callee pairs
  removed, all read against the source. 128 were wrong (65
  `acp::SessionId::new` calls on `scheduler`'s `SessionId`, 22
  macro-made `RoomId::from_proto` calls on another type's
  `from_proto`, `oneshot::channel()` on a repo `channel` method, and
  so on). 13 were right: a local variable or parameter named like an
  imported outside module (`let fs = FakeFs::new(..)` after `use
  smol::fs`, a `stream` parameter after `use futures::stream`) is read
  as the import, so its calls now count external. No pair is gained.
  928 calls go external, ambiguous rows fall from 87,934 to 87,252,
  and `new` rows over 1,000 candidates from 324 to 225. `dekko unused`
  on zed gains 2 rows (one through the shadowing gap above). The other
  six eval repos are unchanged.

## [1.6.4] — 2026-10-02

### Fixed
- **A Rust call no longer resolves to a function its shape or argument
  count rules out.** Rust has no overloads, default arguments or
  optional parameters, so a mismatched count is code that doesn't
  compile. But only the sole-candidate rung read the count: the
  same-file, typed-parameter, container and import rungs took whatever
  matched the name. On zed, 6,039 call sites picked such a target:
  every zero-argument `x.clone()` in the editor crate landed on
  `Editor.clone(&self, window, cx)`, 1,002 `cx.simulate_keystrokes(..)`
  calls landed on `TestAppContext`'s two-argument version, `a.min(b)` on an
  associated `fn min(a, b)`, `drop(x)` on some type's `Drop::drop`, and
  `zlog::init_test()` on the same file's `init_test(cx)`. Now a
  dot-call needs a method with `self` that takes the written count, a
  bare call can't reach a method (or anything taking `self`), and a
  path call writes every parameter, `self` included
  (`Type::method(obj, x)`). A call whose arguments couldn't be counted
  (inside a macro) is judged by shape only. When a pick is ruled out,
  the ladder runs again without it, so a wrapper's
  `self.inner.set(a)` reaches the wrapped type's one-argument `set`
  instead of the wrapper's own two-argument one. Only a candidate that
  was actually picked is dropped, and a call that was ambiguous stays
  ambiguous. Measured against 1.6.3 on zed: 3,151 wrong pairs removed
  (a random 50 read, all wrong) and 320 gained, all 320 read against the source:
  9 wrong (2.8%), mostly a std method (`Cell::update`, `SocketAddr::port`)
  or an outside crate's type. 1,272 calls go external
  and the ambiguous count grows from 85,406 to 87,934 rows, because a
  wrong pick now becomes ambiguous among the candidates that remain.
  `dekko unused` on zed gains 117 rows and loses 41. Of the new rows,
  111 are trait impl methods (flagged as dispatch candidates) and 6
  are structs built only by struct literals. Wrong edges used to hide
  them. cline has one Rust call go external. The other five eval repos
  (no Rust) are unchanged.

## [1.6.3] — 2026-10-02

### Fixed
- **A JS/TS import that goes through a barrel file is followed to the
  declaration.** `export { X } from "./x"`, `export * from "./x"`,
  `export * as ns from "./x"` and a file's default export were not
  extracted at all, so a barrel (`index.ts`, a package entry file)
  was a dead end: the resolver knew the import named the barrel and
  nothing about where the name came from. Three things followed. A
  barrel that renames was bound to the wrong symbol: claude-code's
  `ink.ts` has `export { default as Text } from
  './components/design-system/ThemedText.js'`, and every `<Text>`
  imported from it was attributed to an unrelated
  `ink/components/Text.tsx`, because that file's name is the imported
  name. That was 1,122 wrong reference pairs (4,160 sites): the base
  component read as the most-used symbol in the repo and `ThemedText`
  as nearly unused. A name with a namesake elsewhere was ambiguous or
  bound to the namesake, and a renamed export with no namesake was
  external. Now the name is followed from the import through named
  and renamed re-exports, stars, namespace re-exports
  (`Llms.getProvider(..)` after `export * as Llms from`), a file's
  own `import { X }` that it exports again, source-less renames
  (`export { a as b }`) and named default exports, up to eight files
  deep, each hop resolved with the re-exporting file's own tsconfig.
  As in 1.6.2, the walk answers only when it ends at exactly one
  top-level symbol. Measured against 1.6.2: claude-code moves 1,122
  reference pairs from `Text` / `Box` to `ThemedText` / `ThemedBox`,
  gains 74 call pairs (134 sites) and loses none; cline gains 73 call
  pairs (108 sites), 5 reference pairs and 3 heritage edges, 20 calls
  leave `external`, and the 3 call pairs and 2 reference pairs it
  loses were bound to a same-named file in another app. Five
  repositories without JS/TS barrels are unchanged. Not followed: a
  default import whose local name differs from the declared one
  (`import Text from "./ThemedText"`), CommonJS `module.exports =
  require(..)`, and Python `__init__.py` re-exports.
- **`dekko deps` shows what a barrel re-exports.** Each re-export with
  a source is a module-graph edge from the barrel to that file,
  carrying the exported name (`*` for a star), and a re-exported
  package is listed under the file's external modules. cline gains
  508 edges (7,090 -> 7,598) and claude-code 65. These edges close
  real import cycles (a package's `index.ts` re-exports a file that
  imports from `index.ts`), so `deps --cycles` reports fewer, larger
  clusters: cline goes from 27 clusters over 93 files (largest 14) to
  15 clusters over 290 files (largest 138).
- **`dekko sanity` no longer calls a barrel import "a different
  declaration".** A grep hit in a file that imports the name through
  a barrel was labelled as bound to another declaration, which is
  false when the barrel re-exports the target itself. Such a row is
  now left as a miss and counts toward `--fail-on-unexplained`.
- **Editing a re-export or re-pointing an import re-resolves the
  files that use the name.** Neither is part of a symbol, so the
  incremental map did not see the edit. A star re-export gained or
  lost re-resolves the whole repo.

## [1.6.2] — 2026-10-02

### Fixed
- **A JS/TS import is resolved to its file, and that file answers
  before any filename test.** When two symbols shared a name, the
  resolver asked whether some segment of the import's specifier was a
  candidate's file stem. For `./errors` that is nearly exact. For a
  tsconfig path alias it is a coincidence test: `@/lib/utils` matches
  every `utils.ts` in the repo, so in a monorepo where three apps each
  carry their own copy of a file, `cn(..)` was ambiguous among the
  three though each app's own `tsconfig.json` says which one it means.
  For an import alias it tested the wrong name altogether: after
  `import { isInsideTmux as checkTmux }`, a call to `checkTmux()` went
  to whatever the repo happened to call `checkTmux`, on claude-code a
  method of an unrelated class. The module graph (`dekko deps`)
  already resolved the same specifiers exactly: relative paths,
  tsconfig `paths` aliases, workspace package entry files and
  root-relative paths. The call, reference and heritage passes now use
  that resolution first. When the resolved file declares exactly one
  top-level symbol under the imported name, that symbol is the answer;
  when it declares none, or a type and a value under one name, the
  rest of the ladder decides as before. Measured against 1.6.1: cline
  gains 1,333 caller/callee pairs (1,525 call sites) and 1,677
  reference pairs (2,237 sites), and its ambiguous rows fall from
  5,450 to 4,118; the 3 reference pairs it loses were an import alias
  bound to the wrong symbol. claude-code gains 8 call pairs and 4
  reference pairs and loses the one wrong `checkTmux` edge. Five
  repositories with no JS/TS aliases are unchanged. A re-export
  (`export { X } from "./x"`) is still not followed: an import that
  goes through a barrel file resolves as it did before.
- **Editing a tsconfig `paths` table or a workspace package's entry
  fields re-resolves the repo.** The incremental map now reads both
  to resolve calls, and neither edit touches a source file.

## [1.6.1] — 2026-10-02

### Fixed
- **A construction with arguments is checked against the class's
  constructor, not against the class.** `new AgentRuntime(config)`
  names the class, and a JS/TS or Python class symbol has no
  parameters: they are on `constructor` / `__init__`. When the class
  was the only candidate and no import named its file, the resolver
  read the call as one argument for zero parameters and filed it as
  external. That is every construction with an argument whose import
  goes through a barrel (`./index`, a package `__init__.py`) or a path
  alias, is written on a receiver (`linalg.LinearOperatorDiag(..)`),
  or has no import at all. A zero-argument `new Plain()` passed, which
  made it look like barrels sometimes worked. On cline `query callers
  AgentRuntime` listed 2 of the 46 construction sites, and nothing
  said 44 were missing. Now the argument count is read against the
  class's own constructors, and a count none of them takes is still
  external. A class with no constructor of its own whose argument
  count can't be known (any Python class; a JS/TS class with an
  `extends` clause) needs other evidence: the call's receiver is an
  import of the class's own file, or the call is bare and the class is
  a top-level one that the calling code could mean (a class in test
  code is not what code outside the tests constructs). A JS/TS class
  with no constructor and no `extends` clause still takes no
  arguments. Measured against 1.6.0: cline 57 construction sites
  gained (`AgentRuntime` 2 -> 46, `AgentTeamsRuntime` 26 -> 29),
  tensorflow 1,170 sites and 1,779 caller/callee pairs gained, none
  lost on any of seven repositories. Java, C++, Rust and Go maps are
  unchanged. Not a regression: 1.0.0 gives the same wrong answer.
- **An import of a bare npm package name is external, whatever the
  repo's files are called.** `import * as vscode from "vscode"`
  counted as an import of the repo on cline because a file there is
  named `vscode.ts`, so `vscode.Uri.file(..)` was matched against repo
  symbols and landed on a test stub of the VS Code API. The same test
  bound `z.unknown()` (zod) to a local `unknown` helper,
  `http.createServer(..)` to a repo method, and on claude-code `cwd()`
  from `"process"` to a repo function. A JS/TS specifier with one
  segment (`"vscode"`, `"react"`, `"node:fs"`) is now a package. A
  workspace package is still recognized first, and `@scope/name`,
  `#name` and anything with a slash are tested as before, since they
  can be path aliases. cline loses 79 wrong caller/callee pairs (118
  call sites) and 47 wrong reference pairs; claude-code loses 11
  pairs.
- **An `extends` clause added to or removed from a class re-resolves
  the files that construct it.** The incremental map compared symbols
  only, and the clause is not part of one.


## [1.6.0] — 2026-10-02

Closes round 1.6's fix cycle. The code is 1.5.13; this release is the
version line catching up with the round, per `CONTRIBUTING.md`'s
"Testing rounds and the version line". Round 1.6 evaluated 1.5.0 on
seven real repositories and found three High issues (C++ constructors
with no callers, Java constructor overloads bound to the first one
declared, Kotlin mapped with no symbols) and three Medium ones.
Fixing them surfaced seven more. All thirteen are fixed:

- **1.5.1**: a constructor call picks its overload by argument count
  instead of binding to the first one declared.
- **1.5.2**: C++ constructions count in the call graph (tensorflow
  `Graph`: 0 -> 399 callers).
- **1.5.3**: Kotlin is a Tier-1 language (spring-boot: 0 -> 1,165
  Kotlin symbols).
- **1.5.4**: ambiguous rows and Tier-2 calls stay inside the caller's
  language (tensorflow ambiguous rows 260,507 -> 238,166).
- **1.5.5**: C++ calls resolve through their full written path
  (tensorflow +3,510 edges, none lost).
- **1.5.6**: a one-scope C++ call binds to the namespace it names
  (tensorflow: 11,312 wrong call sites gone, mostly `absl::` and
  `std::`).
- **1.5.7**: C++ default arguments declared in a header count toward
  the arity of the definition in the `.cc` file.
- **1.5.8**: Rust `use std::` imports stay out of the repo, and an
  ambiguous `Type::name` row keeps only the candidates on that type
  (zed: 2,087 wrong edges gone, none added).
- **1.5.9**: a `sanity` grep-only row on a site the map resolved to a
  different same-named symbol names that symbol.
- **1.5.10**: `sanity` names the leftover non-call shapes (type
  bodies, self-recursion, imports bound elsewhere, heritage).
  `sanity --all` unexplained rows dropped 30 -> 1 (claude-buddy),
  957 -> 51 (claude-code) and 1,439 -> 218 (cline).
- **1.5.11**: a Java/Kotlin import matches by qualified path, so an
  external class no longer resolves to a same-named class in the
  repo, and any Gradle source set is a source root.
- **1.5.12**: Groovy files and Gradle build scripts are skipped with
  a stated reason instead of being mapped empty (spring-boot: 9,376
  garbage external rows gone).
- **1.5.13**: each Tier-2 language is read through its own measured
  row instead of a guess at node types: 43 languages, 39 supported
  and 4 partial against a pinned corpus. Vue, Svelte and Mojo are
  disclosed as unsupported rather than mapped wrong.

## [1.5.13] — 2026-09-30

### Changed
- **Tier-2 languages are read through one rule set per grammar, and
  the list of them is measured instead of assumed.** The generic
  extractor decided what a definition was by matching words in
  tree-sitter node type names (`function`, `method`, `class`,
  `struct`, ...) and then asking the node for a `name` field. Run over
  one real repository per grammar, that found no function at all in 21
  of the 47 Tier-2 grammars (Clojure, CMake, Common Lisp, D, Dart,
  Elixir, Elm, Julia, Nim, Nix, OCaml, Odin, Pascal, PowerShell, R,
  Racket, Scheme, Zig among them) and no call in 17, and it invented
  symbols in languages that looked fine. `dekko stats` on a 90-file
  Zig project read `0 symbols, 0 edges`, and nothing said the language
  went unread. Each grammar now has a row in `src/dekko/core/tier2.py`
  naming the node types that define a function or a type, the path to
  the name, and the node types that are calls. Nothing is inferred
  from how a node type is spelled: a grammar without a row is not a
  Tier-2 language. A row ships only if it passes a bar on its
  repository (`benchmarks/tier2_corpus.py`, 43 repositories pinned by
  commit): **supported** is 90% of functions and 80% of types found,
  with calls extracted; **partial** is 75% and 50%. 39 languages are
  supported (10 passed before, and five of those were right) and 4 are
  partial, limited by grammars that fail to parse parts of real files:
  Pascal 89%, Zsh 89%, Crystal 86%, Haxe 82%. `docs/cli.md` "Language
  support" has the list and what Tier 2 does not do.
- **Four extensions are no longer Tier-2.** `.vue`, `.svelte` and
  `.mojo` are now unparsed languages, like `.astro` and `.groovy`:
  skipped as `no parser (vue)` and counted in the coverage note every
  empty answer carries (`76 files unparsed — no parser for: vue (76)`).
  The Vue and Svelte grammars return a component's `<script>` block as
  one piece of raw text, and the Mojo grammar fails on 92% of the files
  of a real Mojo repository. All three yielded 0 symbols before, with
  no caveat. One thing is lost: a TypeScript file's import of a `.vue`
  file is no longer a `dekko deps` module edge (6 of 205 on
  vitepress). `.mli` is no longer a source file: an OCaml interface
  restates the signatures of its `.ml`, so indexing it would give every
  function a twin.
- **Symbol ids change in some Tier-2 languages.** Erlang, Haskell and
  Elixir functions written as several clauses are one symbol (`f`, not
  `f`, `f#2`, `f#3`); Solidity functions are qualified by their
  contract, Ada subprograms by their package, Fortran procedures by
  their module and Tcl procs by their `namespace eval`; SQL table names
  lose their quotes. Notes anchored to the old ids are orphaned;
  `dekko note list --orphaned` finds them.
- **`dekko[all]` needs `tree-sitter-language-pack` 1.20 or newer.** A
  row names its grammar's node types, and an older pack ships older
  grammars: the same Haxe row yields no calls on pack 1.8.
- **A `.scm` file of tree-sitter queries is not mapped as Scheme.** A
  `.scm` file with a capture (`(identifier) @name`) and no `define` is
  skipped as `tree-sitter query`. zed has 149 of them and no Scheme;
  its `stats` line loses `scheme 149f/0s` and `dekko map` prints
  `skipped: ... tree-sitter query 149`.
- One CI leg (ubuntu, Python 3.13) now installs the extras. Every
  Tier-2 test skips without the grammar pack and no leg installed it,
  which is how 21 grammars sat at zero with CI green.

### Fixed
- **Parameters, variables and table mentions were symbols.** On
  slimphp/Slim every PHP parameter was a class (`class
  $responseFactory`), 1,077 types against 748 functions; the file's
  namespace was a class too. Haxe parameters were functions (5,834 on
  HaxeFlixel/flixel, against 4,364 real ones), Gleam parameters were
  functions, Ada variables and Scala constructor parameters were
  classes, C# and Dart constructors had kind `struct` (the word is
  inside "con**struct**or"), an Erlang `mod:fun()` made `mod` a class,
  and every SQL statement that mentioned a table (`INSERT`, `ALTER`,
  `CREATE INDEX`) defined it again: zed's two SQL files held 401
  "classes", now 58 tables.
- **Functions defined by assignment or by a head word were missed or
  misnamed.** All 200 R functions in tidyverse/stringr were named
  `function`; the name is on the assignment around it. Lua's
  `M.f = function`, Nix's `f = x: ...`, Zig's `const T = struct`,
  Elixir's `def`/`defp`/`defmodule`, every Lisp's `defun`/`defn`/
  `(define (f x) ...)`, CMake's `function()` and TclOO's `method` are
  now definitions.
- **A Vim script or Fortran function owned none of its calls.** The
  symbol spanned the header line, so every call in the body had no
  caller: 0 of 4,791 attributed on tpope/vim-fugitive, 0 of 4,326 on
  json-fortran. A symbol now spans its body (97% and 96%), including
  in Dart and Zig, where the grammar puts the body next to the
  signature.
- **Definitions were recorded as calls.** Elixir's `def`, `defp` and
  `defmodule` were the top three "callees" of any Elixir repo, and a
  Julia `function helper(a)` called `helper`.
- **Member and qualified calls could never resolve.** Ada callees came
  out as `TTY.URL` and Fortran ones as `json%get`; no symbol is named
  that. They are now `URL` and `get`. A Perl `&f(...)` is a call to
  `f`. Going the other way, a shell function named `_omz::log` and a
  Scheme function named `list->set` keep their whole name at the call,
  as they do at the definition.
- **Expressions were recorded as callee names.** A callee holding
  whitespace, a bracket or a quote (`(0..<rank).map`) is dropped
  instead of becoming an `external` row nothing can match.
- **No call was extracted from any Lisp, or from Dart, Zig, Nim,
  Haskell, Elm, F#, Nix, Pascal or CMake.** Each spells a call its own
  way (an `apply` node, an argument list after a name, a bare list),
  and none matched the old call pattern. `query callers
  magit-git-string` on magit said `(no callers)` for a function 37
  others call. In a Lisp a list is a call when its head is a symbol
  that is not a special form; binding lists, parameter lists, record
  and class fields and quoted data are not calls. A curried
  application (`lib.nameValuePair name value`) is one call.
- **A Tcl command written from the root namespace was dropped.**
  `::ns::proc arg` failed a filter written for shell commands.

On the five evaluation repos with Tier-2 files every Tier-1 language,
Bash, Starlark and Ruby are identical. zed gains 21 Nix, 23 PowerShell
and 1 Julia symbols where it had none, and `dekko unused` there drops
from 10,397 rows to 10,095 (330 bogus SQL rows leave, 18 tables and 10
real functions enter). tensorflow gains its 11 CMake functions, all of
them called (82 resolved call sites). spring-boot's SQL goes from 54
symbols to 22.

## [1.5.12] — 2026-09-30

### Changed
- **Groovy files and Gradle build scripts are no longer mapped as
  empty files.** Both used to go through the generic extractor on the
  language pack's Groovy grammar, which has no declaration nodes: on
  spring-boot 760 files (736 `.gradle`, 24 `.groovy`) produced 0
  symbols, 0 edges, and 9,376 `external` rows whose "callee" was often
  a whole statement (`tasks.named("compileTestJava") { ... }`, 240
  times). `dekko stats` read `groovy 760f/0s`, `query file` said
  `mapped, no symbols`, and nothing said a class of files went unread.
  No installable Groovy grammar is good enough to fix that the way
  Kotlin was fixed: the one on PyPI is a fork of the Java grammar that
  reads a Spock feature method (`def "does a thing"() { ... }`) as a
  constructor named `def`, and fails on 93% of Spock's own files. So
  dekko now says what it does instead:
  - **`.groovy` is an unparsed language**, like `.astro`. The files
    are skipped with `no parser (groovy)` and counted in the coverage
    note that `stats`, `status`, `summary`, `search`, `affected` and
    every empty `query` answer already carry (`24 files unparsed — no
    parser for: groovy (24)`). A repo whose tests are Spock specs now
    gets that caveat on "no callers" and "no impacted tests".
  - **`.gradle` is a build script**, skipped with `build script
    (gradle)` and counted on its own line: `build scripts: 736 not
    indexed: gradle (736)` in `stats`, `status`, `summary` and the MCP
    `map_status` tool, and a `build_scripts` key in their JSON. It
    stays out of the per-query coverage note on purpose. A build
    script calls the Gradle API and the build's own logic, not the code
    most questions are about, and a caveat on every empty answer in
    every Gradle-built repo would be noise.
  - **The one place a build script does matter gets its own note.**
    Empty `query callers` on a symbol under `buildSrc/` now adds
    `build scripts: ... and this symbol is under buildSrc/; a build
    script may call it`, with the `dekko sanity` command to check
    (`build_script_warning` in JSON).
  - `.gradle.kts` is Kotlin and is indexed as before. Editing a
    `.gradle` file no longer makes the map stale.

  spring-boot: 760 fewer files, 9,376 fewer external rows, and every
  symbol, edge, ambiguous row, supertype and module edge identical.
  Repos with no Groovy or Gradle files are unchanged.

### Fixed
- **A default install told every Gradle repo to install `dekko[all]`
  for files the extra cannot map.** `dekko map` printed `NOT parsed (no
  symbols, no edges): groovy N -- grammar not installed; install the
  extras to map them`, and installing them gave 0 symbols. The files
  are now skipped with their real reason on both install types.
- **`dekko sanity` guessed at calls written in a build script.** A
  real call from `build.gradle` into build logic was labeled by the
  shape of its line: `unexplained miss` for a bare call in a closure,
  `cross-package/qualified call — known resolver blind spot` for
  `version.forAntora()`, `likely an unrelated external-library method`
  for a common name. All three describe a resolver that never saw the
  file. A hit in a build script now reads `build script: dekko does
  not index build scripts, so a call here never becomes an edge`, and
  a hit in a Groovy or Astro file reads `unparsed-language file`
  whatever the line looks like. Hits in other unrecognized files
  (`README.md`, `package.json`) are labeled as before.
- **`query file` and `outline` on a skipped path said only "no mapped
  file".** They now say why: `(a Gradle build script; dekko does not
  index build scripts)` or `(groovy; dekko has no parser for it)`.

## [1.5.11] — 2026-09-30

### Fixed
- **A Java/Kotlin import of an external class bound calls, `new`,
  `extends` and `throws` to a same-named class elsewhere in the repo.**
  `import java.util.jar.Manifest;` counted as in-repo because
  spring-boot has a `Manifest.java` of its own (the in-repo test asked
  whether any segment of the qualified name was a repo file's stem),
  so `Manifest.read(..)` edged to the buildpack's `Manifest` at 40
  sites, `jakarta.servlet.http.Cookie` to the web server's `Cookie`,
  Thymeleaf's `Context` to the jarmode `Context`, and Spring Data's
  `Repository` became the supertype of 29 classes that extend nothing
  in the repo. On spring-boot 4,564 of 23,528 "in-repo" imports named
  no repo file. A qualified JVM name is now matched as a path: the
  import is in-repo only when it names a repo file, a repo package (a
  wildcard import) or a Kotlin top-level function or property, and the
  import hint that picks among same-named candidates matches a
  Java/Kotlin file by that path alone, never by simple name. The same
  hint used to match an in-repo import of one of several namesakes
  (three shaded `JSONObject`s, two `Library`s, `TextResourceOrigin.
  Location`) to all of them and give up, so those calls went
  ambiguous; they now resolve to the file the import names. A file
  under no source root (a Bazel-style `java/org/x/Y.java`) is matched
  by path suffix of two or more segments. spring-boot: 139 wrong call
  edges gone (every one checked), 336 gained, heritage +10 / −51,
  throws resolved 204 → 219, 828 ambiguous rows gone (634 now
  external, 200 now edges); tensorflow 15 wrong edges gone (twelve
  `android.util.Size`), 90 ambiguous rows gone. Repos with no Java or
  Kotlin are unchanged.
- **Only `src/main` and `src/test` counted as JVM source roots.**
  Gradle names source sets freely, and spring-boot keeps 348 Java files
  under `src/intTest/java`, `src/dockerTest/java`,
  `src/testFixtures/java`, `src/systemTest/java`, `src/json-shade/java`
  and `src/main/javaTemplates`; imports of those files never resolved
  in `dekko deps` (210 missing module edges), and with the path match
  above they would have gone external. Any `src/<sourceSet>/java*` or
  `src/<sourceSet>/kotlin*` directory is now a source root. spring-boot
  `deps`: +213 module edges; tensorflow +2 (`src/gen/java`,
  `src/testhelper/java`).

## [1.5.10] — 2026-09-29

### Fixed
- **`dekko sanity` left whole families of non-call lines "unexplained".**
  On the TypeScript repos measured, most of what survived 1.5.9's
  ladder was a line the map rightly has no edge for and the ladder had
  no name for: a JSX attribute (`<KeyboardShortcutHint action="copy" />`,
  175 rows on one claude-code target), a continuation line of a
  multi-line template literal (prompt text, generated-code templates),
  an interface or enum member, a trailing `// comment` after code, a
  method call on an expression result (`foo().name(`), a union or
  generic type member (`Promise<Svc | undefined>`), a recursive
  self-call, a call of `appendFileSync` in a file that imports it from
  `fs`, an `extends X` clause, an abstract or overload signature, a
  destructuring member line, an alias-only `export { a as b };`, a jq
  filter in a shell script. Each now gets a cause. Four are read off
  the map: `inside an interface, type-alias or enum body — a type
  context, never a call site`, `recursive call inside the symbol's own
  body — dekko records no self edges by design`, `this file imports a
  different declaration of the name (see bound_to) — the resolver
  bound the name to that import, not the target` (with the module or
  repo path on the row: `bound_to` in JSON, `(bound to fs)` in text),
  and `heritage clause (extends/implements) the map records — not a
  call site` (a clause the map resolved to a same-named sibling reads
  as resolved elsewhere). A small three-state lexer over each JS/TS
  file gives a line its starting state, so a line inside a template
  literal or a `/* */` block opened above reads as string text or as a
  comment (a file the lexer cannot follow gets no state-based cause).
  The rest are line shapes, JS/TS unless noted: a trailing comment
  (also the `#` grammars and the C family), the missed type positions,
  chained calls in any grammar, JSX attribute and text, signatures, a
  `x.name` property access the map has no read for, declaration lines,
  multi-line `export {` members, Rust `let mut`, and quoted shell text.
  Every one refuses a line that also calls the name bare. Two older
  rules were fixed on the way: a string nested in a `${...}` body is
  blanked (`${isLoading ? "animate-pulse" : ""}` kept `animate`
  visible), and the `eval("name()")` exception now needs a string that
  is *only* a call expression. `sanity --all` unexplained rows:
  claude-buddy 30 → 1, claude-code 957 → 51 (flagged targets 109 →
  28), cline 1,439 → 218 (200 → 20); grep-only totals unchanged, since
  only causes on grep-only rows move; every report target from the
  round reaches zero unexplained.
- **The same-named-local pass only looked inside the innermost
  function.** claude-buddy's `activeSlot` at `server/index.ts:1113`
  sits in an inline `server.tool(..., async () => {...})` callback that
  is not an indexed symbol, so the pass never ran; a local declared in
  an outer function was invisible from an inner arrow; a destructured
  parameter (`{ slots, cursor, activeSlot }`) never matched; and a
  bare call of a local (`const [state, dispatch] = useReducer(..)` then
  `dispatch({...})`) was refused as "call-shaped" although the resolver
  vetoes exactly that edge on the same evidence. The pass now scans
  every enclosing symbol and then the file's top level (up to the first
  indent-0 line when there is no enclosing symbol), matches parameters
  inside destructuring patterns and optional `name?` parameters, knows
  arrow and `function` parameters, `for`-of bindings and multi-line
  destructuring members, and explains a call of a bound local. The
  cause reads `... declared earlier in an enclosing scope ...` (was
  "in the enclosing function"). The guards stand: never a `x.name(`
  method call, never a call with no binding in scope, never a scanned
  declaration that is the target's own definition line, never a
  declaration indented deeper than the use.

## [1.5.9] — 2026-09-28

### Fixed
- **`dekko sanity` called a call to a same-named sibling an
  "unexplained miss".** When two unrelated symbols share a bare name
  (claude-code has a 1-arg `errorMessage(e)` in `utils/errors.ts` and
  a 2-arg one in `remote-setup.tsx`), a grep hit that is really a call
  to the other one was explained only when it sat inside that other
  symbol's own file; a call from any third file, the normal case for a
  reused helper name, fell through to "unexplained miss — inspect
  manually". The map already records which sites it attributed to
  each symbol, so such a row now gets an exact cause, `dekko attributes
  this line to a different, same-named declaration (see resolved_to) —
  a miss only if that attribution is wrong`, with the sibling's id in
  the row (`resolved_to` in JSON, `(resolved to <id>)` in text). It
  applies in single-target and `--all` mode, to recorded calls and
  recorded value references alike, and never to a line the map also
  attributes to the target itself. On claude-code, `sanity --all`'s
  unexplained count drops 1,383 → 957 and `remote-setup.tsx:
  errorMessage`'s 367 → 6; on cline 2,394 → 1,439, and single-target
  `sanity` on one of its three `ClineAccountService` classes no longer
  reports the other two classes' `getInstance()` bodies as misses (8
  → 1). The older file-shape cause ("call-shaped reference to a
  different, same-named declaration elsewhere in the repo") remains
  for a hit in a sibling's file that the map attributed to nobody.

### Known limitations
- A site the resolver left **ambiguous** among several same-named
  candidates is attributed to none of them and has no line in the map,
  so it still reads as unexplained or as the generic-name caution
  (claude-code's three one-arg `debug(msg)` helpers, 34 rows each).

## [1.5.8] — 2026-09-28

### Fixed
- **A Rust call through a `use std::` binding could reach a repo type of
  the same name.** dekko decides whether a `use` points into the repo by
  asking whether any segment of its path is a file's stem, so in a repo
  with a `path.rs` or a `collections.rs` (zed has `crates/path` and
  `crates/collections`), `use std::path::Path;` looked in-repo and
  `Path::new(..)` resolved to gpui's own `Path.new` at 1,377 call sites.
  `Command::new`, `Stdio::piped`, `PathBuf::from` and `HashMap::new`
  went wrong the same way. A Rust `use` rooted at `std`, `core` or
  `alloc` is now never in-repo, the same rule the full-path form
  (`std::path::Path::new(..)`) has always had. On zed: 2,087 wrong call
  sites removed, 0 added, 1,313 ambiguous rows counted external
  instead. On cline: 5 wrong `Command::new` sites removed.
- **An ambiguous Rust `Type::name(..)` call disclosed every namesake in
  the repo.** The resolver narrows such a call to `Type`'s own members
  before it picks, but when the pick failed, the ambiguous row recorded
  the list from before the narrowing: all 1,520 ambiguous `new` rows on
  zed named all 1,386 `new` functions, and every `new`'s callers note
  said "+1,520 ambiguous sites". The row now lists what the type path
  left (the `Editor::new(..)` row in `thread_view.rs`: 1,386 → 2
  candidates; `Editor.new`'s note: 1,520 → 243 sites). On zed, 2,053
  rows narrowed, none widened; edges are unchanged by this rule. A
  dot-call (`x.new(..)`) is not narrowed, as before.

### Known limitations
- A `Type::f(..)` call on a std type that reaches an in-repo extension
  trait's function (`PathBuf::try_from_bytes(..)` through zed's
  `PathExt`, 2 sites) now counts as external, like the full-path form
  always has. Telling that case from `Path::new` needs to know whether
  the std type implements the trait, which a blanket
  `impl<T: AsRef<Path>> PathExt for T` doesn't say.

## [1.5.7] — 2026-09-28

### Fixed
- **C++ argument counts ignored the defaults a header declares.** A
  default argument lives on the header prototype, and the `.cc`
  definition can't repeat it, so dekko read `Status ToGraph(Graph* g,
  GraphConstructorOptions opts = {})` as needing two arguments and
  counted every `scope.ToGraph(&g)` as external. dekko now reads C++
  prototypes (in headers, classes, namespaces and templates) and gives
  each definition with the same qualified name and parameter count the
  defaults they declare. Three things change. Calls that leave out a
  declared default resolve (`Scope::ToGraph` gains 88 call sites on
  tensorflow, `GetTypeFromTFTensorShape` 35). A call through one
  namespace that can't fit the one function on that path even with its
  defaults is counted external instead of reaching it through an
  `#include`: 542 generated `ops::Identity(scope, x)`-style calls no
  longer land on the unrelated `c/experimental/ops` functions. And an
  out-of-line constructor with a known declaration keeps its real
  minimum, so the kernel-builder call `Name("X").Device(DEVICE_CPU)` no
  longer credits `Device::Device` (117 sites). On tensorflow: 663 wrong
  call sites removed, 266 added, 518 ambiguous rows resolved, none new.
  A definition with no matching prototype resolves as before, and
  `map.json`'s parameter lists still show what the definition says.
  Editing a header prototype re-resolves only the files that call that
  name. Other languages are unaffected.

### Known limitations
- A generated `ops::X(scope, ..)` call whose argument count happens to
  fit the `c/experimental/ops` function of the same name still reaches
  it (19 sites on tensorflow). Only argument types could tell them
  apart, and the generated headers aren't in the repo.
- `Device::Device` now shows up in `unused`: its real callers are
  subclass constructors' initializer lists, which dekko doesn't read.

## [1.5.6] — 2026-09-28

### Fixed
- **A C++ call written through one namespace ignored that namespace.**
  `absl::OkStatus()` resolved to tensorflow's own `tensorflow::OkStatus`
  wrapper because it was the only in-repo `OkStatus`, `std::max(a, b)`
  landed on a repo `std::numeric_limits` specialization's `max`, and
  `gtl::MakeCleanup`, `xla::ConvertElementType` and the generated
  `ops::Add` all reached unrelated namesakes. Now, when the written
  scope is a namespace, the call only reaches symbols directly inside a
  namespace of that name, plus what a namespace-scope
  `using`-declaration there re-exports (`namespace tensorflow { using
  tsl::StatusFromTF_Status; }`). The ordinary resolver still picks among
  those, so `reference_ops::ResizeNearestNeighbor` now reaches the
  `reference_ops` one instead of `optimized_ops`. `std::` never resolves
  into the repo. A call through a class (`TensorShape::IsValid()`,
  `View<T>::Next()`) or a namespace alias resolves as before. On
  tensorflow this removes 11,312 wrong call sites (9,504 `absl::`,
  1,113 `std::`; `OkStatus` drops from 10,031 callers' sites to its
  real 806), adds 473 right ones, mostly from ambiguous rows that now
  have exactly one candidate in the right namespace (`mlrt::Execute`,
  `DeviceFactory::GetFactory`, `flags::Global`), and turns 5,698
  ambiguous rows with no candidate in the written namespace into
  external calls. `unused` loses 87 entries that did have callers. Of
  its 25 new entries, 16 had only wrong callers before. The other 9 are
  real functions whose namespace dekko misreads (below). Other
  languages are unaffected.

### Known limitations
- Functions after an unterminated macro line (`PYBIND11_MAKE_OPAQUE(T)`
  with no `;`) can lose their enclosing namespace in dekko's map, so a
  correctly qualified call to them (`tensorflow::InputTFE_Context(..)`)
  now counts as external. tensorflow has 79 such call sites.

## [1.5.5] — 2026-09-28

### Fixed
- **C++ calls written with a full path never resolved.**
  `tensorflow::Scope::NewRootScope()`, `test::function::GDef()`,
  `::tflite::ops::builtin::Register_ADD()`: any call with two or more
  scopes, or written from the root, came out named `Scope::NewRootScope`
  (or `tensorflow::Foo`) instead of `NewRootScope`, so it never matched a
  symbol and was counted external. Now the name is the last segment and
  the whole path must match the target's qualified name: `a::b::F()`
  only reaches a `F` whose scopes end in `a::b`, a path from the root
  (`::a::F()`) only reaches `a::F` itself, and a path that names no repo
  symbol stays external instead of being guessed. `std::` paths never
  resolve into the repo, so tensorflow's `std::numeric_limits`
  specialization isn't credited with every `numeric_limits<int>::max()`.
  On tensorflow this adds 3,510 caller → callee edges (4,067 call sites;
  `Scope::NewRootScope` alone gains 560) and drops 583 symbols from
  `unused` that were never dead, with no edge lost. Calls whose path
  names several overloads become ambiguous rows listing only those
  (+553). The same rule applies to base classes: 444 gtest fixtures
  written `: public ::testing::Test` no longer inherit from TFLite's own
  `tflite::testing::Test` wrapper.
- **A Rust `impl other::Trait for Name` could resolve to a same-file
  struct.** When the file also defined a struct named like the trait
  (or the implementing type shared the trait's name), the struct won,
  which is impossible, since only a trait can be implemented. The
  clause then either vanished as a self-reference or pointed at the
  wrong type. It now resolves to the trait: zed gains 12 impl edges
  (`impl workspace::DebuggerProvider for DebuggerProvider`, `impl
  sum_tree::Summary for Summary`, ...) and corrects one.

## [1.5.4] — 2026-09-28

### Fixed
- **Ambiguous-call disclosures named candidates the call could never
  reach.** The resolver already ignored other-language namesakes when
  picking a target, but it wrote the ambiguous row from the full
  same-name list. So a Python `print(...)` was "ambiguous" among 33 C++
  `print`s, and tensorflow's C++ `errors::InvalidArgumentError(...)`
  calls showed as 2,882 ambiguous calls to a Python class. Now a row
  lists only the 2+ candidates in the caller's language (or language
  family), and a call whose only namesakes are in other languages is
  counted external. Every ambiguous row now has at least two
  candidates. tensorflow goes from 260,507 ambiguous rows to 238,166
  (101k more got shorter), spring-boot from 70,727 to 69,970, zed from
  87,315 to 86,742, cline from 5,628 to 5,564. `query symbol Graph` on
  tensorflow no longer counts Python `tf.Graph()` calls against the C++
  class. The same rule now covers supertypes (`heritage_ambiguous`),
  thrown and caught types, and import aliases: cline's
  `import { main as generateHostBridgeClient } from "./….mjs"` used to
  be ambiguous with two Rust `main`s and now resolves.
- **Shell, Starlark, Gradle and other Tier-2 files resolved calls into
  any language.** A shell script's `exit` edged to a Python
  `control_flow_ops.exit`, `command -v` to a TypeScript variable, and
  a `.gradle` file's `id "java"` and `description = ...` to Java
  methods. Tier-2 calls now resolve only within their own grammar.
  Swift is the one cross-language exception: a bare Swift call can
  still reach a C function, since Swift imports C APIs directly, so
  tensorflow's 62 Swift → TfLite C API edges stay. This removes 1,331
  false edges on spring-boot, 208 on tensorflow, 29 on zed, 7 on
  claude-buddy and 1 on cline. It also drops 13 existing Swift edges
  into C++ methods and classes, which were all namesakes (Foundation's
  `URL(...)` landing on `tensorflow::data::URL`). Swift constructions
  of Swift types that used to be ambiguous next to C++ namesakes now
  resolve (19 on tensorflow).

## [1.5.3] — 2026-09-28

### Added
- **Kotlin is a Tier-1 language.** `.kt` and `.kts` files parse
  offline with the `tree-sitter-kotlin` grammar, now a core dependency,
  so a default install needs no `dekko[all]` for them. Classes,
  interfaces, enums, objects, functions and both kinds of constructor
  are symbols with typed parameters (defaults and `vararg` included,
  so argument counts check out), plus calls with receivers and
  argument counts, imports with aliases, supertypes and
  `System.getenv` reads. Java and Kotlin resolve against each other,
  each preferring its own language first: a Kotlin import of a Java
  class reaches the `.java` file, and a type that exists in both (the
  Java/Kotlin twins in docs samples) resolves to the importer's own
  language. A top-level function imported by package
  (`import org.springframework.boot.runApplication`) resolves to the
  file that defines it. `object : Base(x) { ... }` constructs `Base`,
  like Java's `new Base(x) { ... }`. Not yet for Kotlin:
  `throws`/`catches`, `::fn` references, type-only uses, and calls to
  extension functions through their receiver.

### Fixed
- **Kotlin files extracted no symbols.** The language pack's Kotlin
  grammar has no field names on declarations, and the generic
  extractor only accepts a named definition, so all 492 of
  spring-boot's `.kt` files mapped as "no symbols". A test-covered
  change to `runApplication` got "no symbol changes" from `diff` and
  "no impacted tests" from `affected`. Now spring-boot has 1,165 Kotlin
  symbols, `diff` reports the change, and `affected` names
  `SpringApplicationExtensionsTests.kt` as possibly impacted (its calls
  can't be pinned to one of `runApplication`'s two overloads). The
  Kotlin files gained 273 call edges (166 into Java) and 31 supertypes.
  309 wrong edges from the old path went away: `.gradle.kts` scripts
  "calling" unrelated Java methods, annotations counted as calls, and
  Kotlin docs samples resolving to their Java twin.

### Changed
- `query throws` on a language it doesn't cover says "not covered by
  this query" unless the language has no exception syntax at all
  (Rust, Go, C), which stay "permanently excluded". It used to call
  every uncovered language permanent.

## [1.5.2] — 2026-09-28

### Fixed
- **No C++ construction site reached the call graph.** `new
  Graph(reg)`, `new tensorflow::Graph(reg)` and
  `std::make_unique<Graph>(reg)` weren't extracted as calls at all,
  so `query callers` on tensorflow's `Graph` said "no callers" against
  hundreds of construction sites, and `affected` stopped at its
  constructors. `new T(...)` (brace, placement and argument-less forms
  too) and `std::`/`absl::` `make_unique`/`make_shared<T>(...)` now
  construct `T`. The class gets the edge, and so does the constructor
  the argument count picks, including constructors defined out of line
  in a `.cc`, with header default arguments allowed for. Overloads the
  count can't separate are disclosed as "resolved ambiguously" (both
  of `Graph`'s one-argument constructors). On tensorflow, `Graph` went
  from 0 callers to 399, `EagerContext` from 0 to 36, C++ constructors
  listed by `unused --kinds all` from 6,225 to 4,312, and C++ classes
  from 3,890 to 3,461. Stack declarations `T x(args);` are still not
  counted: without types they parse as function declarations.
- **A class and its own constructors counted as rivals.** Java and C++
  constructors share their class's name, so a construction whose class
  had two or more constructors went ambiguous whenever no other rung
  settled it. Now they collapse to the class before resolution. On
  spring-boot, 1,094 construction edges were added and 548 ambiguous
  records went away. A Java class with a builder method named
  `constructor(...)` no longer gets that method credited as its
  constructor.
- **`= delete` functions were symbols.** A deleted copy constructor
  was credited with real construction sites (93 edges on tensorflow),
  showed in `outline` and filled `unused`: copy/move-shaped constructor
  rows went from 716 to 85. Deleted functions are no longer extracted.
  `= default` ones still are.
- **A namespace-qualified C++ name could resolve to a same-named type
  in another namespace.** `absl::Status()` credited
  `tensorflow::experimental::cc::Status`, `xla::Parameter(...)` a
  `tensorflow::data::model::Parameter`, and `class X : public
  ::testing::Environment` a `tflite::gpu::cl::Environment`. A `q::Name`
  usage now resolves to a type only when `q` is one of its enclosing
  namespaces or classes. On tensorflow that removed 602 call edges and
  11 heritage edges, all wrong except about 100 `ops::NodeOut` sites.
  A qualifier that works through a `using`/`typedef` alias is left
  unresolved too, which is what happens to those (`ops::NodeOut` is
  `NodeBuilder::NodeOut`).

## [1.5.1] — 2026-09-28

### Fixed
- **`new X(...)` always credited the class's first-declared
  constructor, whatever the arguments.** On spring-boot, 481 classes
  declare two or more constructors; every construction site sat on
  the first one and all 637 later constructors read zero callers, with
  nothing disclosed. `query callers` on `ErrorPage(HttpStatus, String)`
  said "no callers" while the 1-arg `ErrorPage(String)` listed its
  2-arg call sites. The written argument count now picks the overload:
  an exact declared-count match first, then a unique varargs or
  default-argument fit, so `new SpringApplication(loader, sources)`
  reaches the 2-arg constructor. Overloads that only argument types
  could separate, like `ErrorPage(HttpStatus, String)` and
  `(Class, String)`, are disclosed as "resolved ambiguously" on each
  of them instead of guessed. The same applies to a C++ class with
  several constructors defined in its header. Class-level callers are
  unchanged. On
  spring-boot, 1,032 caller edges moved to a later constructor and 954
  construction sites are now disclosed as ambiguous. Because `unused`
  lists ambiguous candidates tagged `[dispatch?]`, constructors whose
  only construction sites are ambiguous now show up there with that
  tag (47 on spring-boot), where the first constructor's false credit
  used to hide the whole overload set.
- **A Java varargs parameter with a type annotation before the
  ellipsis (`Resolver @Nullable ... resolvers`) lost its varargs
  flag.** tree-sitter-java parses that shape as an error node, so the
  parameter counted as one fixed argument. It is varargs again, which
  keeps calls with fewer arguments on that constructor or method.
- **A comment between call arguments counted as an argument.**
  A multi-line `new ClineError(raw, undefined, // modelId` ...
  `"cline")` read as four arguments, and a call with four commented
  lines as nine instead of five, in every language. The count feeds the arity checks that keep
  a call off a target it can't be calling, so a commented call could
  be kept off the right one. Comments no longer count.
- **A typed Python `*args: T` / `**kwargs: T` parameter wasn't marked
  variadic** (the untyped forms were), so `List(a, b, c)` against
  `__init__(self, *components: TraceType)` read as a mismatch.
- **An unpacking argument counted as one argument.**
  `SparseTensor(*iterator.get_next())`, `f(**kw)`, `f(...rest)`,
  `f(xs...)` and C++ `f(args...)` supply a number of arguments only
  known at run time. Their count is now "unknown", which the arity
  checks never treat as a mismatch.

## [1.5.0] — 2026-09-26

Closes round 1.5's fix cycle. The code is 1.4.4; this release is the
version line catching up with the round, per `CONTRIBUTING.md`'s
"Testing rounds and the version line". Round 1.5 evaluated 1.3.7 on
seven real repositories and found two Medium and two Low issues, no
regressions. All four are fixed:

- **1.4.1**: `affected` printed the same test command once per file
  extension (`bun run test` twice for `.ts` + `.tsx`); hints now group
  by runner family.
- **1.4.2**: `affected --budget 0` still cut the runner line at 20
  paths, so `/impact`'s "rerun with `--budget 0`" gave the same
  truncated command. `--budget 0` now lists every impacted path
  (tensorflow: all 1,286 instead of 20), and a capped line says how to
  get the rest.
- **1.4.3**: `sanity` names three JS/TS non-call shapes (string or
  template text, recorded property reads, object keys/fields/typed
  parameters) and re-exports. `sanity --all` unexplained rows dropped
  127 -> 30 (claude-buddy), 4,319 -> 1,383 (claude-code) and
  4,406 -> 1,767 (cline), with the real misses still flagged.
- **1.4.4**: `--mcp-install --dry-run` registered for real. The MCP
  actions now preview their `claude mcp` command, and every action
  that can't preview refuses `--dry-run` instead of writing.

## [1.4.4] — 2026-09-26

### Fixed
- **`--mcp-install --dry-run` registered the MCP server for real.**
  `--dry-run` only worked with `--claude-install`/`--claude-uninstall`;
  every other action accepted it and ignored it, so the careful person
  who asked for a preview got a live write. `--mcp-install` and
  `--mcp-uninstall` now print the `claude mcp add|remove` command they
  would run and change nothing. Every other action (`--cline-*`,
  `--claude-md-*`, `--map`, bare `dekko DIR`) now refuses `--dry-run`
  with exit 2 instead of writing.

## [1.4.3] — 2026-09-26

### Fixed
- **`sanity` called plain TS/JS non-calls "unexplained miss".** On
  every TypeScript repo measured, `sanity --all --fail-on-unexplained`
  failed on lines a person dismisses at a glance, so the CI gate cried
  wolf. Three JS/TS-only shapes now get a named cause: the name only
  inside string or template text (`mention inside a string or template
  text`; the biggest shape by far, mostly log messages), a property
  read the map records at that line (`a property read of a same-named
  field`), and an object key, one-line interface/type field or typed
  parameter (the local-binding cause, whose text now says "object
  key/field"). `export { X } from` re-exports read as import
  statements. A line that also calls the name bare, or a string that
  calls it (`eval("x()")`), is never explained this way, so a real
  missed call still shows. Other languages are unchanged.
  `sanity --all` unexplained rows, same maps: claude-buddy 127 -> 30,
  claude-code 4,319 -> 1,383, cline 4,406 -> 1,767; total rows
  unchanged, and the known real misses (`new WebSocketTransport(...)`)
  still show.

## [1.4.2] — 2026-09-26

### Fixed
- **`affected --budget 0` still cut the runner line at 20 paths.** The
  line said `# +N more impacted test files not shown`, and `/impact`
  told the agent to rerun with `--budget 0` to get the full command,
  but the rerun printed the same 20 paths: on tensorflow, an agent
  following it ran 20 of 1,286 impacted `.py` files. `--budget 0` now
  lifts the cap on `affected` and `workset` (text, JSON `command` and
  `pytest`, and the MCP tools' `budget: 0`), so the line lists every
  impacted path. Any other budget keeps the 20-path cap, which still
  keeps a workset inside its budget, and the truncation tail now says
  `--budget 0 lists all`. `/impact` adds that on a set of hundreds the
  whole suite is often the saner run.

## [1.4.1] — 2026-09-26

### Fixed
- **`affected` printed the same test command once per file
  extension.** A change impacting both `.ts` and `.tsx` tests gave
  `bun run test` twice (text and the JSON `command`), so an agent
  following the hint ran the whole suite twice; mixed Java/Kotlin
  Gradle repos did the same with `./gradlew test`. Runner hints now
  group by runner family, one line per family, first-seen order.

## [1.4.0] — 2026-09-26

Closes round 1.4's fix cycle. The code is 1.3.8; this release is the
version line catching up with the round, per `CONTRIBUTING.md`'s
"Testing rounds and the version line". Round 1.4 evaluated 1.3.0 on
seven real repositories: the agents found no bugs and no regressions,
and re-timing cline turned up one Low. Between the round and its fix,
1.3.1 through 1.3.7 went to the Claude Code plugin. Everything since
1.3.0:

- **1.3.1-1.3.3**: plugin skills and commands no longer give stale
  guidance; `dekko-orient` and `dekko-verify` cost about 2.9k fewer
  tokens to load; the MCP tool schemas cost about 600 fewer tokens a
  session.
- **1.3.4-1.3.6**: three new skills. `dekko-refactor` lists every site
  a rename, move or signature change must touch and proves nothing was
  missed; `dekko-delegate` briefs a subagent with a dekko digest before
  it's dispatched; `dekko-debug` works a traceback from the map.
- **1.3.7**: the `/impact` command, impacted tests plus a ready-to-run
  test command.
- **1.3.8** (the round's fix): repeat `diff`/`affected` calls on an
  edited, unmapped tree reuse the last in-memory re-map under the
  daemon and the MCP server (cline 3.3 s to ~0.4 s, tensorflow ~30 s to
  ~3 s), and a stale map is no longer parsed just to be rejected.

## [1.3.8] — 2026-09-26

### Performance
- **Repeat `diff`/`affected` calls on an edited, unmapped tree are fast
  under the daemon and the MCP server.** With a stale map, both
  commands re-map the working tree in memory (they never write the
  map) and used to throw that work away, so an agent's edit,
  `impacted_tests`, `impacted_tests` paid the full re-map every time.
  The daemon and `dekko serve --mcp` now keep the last one and reuse it
  while the tree holds the same content: repeat calls went from 3.3 s
  to ~0.4 s on cline and from ~30 s to ~3 s on tensorflow, with
  byte-identical output. Any edit, or a new `dekko map`, starts over.
  The cost is one extra current-tree snapshot in memory, about the
  size of the map, dropped once a call finds the map fresh again.
- **A stale map is no longer parsed just to be rejected.** `diff` and
  `affected` judge freshness from the small provenance sidecar and load
  `map.json` only when it is current, which takes a map load off every
  stale call, including from a plain shell (cline 3.3 s to 2.8 s,
  tensorflow 30 s to 23 s).

## [1.3.7] — 2026-09-26

### Added
- **`/impact [REV] [--possible]` command.** Runs `dekko affected` and
  relays the impacted test files by tier with the ready-to-run test
  command the report ends with, so "which tests should I run for
  this change" is one slash command in Claude Code instead of a CLI
  call plus reading its output. Claude is told to rerun with
  `--budget 0` when the runner line says `+N more impacted test files
  not shown`, so a budget-capped report never becomes a partial test
  run; to relay the count of tests reached only through an ambiguous
  call and offer `--possible`; and to treat a `no impacted tests`
  answer against the default rev as a prompt to pick an older base
  when the change is already committed. `dekko-orient`'s table names
  it next to `dekko affected`.

## [1.3.6] — 2026-09-26

### Added
- **`dekko-debug` skill.** Fires on a traceback, exception, failing
  test error, or a "why does this raise / who handles it / how did
  control get here / where is this env var read" question in a mapped
  repo. Rather than opening each frame's file, it starts from the
  innermost in-repo frame with `query_symbol` (notes first), finds
  the raise with `query throws` (walking callees with `--transitive`
  and treating the depth-cap and re-raise notes as instructions, not
  footnotes), checks handling with `query catches` plus the type's
  supertypes since matching is exact-name, reconstructs a truncated
  or async path with `get_callers` or `trace`, rules out
  configuration with `query env`, and finishes with `impacted_tests`.
  The one grep it expects is for the error message text, which dekko
  doesn't model. The plugin now ships eight skills.

## [1.3.5] — 2026-09-26

### Added
- **`dekko-delegate` skill.** Fires before a subagent is dispatched
  into a repo with a `.dekko/` directory. A subagent starts with an
  empty context and no session-start orientation, so it re-explores
  with grep and whole-file reads and then reports that exploration
  back, which the parent pays for twice. The skill has the parent
  compute one budgeted digest scoped to the agent's job (`dekko
  orient --budget 800` to explore, `dekko workset --budget 2000` to
  implement, `dekko context` or `outline` for one symbol or file),
  paste it verbatim under its own heading, and add a short paragraph
  naming the MCP tools and the ladder, with the rule that the agent
  must never stop or kill the parent's dekko processes. It also
  covers fan-out (one `orient`, one `outline <dir>` per agent, the
  daemon for CLI-heavy agents) and asks agents for `path:line`
  reports instead of pasted source. The plugin now ships seven
  skills.

## [1.3.4] — 2026-09-26

### Added
- **`dekko-refactor` skill.** Fires before a rename, move, or
  signature change and again after the edits. Before: pin the target
  with `query_symbol`, then list every site from the map by kind of
  change (call sites with `sites` and `include_tests` on, type
  annotations and implementors for a type, import lines and their
  aliases via `deps --file` / `query importers`), with no budget-
  omitted rows. After: `find_usages <old name>` lists exactly the
  direct calls still pointing at a name that no longer exists, `query
  importers` the leftover import lines an alias would otherwise hide,
  then `impacted_tests` and the `note list --orphaned` sweep, and one
  grep only for the text dekko doesn't model. This guidance was spread
  across `dekko-orient`'s CLI-only table, `dekko-verify` and
  `dekko-notes`; the plugin now ships six skills.

## [1.3.3] — 2026-09-25

### Changed
- **The MCP tool schemas cost less per session.** Every tool's
  `tools/list` entry is sent to the model at the start of each session
  the plugin is enabled in. The eight longest tool descriptions no
  longer repeat what their own parameters already document (`sites`,
  `include_tests`, `transitive`, `exact`, `type_impact`) or explain
  extractor internals; each keeps what the tool returns, when to reach
  for it, and the one caveat that prevents misreading a result. The
  shared symbol-argument text and a few parameter descriptions were
  tightened the same way. The whole payload went from about 18.6k to
  about 16.2k characters, roughly 600 tokens a session, with no change
  to tool names, parameters, or behavior.

## [1.3.2] — 2026-09-25

### Changed
- **The `dekko-orient` and `dekko-verify` skills cost less to load.**
  `dekko-orient` fires before any Grep, Glob or Read, so its body is
  paid for repeatedly in a session. It went from about 3.3k tokens to
  about 1.15k: the ladder, one need-to-tool table with MCP and CLI
  columns, and target syntax. The CLI-only queries, the CLI flag for
  each knob, the digest budgets and the staleness tools moved to a
  `reference.md` beside it that Claude reads only when it needs them.
  The per-call knob guidance no longer repeats what the MCP tool
  schemas already say. `dekko-verify` went from about 1.7k tokens to
  about 1k with every trigger condition kept.

### Fixed
- **`dekko-orient` gave the wrong CLI test-filter flag for search.**
  `dekko search` takes `--include-tests`; `dekko query callers`
  includes tests unless given `--no-tests`.

## [1.3.1] — 2026-09-25

### Fixed
- **Plugin skills and commands no longer give stale guidance.**
  `dekko-verify` called Go `pkg.Func()` cross-package calls a known
  resolver blind spot, but those have resolved since 0.31.0; it now
  leads with trait/interface dispatch and says a qualified call can
  still drop through a re-export or alias. `dekko-review-context`
  named a review command that isn't part of Claude Code and pointed
  at a `dekko review` command that doesn't exist yet; it now names
  `/code-review` and drops the roadmap section. `dekko-orient` lists
  `dekko query file` (a file's symbol list, cheaper than `outline`)
  and `dekko ledger` (what the session already has in context).
  `/sanity` lists `--all` in its argument hint and says how to relay
  the `--all` triage summary. `docs/claude-code.md` matches.

## [1.3.0] — 2026-09-25

Closes round 1.3's fix cycle. The code is 1.2.4; this release is the
version line catching up with the round, per `CONTRIBUTING.md`'s
"Testing rounds and the version line". Round 1.3 evaluated 1.1.10 on
seven real repositories and found no regressions and nothing
Critical: two Mediums, a few carried Lows, and one more bug found
while re-running the MCP checks on tensorflow. Four fix tracks, all
shipped:

- **1.2.1**: conflicting MCP target names (`symbol`, `name`,
  `target`, `type`) are always an error, including when one of them
  is the tool's own argument. The same value under two names still
  folds.
- **1.2.2**: `unused` sees getters and handlers that are read rather
  than called, and object literals handed to external packages. On
  claude-code, flagged callables with no `[dispatch?]` mark went from
  112 to 50.
- **1.2.3**: the MCP default-root note is `(default root: <path>)`,
  23 tokens instead of 38 on every call that omits `root`.
- **1.2.4**: integer arguments are validated on MCP and the CLI. Bad
  values used to return `dekko: internal error` (21 of 70 probe calls)
  or be quietly misread. Also fixed: `trace --max-paths 0` reporting
  no path, and `query callers --limit 0` printing nothing.

`docs/cli.md` now says what `--json`'s `meta.tokens` measures.

## [1.2.4] — 2026-09-25

### Fixed
- **MCP integer arguments fail as the caller's mistake, not dekko's.**
  `limit`, `hops`, `packs` and `top` went through a bare `int()`, so
  `"abc"`, a list, or (on `search_code`, `impacted_tests`,
  `get_context_pack`, `workset` and `check_ambiguous`) `null` came
  back as `dekko: internal error: invalid literal for int()`: 21 of 70
  bad-value calls on one repo. Every integer argument, `budget`
  included, now follows one rule: absent or `null` means the default;
  an integer, a whole-number float (`20.0`) or a numeric string
  (`"20"`) is read as that number; `true`, a fraction, a list or a
  negative value is an error that names the argument. `true` used to
  read as 1 and `2.5` as 2.
- **Negative row counts are rejected on the CLI.** `--limit`, `--top`,
  `--hops` and `--packs` took any integer, and a negative one reached
  a `rows[:limit]` slice: `query callers X --limit -1` dropped the
  last row and suggested raising the limit. They are usage errors
  now, like a negative `--budget`.
- **`trace --max-paths 0` no longer reports a missing path.** It
  answered "no resolved call path" (exit 1) for a pair `--max-paths 1`
  connects. `--max-paths` now takes 1 or more.
- **`query callers`/`callees --limit 0` prints its footer.** It
  printed nothing at all, which reads as "no callers"; it now prints
  `(~0 tokens · N of N omitted · raise --limit)`, as `query uses`,
  `outline`, `search` and `unused` already did. MCP `get_callers`,
  `get_callees` and `find_type_usages` with `limit: 0` get the same
  footer.

## [1.2.3] — 2026-09-25

### Changed
- **The MCP default-root note is one short line.** Every reply to a
  call that omits `root` now starts with `(default root: <path>)`
  instead of `(root: <path> — no 'root' argument was given; pass one
  to target a different repo)`: 23 tokens instead of 38 on a typical
  path, paid on every such call. It still appears on success and
  error replies alike, still carries the full path, and still says
  the root was defaulted; the "pass one" hint went because every
  tool's input schema already lists `root`.

## [1.2.2] — 2026-09-25

### Fixed
- **`unused` sees getters and handlers that are read, not called, and
  object literals handed to external code.** On claude-code, 91 of the
  112 flagged functions that carried no `[dispatch?]` mark were
  members of object literals: command objects whose `isHidden` and
  `immediate` getters are read as `cmd.isHidden` (a shape no call edge
  covers), and the 30 methods of the react-reconciler host config,
  which `react-reconciler` calls and nothing in the repo does. Both
  looked like dead code. Two changes:
  - The map gains a `reads` section: property reads (`x.name` not
    called and not assigned, `cmd?.name`, `const { name } = x`) in
    JS/TS/TSX, grouped per reader and name, kept for the names some
    repo callable or variable defines. A read is never an edge and
    never marks anything used: property names are far too common for
    a name match to pick a definition (the one repo symbol named
    `type` had 2,735 `msg.type` reads against it). It is the third
    `[dispatch?]` evidence, `property-read`, for a flagged symbol
    whose name is read somewhere and that either shares the name with
    2+ definitions or is itself an object-literal member. `--dispatch`
    rows say which, and `sanity --unused` prints the read sites.
  - Symbols record whether they are object-literal members and, when
    the literal is a direct argument of a call, that call's callee.
    A member whose literal went to a binding imported from outside
    the repo (`createReconciler(...)` from `react-reconciler`,
    `createContext(...)` from `react`, `marked.use(...)`) is a root,
    like `decorated` and `exported`. The import is the test, not the
    external table: a literal passed through a receiver the resolver
    couldn't follow (`deps.callModel({...})`) is consumed in-repo and
    stays flagged.

  On claude-code the unmarked flagged functions drop from 112 to 50:
  34 rows leave the list as roots and 32 gain the mark. No edge set
  changed on any of the seven evaluation repositories.
  Maps written before this load as before. Arrow-function properties
  (`isEnabled: () => ...`) are not symbols at all and are unchanged.

## [1.2.1] — 2026-09-25

### Changed
- **Conflicting MCP target names are always an error.** The tools that
  take a target accept it under any of `symbol`, `name`, `target` or
  `type`. Since 0.43.32 the tool's own name silently won when a
  caller sent it beside a different value under another name, and
  1.1.10 promised an error for that case without delivering one: the
  check ran only when the tool's own name was absent, so 12 of the 24
  name pairs picked silently and 12 errored. Two different values in
  one call is a caller bug (a stale value, a copy-paste), and the
  silent pick hid it behind a confident answer about the wrong
  symbol. Every pair now errors, and the message names each key and
  value it saw (`got symbol='f', name='g' naming different targets;
  pass one 'symbol' argument`). The same value under several names
  still folds to the tool's own name, and a JSON `null` counts as an
  absent argument. Round 1.3 found this on four of seven repositories.

## [1.2.0] — 2026-09-25

Closes round 1.2's fix cycle. The code is 1.1.11; this release is the
version line catching up with the round, per `CONTRIBUTING.md`'s
"Testing rounds and the version line". Round 1.2 evaluated 1.0.5 on
seven real repositories and found no regressions, but its
edit-allowed brief turned up four Highs. Those, the Mediums and Lows,
and three more bugs found while designing the fixes became ten fix
tracks, all shipped, plus one Windows fix found while releasing:

- **1.1.1**: `affected` reports only test files a runner would
  discover, so test-support code under `testing/` is no longer an
  "impacted test".
- **1.1.2**: TypeScript arrow-function and callback parameter types
  count as type usages (`query type`, `unused`, `workset
  --type-impact`).
- **1.1.3**: external callee ids are stored canonically, without call
  arguments. The longest id on any evaluation repo went from 1,749
  characters to under 162, and Rust turbofish and C++ template calls
  are named correctly.
- **1.1.4**: `sanity` explains misses in Rust inline test modules,
  Rust `use` lines and files the map skipped. zed's `--all`
  unexplained count fell from 6,356 to 2,800.
- **1.1.5**: Rust code compiled only under `cargo test` is test code.
  1,826 zed symbols moved, and `unused` dropped from 11,141 to 10,392.
- **1.1.6**: `.ts` and `.tsx` resolve as one language, with a
  relative-import tiebreak.
- **1.1.7**: `unused --dispatch` marks each candidate row, and an
  explicit `--limit` lifts the section's cap.
- **1.1.8**: `affected` includes Rust `cfg(test)` files (zed `HEAD~1`
  46 -> 213 impacted), and tests reached only through an ambiguous call
  are counted in a note and listed by `--possible`.
- **1.1.9**: a stale map's `diff`/`affected`/`workset` reuse `dekko
  map`'s caches. A dirty-tree tensorflow `diff` went from 205 s to
  32 s.
- **1.1.10**: ten small fixes, including `--budget 0` meaning no cap,
  `query file` on empty files, a fully keyed rev cache, and MCP
  target-argument aliases.
- **1.1.11**: test filename patterns match case-sensitively on
  Windows, where `*Test.*` had made `test.rs` and `latest.py` test
  files. Caught by this release's own Windows CI.

The Claude Code skills and the MCP docs were brought up to date with
these changes ahead of the release. Round 1.3 (dekko 1.1.10,
2026-09-25) has since run on the same seven repositories: all ten
tracks held, with no regressions. Its findings open the 1.2.x line.

## [1.1.11] — 2026-09-25

### Fixed
- **Test filename patterns match case-sensitively on Windows.** The
  `*Test.*` and `*Tests.*` patterns went through `fnmatch.fnmatch`,
  which folds case on Windows, so `test.rs`, `ed_tests.rs` and even
  `latest.py` counted as test files there: hidden by `--no-tests`,
  reported by `affected`, and left out of `unused`. They now match the
  way they always have on macOS and Linux, where nothing changes.

## [1.1.10] — 2026-09-25

### Fixed
- **`query file` no longer calls mapped files unmapped.** It only
  searched files that define symbols, so a docstring-only
  `__init__.py`, a `package-info.java` or a barrel file got "no mapped
  file matches" and exit 3. That's 2,374 files on spring-boot and
  2,547 on tensorflow. With `--no-tests`, a file holding only test code
  got the same answer. Both now exit 0 and say why the list is empty
  (`mapped, no symbols`, or `only test code (58 symbols hidden by
  --no-tests)`, also as `hidden_test_symbols` in `--json`). `query
  cohesion` shares the fix. Files that define symbols are still matched
  first, so no path that resolved before resolves differently now.
- **`--budget 0` means no cap.** A zero budget kept exactly one row on
  every command, and the five commands with a default budget
  (`affected`, `workset`, `search`, `summary`, `orient`) had no way to
  ask for everything. `0` now lifts the token cap (and, like any
  explicit budget, the default row limit) on the CLI and every MCP
  tool. A negative budget is a usage error.
- **The rev cache is keyed like the other caches.** A cached old-side
  snapshot was checked against the extraction spec only, so one built
  before a resolver change, or by an older dekko, kept serving its old
  caller lists to `diff`/`affected`/`workset`. It now also has to
  match the dekko version and the resolver, and a stale entry is
  rebuilt once. The daemon's "is this rev cached?" check reads the
  same stamp, so a stale entry no longer gets the short cache-hit
  timeout.
- **A failed export says what failed.** `diff` against a valid commit
  on a full disk printed "unknown rev or not a git repo". Every export
  failure now carries its real reason: git's own error, the timeout, or
  the extraction error (`No space left on device`).
- **Rust attributes that tree-sitter can't parse no longer become
  calls.** An attribute on a destructured struct field
  (`#[cfg_attr(not(..), allow(..))] icon,`) is outside the grammar, and
  error recovery turned its payload into call expressions. On zed that
  made `Window::new` call an unrelated test helper named `not`.
- **`outline --budget` isn't cut off at 200 rows.** An explicit budget
  with no `--limit` now lets the budget govern, as it already did for
  `query` and the MCP `outline` tool.
- **MCP tools accept each other's target argument names.** The tools
  that take a target call it `symbol`, `name`, `type` or `target`
  depending on the tool, and agents guess by analogy. Every one of them
  now accepts any of the four. Two different targets under different
  names is an error rather than a silent pick.
- **The process-pool retry note states what happened.** It blamed
  "another concurrent dekko process", but the usual cause on macOS is
  the Objective-C runtime aborting a forked worker, with nothing else
  running. The note now says a worker crashed and, when the pool
  forked on macOS, names that check.
- **The `pre-read` hook works without a `cwd` in its payload**, and
  when `cwd` and the read path spell the repo differently through a
  symlink.

### Internal
- A daemon test failed intermittently with `I/O operation on closed
  file`. Its helper returned a canned reply without waiting for the
  daemon thread to leave its output redirect, which could then restore
  pytest's already-closed capture stream as `sys.stdout`. The helper
  now drains the real reply first.

## [1.1.9] — 2026-09-25

### Fixed
- **`diff` and `affected` on an edited tree no longer re-resolve the
  whole repo.** When the map was older than the working tree, the
  current side was re-mapped from scratch on every call and never used
  the caches `dekko map` keeps. Parsing turned out to be the small part:
  on tensorflow, re-resolving calls was 146 of the 180 seconds. The
  current side now reuses both the extraction cache and the cached call
  resolution for everything the edit can't have affected, like an
  incremental map, and still writes nothing. A one-line edit's `diff`
  on tensorflow goes from 205s to 32s, and `affected` to 32s, with
  byte-identical output. spring-boot's `affected` goes from 12.6s to
  9.4s. An added, removed or renamed file, or a changed type, still
  resolves the whole repo, as it does for `dekko map`. `cache.json` is
  now parsed at most once per call, and not at all when neither side
  needs it.
- **A map built with `--follow-symlinks` is diffed with it.** `diff`,
  `affected` and `workset` dropped the recorded setting on both sides,
  so they compared a different set of files than the map held.
- **Cached call resolution is never paired with an extraction cache
  from a different run.** `dekko map` writes the two files one after
  the other, so a `diff` reading them during a concurrent map could get
  one of each and keep a stale edge. The mismatch is now detected and
  the whole repo is resolved instead. After a normal run it never fires.
- **Long waits say so first.** On repos with 5,000+ mapped files, a
  stale map's in-memory pass (`diff`/`affected`) and the auto-regen
  every other read command does (including `workset`: 60s of silence on
  tensorflow) print a `note:` to stderr before they start. The
  `diff`/`affected` note says whether cached resolution can be reused
  or the whole repo has to be resolved. Building a missing map is
  always announced.

## [1.1.8] — 2026-09-25

### Fixed
- **`affected` reports Rust files that hold `cargo test` code.** It only
  listed test *paths*, and most Rust unit tests live in a
  `#[cfg(test)] mod tests` inside an ordinary source file, so zed's
  were invisible to it. A reached symbol that only `cargo test` compiles
  now makes its file an impacted test file. On zed, `affected HEAD~1`
  goes from 46 to 213 test files, with nothing lost; every addition is
  a `crates/*/src/*.rs` file whose reached symbol is a `cfg(test)` test
  (`crates/vim/src/motion.rs`'s `test_start_end_of_paragraph`).
  `testing/` test-support code is still never listed.
- **`affected` says when a test reaches the change only through an
  unresolved call.** Its walk follows resolved calls, so a test calling
  `handler.createMessage()` (nine classes define `createMessage`) was
  silently missing on cline. Following ambiguous calls instead reaches
  most of a large suite (a median of 689 test files on zed), so these
  are *possible* impacts: never in the impacted list, the runner hint or
  the exit status. A note counts them and names the strongest lead,
  ranked by shared directories with the code it may reach, then by
  fewest same-named definitions. On cline, the report's missing
  `vscode-lm-handler.test.ts` is that lead. `--possible` lists them all.
  JSON always carries `possible_total`/`possible_example`, `workset`
  counts them as `possible_tests_total`, and MCP `impacted_tests` shows
  the note.
- **`workset` keeps impacted tests when the budget runs short.** The
  tests tier came last, after outline detail, so on zed 16 of 40
  symbol seeds printed `impacted_tests: []` beside a nonzero
  `impacted_tests_total`. The first 20 test rows now come right after
  files and packs, and the rest stay last (as "more impacted tests:" in
  text): 0 of 40 lose them all.
- **Exit status is documented.** `affected` and `diff` `--help` state
  0/1/2, and `docs/cli.md` no longer says to parse `--json` output only
  on exit 0: exit 1 (`affected`, `diff`, `unused`, `status`, `trace`)
  is a normal answer with full stdout.

## [1.1.7] — 2026-09-25

### Fixed
- **`unused` shows which flagged rows dynamic dispatch might reach,
  and lists all of them on request.** On claude-code, 250 of the
  flagged symbols are `Tool` interface methods (`prompt`,
  `description`, `isConcurrencySafe`, ...) defined on ~40 tool object
  literals and called as `tool.prompt(...)`. `unused` already knew 211
  of them were dispatch candidates and said 56% of the list was, but
  nothing showed which rows: `--dispatch` printed 20 rows, all from
  `src/bridge/`, and `--limit 1400` couldn't raise that cap, though
  the footer said to raise `--limit`. Three changes:
  - Every candidate row in the main listing is marked: text rows end
    in `[dispatch?]`, JSON rows carry `"dispatch_candidate": true`
    (absent on other rows; +5% JSON tokens on claude-code).
  - An explicit `--limit` now sets the `--dispatch` and `--suspect`
    section caps in either direction. They still default to 20, and
    the main list to 50.
  - Receiver calls the resolver never resolves by design (`description`,
    `parse`, `build` and other built-in method names always go
    external) now count as dispatch evidence when 2+ repo symbols
    define the name. That is the other 39 of the 250, all
    `tool.description()`. `--dispatch` JSON rows say which evidence
    they rest on, `"ambiguous"` or `"guarded-name"`. Guarded-name
    candidates: claude-code 46, cline 5, spring-boot 11, tensorflow
    40, zed 85. `unused` totals are unchanged.

  `--dispatch` help, the caveat and `docs/cli.md` now say the check
  covers receiver calls through interface- or trait-typed values, not
  only `this.method()` through a base class.

## [1.1.6] — 2026-09-24

### Fixed
- **`.ts` and `.tsx` files are one language to the call resolver.**
  Before any evidence was weighed, resolution kept only candidates in
  the call site's exact language, and `typescript` and `tsx` counted
  as two. A `.tsx` file calling a function it imports from a `.ts`
  file lost the real target whenever some other `.tsx` file defined
  the same name: on claude-code, `mcp.tsx` called
  `ManagePlugins.tsx::getScopeLabel` instead of the
  `services/mcp/utils.ts` one it imports, and `REPL.tsx`'s imported
  `errorMessage` was reported as external. Interface calls went wrong
  the same way: `tool.extractSearchText()` was pinned to BashTool's
  implementation, the only `.tsx` one of six, so the other five read
  as unused.

  Merging the dialects exposed a tie the filter had been hiding. A
  named import is stored as `<module>/<name>`, and the import rung
  matches file stems, so `import { ClinePassLimitError } from
  './errors'` matched both `errors.ts` and an unrelated
  `ClinePassLimitError.tsx` component, and gave up. A relative
  specifier now settles that tie: the module is resolved against the
  caller's directory with the same extension and `index` rules the
  module graph uses, and the one tied candidate in that file wins.
  Package and alias specifiers are left alone.

  claude-code: resolved calls 26,696 -> 26,492 (+176 -380), references
  10,100 -> 10,209 (+157 -48). cline: calls 20,648 -> 20,749 (+121
  -20), references 9,171 -> 8,853 (+227 -545; 545 of them were `fs`
  and `path` module references credited to two locals in one test
  file). Nearly every removed edge had no import behind it; 275 of the
  added calls and 384 of the added references follow a real import.
  Twelve removed edges did have one, and each is now honestly
  ambiguous or was wrong. Seven added edges on claude-code are wrong:
  bare `set(...)`/`unmount()` calls on local bindings, which a
  weaker last-resort step now matches once a second candidate exists.
  awesome-go, claude-buddy, spring-boot, tensorflow and zed are
  byte-identical.

## [1.1.5] — 2026-09-24

### Fixed
- **Rust code compiled only under `cargo test` is test code.** Dekko
  recognised Rust tests by module *name* (`mod tests { }`), and Rust
  decides by attribute. On zed 1,826 symbols the compiler builds only
  for tests were production code to every command: 1,558 in files
  declared out of line (`#[cfg(test)] mod editor_tests;` in
  `editor.rs`, plus `vim/src/test.rs`, `tests.rs` and friends), 153 in
  `#[cfg(test)]` modules with other names, 115 on single `#[cfg(test)]`
  functions and `impl` blocks. `--no-tests` (the MCP tools' default)
  kept them, so 174 test helpers like `StubAgentConnection.end_turn`
  showed production callers that are really tests, and `unused`
  flagged 749 test symbols as dead code.

  The extractor now evaluates each `cfg` predicate (`test` false,
  every other atom unknown) and flags whatever can't be built without
  `test`: an item, a module, a whole file opening with `#![cfg(test)]`,
  or a file declared `#[cfg(test)] mod x;` from its parent, and that
  file's own submodules. The last is decided across files at map time,
  never cached, so editing only the parent re-flags the child. Code
  gated `any(test, feature = "test-support")` stays production: the
  feature builds it into benchmarks and binaries, which call it. `unused`
  no longer flags any test-flagged symbol.

  zed: 1,826 symbols flip to test and none flip back; symbols, edges
  and every other map section are byte-identical on all seven eval
  repos; the `--no-tests` view drops from 50,790 symbols / 88,733
  caller entries to 48,964 / 81,619; `unused` from 11,141 to 10,392,
  every removed row test code. Full-map time on zed is within noise.
  `affected` still reports by file path, so Rust `#[test]` functions
  in ordinary source files are not yet listed as impacted tests.

## [1.1.4] — 2026-09-24

### Fixed
- **`dekko sanity` stops calling ordinary Rust lines "unexplained".**
  On zed, `sanity MultiBufferOffset` labelled 407 of its 814 grep-only
  rows "unexplained miss — inspect manually", and `DevicePixels` 50 of
  220, which made `--all --fail-on-unexplained` useless as a CI gate
  on a normal Rust repo. Three causes, none of them a resolver bug:
  - *Inline test modules.* `--no-tests` drops a caller when its path
    is test code **or** the extractor flagged it as test code (a
    function inside `#[cfg(test)] mod tests { ... }`). `sanity`
    compared against the already-filtered view, where those symbols
    no longer exist, so it could only recognise test code by path.
    It now reads the flag from the unfiltered map, the same predicate
    the filter applies. 186 of the 407 rows.
  - *Rust `use` lines.* A single-line `use a::{X, Y};` and each row of
    a multi-line `use` list are now "import/require statement naming
    the symbol". Checked against tree-sitter over all 26,610 `use`
    lines in zed's 1,923 Rust files: none missed; the only other lines
    claimed are `use` text inside string literals and macro bodies,
    which aren't calls either.
  - *Files the map never parsed.* A supported-language file skipped as
    too large or generated, or excluded, used to fall through every
    rung. It now gets its own cause, "file not in the map ... dekko
    never parsed it", checked before every other rung, since the
    others explain a resolver miss and the resolver never saw the
    file. 120 of the 407 rows sat in one 1.3 MB test file.

  Result: `MultiBufferOffset` 407 → 39 unexplained, `DevicePixels`
  50 → 0. The 39 left are calls inside macro arguments (`vec!`,
  `assert_eq!`, a documented gap) and a few type positions no rule
  covers yet; they stay "unexplained" on purpose. Across zed's whole
  `sanity --all --max-names 300` sweep, unexplained rows drop from
  6,356 to 2,800. On the six non-Rust eval repos the unexplained count
  is unchanged; the only rows that move are ones in skipped files
  (tensorflow 203, claude-code 1) taking the new cause.

## [1.1.3] — 2026-09-24

### Fixed
- **External callee ids are bounded and canonical.** A call's
  receiver used to be stored as its source text, arguments and all,
  so `expect(result.foo).toBe` or a thirty-line array literal's
  `.join` became the external callee id: claude-buddy's longest was
  1,749 characters, claude-code's 122,129, and half of a TypeScript
  or Rust repo's distinct external ids were argument-bearing chains.
  The extractor now renders the receiver structurally, identifiers
  and member chains verbatim, a call as `name()`, a subscript as
  `name[]`, a literal as `""`/`[]`/`{}`, anything else as `(…)`, and
  caps a chain of many hops to its head, `…` and member. Every head
  token the resolver keys on (an import binding, `this`/`self`, a
  parameter name, a Rust `std` path) is byte-identical to before.
  Rows that differed only by an argument merge: cline's external
  table drops from 53,600 rows / 24,646 distinct ids to 46,453 /
  14,172, zed's from 197,553 / 97,137 to 189,714 / 77,367, and no
  eval map has an external id over 161 bytes. `query uses` rows read
  `[chalk.hex().bold]` instead of the source line, and its "closest
  external names" hint now applies a quality floor (no whitespace,
  brackets, quotes, or entries over 40 characters), so
  `expect(result` can no longer be suggested. Resolved edges are
  unchanged on claude-buddy, awesome-go and cline. On claude-code
  (-3), zed (+218/-24) and spring-boot (+19/-1) every moved edge was
  read: a multi-line chain such as `z\n .string()` used to store its
  head as `"z "`, which matched no import or parameter, so the call
  either resolved by bare name to an unrelated symbol (those false
  edges are gone) or sat ambiguous where a parameter's type now
  settles it (those edges are new and correct). A C++ template
  member name longer than 40 characters is stored as `add<>` rather
  than with its argument list (2,350 characters on one tensorflow
  call); a short one such as `Get<int>` is kept, since a template
  specialization is a symbol with exactly that name.
- **Rust turbofish calls are named after their real callee.**
  `xs.iter().map(f).collect::<Vec<_>>()` was named `iter`: the
  `generic_function` node has no name field and the raw-text
  fallback cut at the first `(`. 2,859 of zed's 7,372 turbofish
  calls carried the wrong name and resolved to it. Found by the
  edge gate above, where the canonical text stopped masking one;
  103 of zed's moved edges are these.

## [1.1.2] — 2026-09-24

### Fixed
- **`query type`/`find_type_usages`/`unused` now see a type used
  only on a function-shaped site the map has no symbol for**
  (TypeScript/TSX). A parameter or return annotation on a returned
  or callback arrow function, a function-typed interface member, a
  `type Fn = (a: A) => B` alias, a method/call/construct/overload
  signature or a class-field arrow was parsed by nobody: only the
  five named function shapes ever filled `Symbol.params`/`returns`,
  so `query type` reported no results for a type used only there and
  `unused --kinds types` flagged it. The extractor now records those
  annotations as `type_uses` (a new, additive `map.json` section: one
  record per typed parameter and per return type, attributed to the
  innermost enclosing definition or to the module) and the read side
  matches them with the same token rule it applies to a symbol's own
  signature. A site row prints its file and line, the kind of site
  and its owner: `sdk-followup-coordinator.ts:31  function type in
  SdkFollowupCoordinatorOptions  [param: config]`; JSON entries for
  sites carry `site`, `line` and `owner`, while entries for a
  symbol's own signature are unchanged. `--no-tests` drops sites by
  path, `workset --type-impact` counts them and bundles their owners.
  On cline, `unused --kinds types` drops from 557 to 459 rows and
  `query type AccountContext`/`SessionConfig` find their sites; on
  claude-code, 387 to 364. Symbol, edge, reference and heritage sets
  are byte-identical on every evaluation repo; the map gains ~7,500
  records (+1.8 MB) on cline and none on non-TS repos. Struct/class
  fields, generic arguments and JSX remain the documented gap.

## [1.1.1] — 2026-09-24

### Fixed
- **`affected`/`impacted_tests`/`workset` no longer list test-support
  files as impacted tests.** A file under a `testing/` directory
  (mocks, matchers, test-case generators, a tool that only exists in
  a test build) is test code, and stays hidden by `--no-tests` and out
  of `unused`, but no test runner discovers tests under that
  directory name, so it was never something to run. The path
  classifier now has two levels: `classify.is_test_path` (test code;
  its answer is unchanged on every mapped path of the seven evaluation
  repos) and the new `classify.is_test_file` (test directories and
  test filename patterns only). The impacted-tests walk reports the
  narrow one and still passes through support code to the tests
  beyond it. On claude-code, a repo with zero test files, `workset
  --symbol MCPServerConnection --type-impact` reported
  `src/tools/testing/TestingPermissionTool.tsx` as an impacted test;
  it now reports none. tensorflow has 226 such files under
  `lite/testing/` and its siblings. zed's `affected HEAD~1` still
  names the same 46 tests.

## [1.1.0] — 2026-09-24

Closes round 1.1's fix cycle. The code is 1.0.5; this release is the
version line catching up with the round, per `CONTRIBUTING.md`'s
"Testing rounds and the version line". Round 1.1 (the first
evaluation of the 1.x series, seven real repositories against
0.43.77) was the cleanest on record: zero new High, Medium, or
Critical findings, and every round 33 fix held. Its five Low findings
became five fix tracks; four shipped and one measured out:

- **1.0.2** — daemon-routed `diff`/`affected`/`workset` print the
  cold rev-cache note once, not twice.
- **1.0.3** — `outline`'s savings line says `partial:` when the
  outline was cut, so a truncated ratio can't be quoted as the whole
  file's (the README's zed row was; it's fixed).
- **1.0.4** — MCP `get_callers`/`get_subtypes` say how many test rows
  their default hid, and nothing when it hid none.
- **1.0.5** — a bare `workset`/`affected`/`diff` on a clean tree no
  longer exports and re-parses the commit it's sitting on: tensorflow
  265 s -> 10 s, cline 6.7 s -> 1.0 s.
- **Search centrality** (the round's one Low-Medium): measured against
  an 11-query known-answer set (`benchmarks/search_known_answers.py`)
  and left as is. Scaling the connectivity bonus by relevance changed
  no top-1 result and cost the `isEnvTruthy` hub its place, so the
  benchmark shipped and the change didn't.

1.0.1, a source cleanup, rode along ahead of the tracks. Round 1.2
(dekko 1.0.5, 2026-09-23) has since run: no regressions on any of
the seven repositories, map counts byte-identical to 0.43.77. Its
findings open the 1.1.x line.

## [1.0.5] — 2026-09-23

### Performance
- **A bare `workset`/`affected`/`diff` on a clean tree no longer
  rebuilds the commit it's already sitting on.** With no rev given,
  these compare against the map's own commit, usually `HEAD`. The
  first call after a new commit exported that commit with `git
  archive`, re-parsed and re-resolved all of it, and cached the
  result, only to compare it against an identical working tree and
  report nothing changed. Now, when the target rev is `HEAD`, the tree
  has no changes or untracked files outside `.dekko/`, and the map is
  fresh, both sides come from the current map and nothing is exported.
  Cold bare `workset` on tensorflow: 265 s -> 10 s; cline: 6.7 s ->
  1.0 s. No rev-cache entry is written on that path, and a routed
  call no longer prints the "no rev-cache ... may take a while" note
  for it. Any change, untracked file, older rev, or stale map takes
  the full path exactly as before. Before shipping, a cold clean-tree
  `diff HEAD` on the full path was confirmed empty on all seven
  evaluation repos, so the shortcut returns what the full path did.

## [1.0.4] — 2026-09-23

### Fixed
- **MCP `get_callers`/`get_subtypes` say how many test rows they
  hid.** Both tools leave out test files by default (the CLI's
  `query callers` includes them), and the reply always ended in the
  same "test-file callers excluded" note, whether that hid fifty
  callers or none. An agent couldn't tell "nothing to see" from "go
  look". The note now gives the count, plus the call sites when they
  differ (`note: 1 test-file caller (21 call sites) excluded ...`,
  a test file calling the target from its top level), and is left out
  when nothing was hidden. `get_subtypes`' count follows
  `transitive`/`relation`. Neither default changed.

### Documentation
- `query --no-tests` help, `docs/cli.md` and `docs/claude-code.md` now
  name the other interface's test default, so the CLI/MCP difference
  is findable from either side.

## [1.0.3] — 2026-09-23

### Fixed
- **`outline`'s savings line says when the outline was cut.** A
  budget- or limit-trimmed outline printed the same `full ≈ N tok ·
  outline ≈ M tok (P%)` line as a complete one, with the truncation
  only in the footer below it, so the ratio was easy to quote as the
  whole outline's. The line now ends in `partial: K of N symbols;
  complete outline ≈ T tok (P%)` when anything was omitted
  (directory outlines count `rows`). This shows up by default on the
  MCP `outline` tool, whose 2000-token budget trims large files.
  `--json` gains `outline_tokens_complete` and `complete` per file.
- **README savings table:** the zed `crates/git_ui/src` directory
  outline row said ~80x, measured on the default 200-of-1,574-row
  view. The complete outline is ~35,778 tokens, ~11.7x.

## [1.0.2] — 2026-09-23

### Fixed
- **Daemon-routed `diff`/`affected`/`workset` no longer print the
  cold rev-cache note twice.** The client prints it before dispatch
  so you see it before the wait, and the daemon's replayed stderr
  carried the same line again. The client now drops that one replayed
  copy. Works against an already-running older daemon too; direct
  mode is unchanged.

## [1.0.1] — 2026-09-23

Source cleanup. The code no longer points at internal evaluation
reports, fix plans, or design documents, none of which ship with
dekko. No behavior changes.

### Changed
- **Comments and docstrings say why, not where it came from.**
  Citations of internal evaluation rounds, fix-plan items, report
  sections, and design-spec labels (e.g. `FR1`, `NFR2`) are gone from
  `src/`, `tests/`, and `scripts/`. The reasoning they carried stays.
  Where a label named a concept, the concept's plain name replaces it.
- **CLI help text** for `--scorer`, `--relation`, and dense output no
  longer mentions internal round or phase labels.
- **`dekko-verify` skill and `docs/`** no longer reference
  maintainer-local files that don't exist in a clone.
- **Test names** carry what they test rather than which round found
  it (`tests/test_round33_small_fixes.py` is now
  `tests/test_small_output_fixes.py`, among a few function renames).

### Fixed
- **`RawHeritage.relation` docstring** said Rust `impl` edges weren't
  produced by any extractor; they are. It now says only Go `embeds`
  is not yet produced.

## [1.0.0] — 2026-09-22

dekko 1.0. This is a milestone promotion, not a rewrite: the code is
0.43.79 plus the benchmark refresh below. It marks the point where the
surfaces agents and scripts depend on are stable enough to promise
semver on, after 34 evaluation rounds against seven real repositories
(awesome-go, claude-buddy, claude-code, cline, spring-boot, tensorflow,
zed) and a final round (34, dekko 0.43.77) that found zero new High,
Medium, or Critical issues across all of them.

### Breaking

Nothing breaks in this release. This section states what 1.0 commits
to, so that a future MAJOR bump has a defined meaning:

- **The CLI surface is stable.** Every subcommand and flag documented in
  `docs/cli.md` keeps its name, argument shape, exit codes, and `--json`
  output keys. Removing or renaming one is a MAJOR change. New
  subcommands and flags are MINOR/PATCH as before. The legacy
  flag-form aliases (`dekko --map`, `dekko --claude-install`, ...) stay
  supported.
- **The MCP surface is stable.** The 18 tools `dekko serve --mcp`
  exposes keep their names, required arguments, and argument names
  (including the `name` alias for `symbol`). Removing a tool or
  renaming an argument is a MAJOR change; new optional arguments and
  new tools are not.
- **`map.json` is versioned and backward-readable.** The document
  carries `"version"` (currently 11). A dekko 1.x reads every 1.x map;
  a map written by a newer dekko than the reader is refused with a
  clear "restart or upgrade" error, never misread. A schema change that
  a 1.x reader cannot load is a MAJOR change.
- **`.dekko/notes.json`** keeps its symbol-id keyed shape, so committed
  notes survive upgrades.
- **Python 3.10 is the floor** for the 1.x line; dropping it is MAJOR.
- **Hook, plugin, and Cline installers stay idempotent and reversible.**
  `install` then `uninstall` restores the edited file.

Outside those promises: the exact text of human-readable output
(row wording, footers, notes) may still change in PATCH releases, and
the resolver's precision keeps improving, which means caller counts
and `unused` verdicts can shift between versions as more code becomes
resolvable. Use `--json` and the documented keys for anything a script
depends on.

### Fixed
- **`benchmarks/measure.py` ran again.** The harness crashed on its
  `workset` task (`workset._render_text` gained a `root` parameter the
  harness never passed), and its two `outline` targets still named
  `src/dekko/cli.py` and `src/dekko/render_lean.py`, which moved into
  subpackages in 0.31.1. A target that is not a file now reports
  `unresolved` instead of a `0 → N` row, and `tests/test_benchmark.py`
  exercises the `workset` measurer so the drift cannot recur silently.

### Changed
- **Benchmark docs refreshed to the latest evaluation rounds.**
  `benchmarks/README.md`'s representative output is from 0.43.79 on
  the current 170-file / 4,283-symbol source (it showed an 88-file
  snapshot). `benchmarks/real-world-repos/README.md` gained a "Current
  numbers (dekko 0.43.77)" section with the round 33/34 (2026-09-21/22)
  cross-repo table and current repo sizes; the 2026-08-03 study on a
  0.20-era dekko is kept below it as the methodology and analysis, with
  a dated provenance note on each per-repo write-up, and its
  correctness-caveats section now records that every caveat it raised
  was fixed and re-verified (round 34: zero new High/Medium/Critical
  findings). The root README's headline moved from "3x-200x" to
  "10x-300x" with the ~2.5x floor stated, its table uses round-34 rows,
  and the cold-map timing was re-measured (about 5 s for ~4,300 symbols
  on all cores, not 1.8 s for ~3,500).
- **`CLAUDE.md` and `CONTRIBUTING.md` now require regenerating dekko's
  own map before every commit** and committing the refreshed `.dekko/`
  files with the change. The `dekko-map` pre-commit hook regenerates a
  stale map but does not stage it, so the rule is explicit.

## [0.43.79] — 2026-09-22

Review pass over `src/dekko/integrations/` (cli, server, hooks,
doctor, cline, orient, claude_md): stale references, wrong schema
text, and a few cheap wins. No new commands or tools.

### Fixed
- **`pre-read` hook actually reaches the model.** It emitted
  `permissionDecision: "defer"` with the advisory in
  `permissionDecisionReason`. In Claude Code's hook contract `defer`
  is not an advisory: it hands the decision to an Agent SDK wrapper
  in `-p` mode, and the reason text of a non-`ask` decision is never
  shown to the model, so the "outline this first" nudge was invisible
  in interactive sessions. It now emits `additionalContext` with no
  `permissionDecision` (the documented non-blocking channel), so the
  Read proceeds normally and the nudge lands in context.
- **`pre-bash` sees combined short flags.** `grep -rli x .`, `grep
  -nR x src`, `grep -rnE ...` never matched (only six spelled-out
  tokens did); eval transcripts show those forms in live use. Flags
  are now read letter by letter. In the other direction, `rg pattern
  src/one_file.py` (every path an existing file) no longer pauses for
  confirmation: that is a targeted read, and a false interruption is
  the worst outcome for a hook designed to favor false negatives.
- **`session-start` hook respects the search-availability guard.** It
  used the raw `_PREAMBLE` constant and so always recommended
  `search`, bypassing `orient._preamble()`'s drop-the-line-when-
  unusable check that `dekko orient` already applied.
- **MCP schema text was wrong in two places.** `impacted_tests`
  advertised a default budget of 800 (it is 6000, `affected.
  DEFAULT_BUDGET`); `get_supertypes` advertised `include_tests` as
  "default: false, test-file callers are noise" when its default is
  true and it has no callers. `get_subtypes` now also appends the
  same "test-file subtypes excluded by default" note `get_callers`
  does, and both heritage tools honor the shared budget/limit
  precedence (`_limit_arg`) instead of a fixed 50-row cap.
- **`add_note` on an ambiguous target lists the candidates.** It said
  `'X' is ambiguous (3)` and stopped; it now returns the same
  candidate rows (with the `:LINE` form that picks one) or
  closest-match list the CLI prints, so the agent can retry without a
  second lookup.
- **`dekko sanity --all` error text** pointed at "the design doc's
  Scope section", a gitignored `.features/` file no user has.
- **`dekko --claude-install` / `--claude-uninstall`** said the restart
  activates or drops `/map`; the plugin also ships `/doctor`,
  `/sanity`, the MCP tools, and five skills.
- `orient.py`'s module docstring pointed at a README "Proactive
  orientation" section that no longer exists (now `docs/claude-code.md`
  "Push hooks"); `server.py`'s tool-list comment claimed the five
  unregistered handlers "remain callable" (they are reachable only
  from tests, not from an MCP client).

### Changed
- **`map_status` reads the provenance sidecar, not `map.json`.** The
  tool exists to give the staleness fact *without* paying for a
  regen, yet it parsed the whole map (hundreds of MB on a large repo)
  to answer it. It now takes the same cheap path `dekko status` and
  `doctor` do, falling back to the full parse only for a pre-sidecar
  map. `mapfile.load_provenance` gained an opt-in `check_version`
  flag so that fallback still raises the too-new / malformed
  `map.json` errors the tool's call path translates into restart or
  regenerate instructions.
- **CLAUDE.md usage-policy block** (`dekko --claude-md-install`) gained
  two lines: pick tests with `impacted_tests`/`dekko affected`, and
  keep using the tools after your own edits (every tool regenerates a
  stale map itself; never run `dekko map` by hand mid-task). Re-run
  the install to pick up the new wording.
- `doctor` reuses `selfcheck.loaded_version()` instead of a private
  `importlib.metadata` copy, and drops an unused `_STATUSES` constant;
  `diff`/`affected`/`workset` handlers' `getattr` fallback for `jobs`
  now matches the parser default (0, all cores) instead of the
  pre-round-31 sequential 1.

## [0.43.78] — 2026-09-22

Claude Code plugin refresh: stale references fixed and the skills
reinforced so Claude reaches for the map more often. No CLI or MCP
behavior change.

### Changed
- **Claude Code plugin refresh** (`integrations/claude/`). Stale
  references fixed: the `dekko-orient` skill called bare `dekko
  summary` unbounded (it has defaulted to `--budget 5000` for a while;
  only the raw `dekko://summary` MCP resource is uncapped by design);
  `dekko-verify` said `get_callees` hides test callers by default
  (only `get_callers` does); `dekko-daemon`'s routed-command list was
  missing `ambiguous`, `deps`, and `ledger`; `dekko-review-context`
  said "`dekko` equivalent" where it meant `dekko ambiguous`;
  `plugin.json`/`marketplace.json` still described the plugin as "a
  /map command" though it ships `/map`, `/doctor`, `/sanity`, an
  18-tool MCP server, and five skills; `/sanity`'s argument hint
  omitted `--unused NAME`.
- **`dekko-orient` skill reinforced** so Claude reaches for the map
  more often and more cheaply: a five-rung default ladder (orient,
  locate, shape, relate, read only the lines to edit); an explicit
  "edits never stale you out, don't run `dekko map` by hand" rule; a
  table of Read/Grep impulses and the dekko call that replaces each;
  a "get more out of each call" section (`budget`, `sites`,
  `with_source`, `task`, `include_tests`, `hops`, `:LINE`
  disambiguation); rows for the four MCP tools the skill never
  mentioned (`find_type_usages`, `get_supertypes`/`get_subtypes`,
  `check_ambiguous`) and for `dekko diff`/`dekko ambiguous`; both MCP
  tool-name prefixes (`mcp__dekko__*`, `mcp__plugin_dekko_dekko__*`).
  `dekko-verify` now leads with `dekko sanity`/`/sanity` and keeps
  the hand grep as the fallback for the heritage/throws cases
  `sanity` doesn't cover. `dekko-review-context` gained `task=` and
  `type_impact` guidance. `TESTING-GUIDE.md` §5a gained verification
  bullets for `dekko-orient` and `dekko-verify`.

## [0.43.77] — 2026-09-21

Round 33 Track 6: seven small fixes from the round's Low tail, one
version. Design: `.features/fixes/round33/06-small-fixes.md`.

### Fixed
- **`deps --file` on a Go file explains its zeros** (awesome-go). The
  bare summary has carried the language-scope note since round 29;
  `--file` printed `imports (0):` / `imported by (0):` and listed the
  repo's own packages under `external` with no explanation. Now a
  `note:` on stderr (`import_scope_note` in JSON), gated on the file's
  own language, saying that `imported by` is always empty there.
- **`daemon status` prints `busy: no`, not `busy: False`** (claude-buddy).
- **`unused --dispatch` / `--suspect` sections say what they dropped**
  (cline). Each section has always had a flat 20-row cap so it can't
  steal budget from the main list, but it printed those 20 in silence
  under a header saying 258, and ignored `--limit` and `--budget`
  alike (the report said `--budget` capped it; it didn't). Now: a
  `(N of M omitted · raise --limit)` footer, an explicit lower
  `--limit` binds, `--budget` applies to the section independently,
  and JSON carries `dispatch_meta`/`suspects_meta` totals.
- **"with all cores" only when it is** (tensorflow). The cold-rev-cache
  note took a bool that read every `--jobs N > 1` as all cores; it now
  takes the worker count and says `with 4 workers (of 11 cores)` or
  `with all 11 cores`.
- **External heritage rows keep `extends`/`implements`** (spring-boot).
  The parser always knew it and resolved edges kept it; the three
  external exits in `_resolve_one_heritage` dropped it. `ExternalCall`
  gains an optional `relation`, written on `heritage_external` rows
  only (the `external`/`throws_external` sections are byte-identical;
  no `MAP_DOC_VERSION` bump). `(external) Ordered  [implements]` after
  the next regen; a pre-0.43.77 map renders as before.
- **MCP error replies carry the default-root line** (spring-boot). The
  report said omitting `root` was silent; it wasn't on success replies
  (that line has been there since bug #1/B1). It *was* silent on
  errors, and a wrong-repo query's likeliest outcome is a not-found
  error with plausible closest-matches from the wrong repo.

### Changed
- **`dekko[all]` now includes `orjson`.** The extra named `all` held
  only the grammar pack, so the eval tool (installed as `dekko[all]`)
  ran every `map.json` load and write on stdlib `json` through every
  round to date: measured 2.0-2.6x slower parse and 6.0-6.8x slower
  serialize on the real maps. `tokenizer` and `search` stay separate
  on purpose (each changes what a command's numbers mean). `dekko
  doctor` gains a `json-backend` row so the state is visible instead
  of inferable from a profile. Reinstall with `[all]` to pick it up.

## [0.43.76] — 2026-09-21

Round 33 Track 5. Design and measurements:
`.features/fixes/round33/05-sanity-colon-template-non-type-target.md`.

### Fixed
- **`sanity` no longer calls a function's name after a colon a "type
  position".** The `x: Name` template matches `identifier: identifier`,
  which in TS is a type annotation, an object-literal value, a ternary
  else-branch or a `case` label; a regex can't tell them apart but the
  target's kind can, and the caller already computed `target_is_type`
  without the colon check consulting it. `error: errorMessage,`
  (claude-code `ide.ts:617`, a function target) read "type position".
  The colon shape now applies to type targets only; `Foo<Name>` and
  `import type` stay ungated. Heavy-tailed: 0 of 659 rows on a random
  150-symbol sample were affected, 145 of 186 on the 100 most-flagged
  names (`action`, `count`, `errorMessage`). Those 145 now fall to
  "unexplained", on purpose: a wrong explanation closes an
  investigation that should stay open.

### Added
- **A same-named local is explained.** A value-position use of a local
  declared earlier in the enclosing function (`const errorMessage =
  ...` four lines above `error: errorMessage,`) reads `use of a
  same-named local declared earlier in the enclosing function`, with
  the declaration line on the row (`(declared at line N)` in text,
  `decl_line` in JSON). A post-pass that only upgrades "unexplained"
  rows, under four guards: never on a call-shaped line (a call dekko
  has no edge for is what `sanity` exists to surface), never when the
  declaration is the target's own definition, never when it is
  indented deeper than the use, JS/TS only. Parameters are index-backed
  (`Symbol.params`), no regex. Measured on claude-code's 100
  most-flagged non-type targets: unexplained 3,918 → 3,301, 733 rows
  explained, led by `errorMessage`, `count`, `action`; 50 read by hand,
  0 real references to the target. cline `--all`: 343 rows. The
  round-32 note that `REPL.tsx:1624` "correctly" lands in unexplained
  is retired: it reads as a local declared at 1623.

## [0.43.75] — 2026-09-21

Round 33 Track 3. Design and measurements:
`.features/fixes/round33/03-uses-receiver-and-module-match.md`.

### Fixed
- **`query uses chalk` said "no external reference matches" while 283
  `chalk.*` call sites sat in the map.** Reported (claude-code.md
  Finding 1, as HIGH) as `chalk.<method>()` calls being "simply absent
  from the call graph", with the fix pointed at the extractor. They
  were never absent: `externals_by_name` keys on the *last* callee
  segment (which is what `find_usages`'s schema documented), so every
  `chalk.red(...)` was filed under `red`. Not TS-specific either:
  `uses subprocess` failed the same way on dekko's own repo. Read-side
  fix, no spec bump, no edge movement. `uses` now matches three ways
  and labels each row: `base` (the old behavior, unchanged), `binding`
  (first segment is an import binding in the calling file: `chalk`,
  `React`, `np`, `subprocess`), and `module` (the bare import source:
  `numpy`, `fs`, `node:path`, reaching bare named imports too). The
  binding match is import-gated on purpose: ungated, `uses path` on
  claude-code returned 48 parameters named `path` (42% noise). Now 0
  of 50 sampled binding rows on cline sit under a local rebind.
- **Multi-line chains were invisible to any head match.** Chains are
  whitespace-normalized to `z .object` before storage, so 979 of
  claude-code's `z.*` externals (39%) had a head of `"z "`. Segments
  are stripped (`mapfile.callee_segments`).
- **The not-found message stopped claiming absence it couldn't prove.**
  A name that appears as a receiver but is never imported (`uses
  result`: 356 sites on claude-code) now says so: "appears as a
  receiver in N call sites, but never as an import binding; those are
  local variables, not a module". Closest-name suggestions draw from
  bases, heads and bare import sources, so `uses Chalk` suggests
  `chalk`.

### Added
- A summary header before the rows: `chalk: 284 call sites in 44
  files`, `top members: dim 73, bold 67, red 50, ...`, and `imported by
  47 files; type-position, JSX, and property reads are not recorded
  (calls only)`. That last line is the honest denominator: 520
  claude-code files import `React` and 212 call sites are recorded,
  because `React.FC` in type position and JSX never reach the call
  bucket. `--json` carries the same numbers under `summary` and a
  per-row `match`.
- `MapIndex.externals_by_head`, built lazily on first `uses` call
  (189 ms on zed's 197K externals; `query symbol` timing unchanged).
- MCP `find_usages`'s description and `name` schema text describe the
  three shapes.

## [0.43.74] — 2026-09-21

Round 33 Track 4, found during Tracks 2 and 3 rather than by any eval
agent. Design: `.features/fixes/round33/04-rows-that-outrun-the-budget.md`.

### Fixed
- **One row could be 122,327 characters, and the budget couldn't cut
  it.** `fit_to_budget` drops whole rows and always keeps one, which is
  right, but rests on rows being small. An external callee text is the
  whole receiver expression, nested function bodies included, so
  claude-code's commander builder (`program.name(...)...version`) made
  `query uses version` print ~30.5K tokens against an 800-token
  default, with a footer that didn't say anything was wrong. `uses`,
  `throws` and `supertypes` labels are now elided in the middle at 120
  characters (`head…[+122,009 chars]…tail`, receiver and method both
  kept); `--json` marks such entries `callee_truncated`. That row is
  now 44 tokens. Clipping happens at render, after lookup, so a row
  still matches by its base name.
- **The footer now admits an overrun.** When the kept output exceeds
  `--budget` anyway (only possible when the first row alone is bigger
  than the budget), the footer says `over --budget N: first row alone
  exceeds it` and `meta.over_budget` is true. It should never fire now;
  it exists so the next producer that breaks the small-rows assumption
  shows up in a terminal instead of an eval round.
- **Generated `map/` pages cap inline link lists at 25.** A `called by`
  line was one link per caller with no cap: claude-code's
  `logForDebugging` made a 102,113-character line, and 191 lines over
  2,000 characters were 13% of that repo's pages. Now `+975 more
  (\`dekko query callers <symbol>\`)`. claude-code's pages: 8.5 MB to
  7.4 MB, longest line 102K to 3.3K.

### Added
- `textutil.clip_middle`, `ROW_CHAR_CAP`/`LABEL_CHAR_CAP`, and
  `tests/test_row_size.py`: a parametrized test that runs 13 read
  commands over a fixture seeded with a 5,000-character chain and a
  40-caller symbol and asserts no printed line exceeds the cap (long
  signatures are the one named exemption).

## [0.43.73] — 2026-09-21

Round 33 Track 2. Design and measurements:
`.features/fixes/round33/02-deps-cycles-fabricated-path.md`.

### Fixed
- **`deps --cycles` no longer draws an import path that doesn't
  exist.** `find_cycles` returns each strongly-connected cluster as its
  members *sorted*; the renderer joined that list with `->` arrows, so
  an alphabetical listing read as an import chain. Audited on the eval
  repos: 0 of 4 printed arrows were real on claude-buddy, 159 of 1,175
  on claude-code, 226 of 433 on zed, and a chain was only ever right for
  a two-file cluster. An agent asking "which import do I cut" was
  pointed at edges that weren't there. A cluster now prints its members
  comma-separated, one `shortest loop:` chain (BFS, every arrow a
  verified direct import; 0.1 ms on claude-code's 1,156-file cluster),
  and its internal edges when there are at most 12, otherwise a count
  and the number of two-file loops inside it. After: 360 of 360 arrows
  real on zed, 105/105 cline, 28/28 claude-code, 7/7 claude-buddy.
- **The 1,156-file cluster was one 44,914-character row.** `--budget
  500` printed ~11,500 tokens because the budget can't cut a row it
  must keep (Track 4's general case). Members clip at 12 with `+N
  more`; the default output on claude-code went from ~11,500 tokens to
  ~740 and `--budget 500` now binds at ~460.
- The summary line says `N circular-import cluster(s)` instead of
  `N cycles`, since a cluster is usually several overlapping loops.

### Added
- `--cycles --json` entries carry `internal_edges`, `shortest_loop`
  (import order, first file not repeated), `two_file_loops`, and
  `edges` (only when at most 12). `files`/`self_import` unchanged.

## [0.43.72] — 2026-09-21

Round 33 Track 1. Design, measurements, and the real-process A/B:
`.features/fixes/round33/01-stale-process-map-overwrite.md`.

### Fixed
- **An outdated long-lived process no longer rewrites the map.** A
  `dekko serve --mcp` or daemon process keeps the code it imported;
  when dekko was upgraded underneath it, that process and a fresh CLI
  each read the other's `map.json` as stale (a spec-hash mismatch says
  "different", never "older") and regenerated it with their own
  extractor, alternately, forever: every flip a cold remap that also
  threw away the other side's caches (~3 minutes per flip on
  tensorflow). Found live: three servers started before the day's
  reinstall rewrote maps on all seven eval repos and dekko's own
  tracked map. A long-lived process now asks the on-disk code who is
  outdated (a ~30 ms child interpreter, memoized on the install's stat
  signature, consulted only on a mismatch). If it is the outdated
  party it serves the on-disk map untouched, judges freshness on
  source content alone, hands any regeneration to the installed dekko
  (`repo_ops._delegated_regen`) instead of extracting in-process, and
  appends `note: this dekko server is running outdated code ...
  restart` to every reply until restarted. A process that can't prove
  it's current never writes. A one-shot CLI process pays nothing and
  behaves exactly as before.
- **`tool_version` was a live read.** `importlib.metadata.version`
  reads dist-info from disk at call time, so an outdated server
  reported, and stamped into provenance, whichever version was
  installed *now*. That is why every round-33 staleness message read
  "same version string 0.43.71 on both sides" and why the
  `tool_version` signal could never fire for the one kind of process
  it existed to catch. The loaded version is now frozen at import
  (`dekko.selfcheck`) and used for provenance, cache stamps, MCP
  `serverInfo`, and freshness checks.
- **A held index now notices `map.json` was replaced.** The MCP
  server's and daemon's caches validated a cached index with
  `check_freshness` alone, which compares the *cached copy's*
  provenance to the source tree. A newer dekko rebuilding the map with
  no source change was invisible, so an outdated server kept answering
  from its old extractor's index. `mapfile.index_matches_disk` (one
  `stat` of `map.json`) closes it.
- **The `spec_hash`-only staleness message stopped blaming the reader.**
  It told every process "this is a long-lived process running older
  code; restart it", including a fresh CLI looking at a map an outdated
  server had just rewritten, which had two eval reports contradicting
  each other about which spec was current. It now says "restart it"
  only when the process is proven outdated, and otherwise that the map
  was written by a different dekko build. MCP `map_status`'s advice
  splits the same way: an outdated server says restart; a current one
  looking at an old map says regenerate. `refresh_map`'s round-23
  "rebuilt with stale code" caveat is gone because the case is.
- **`dekko doctor` names outdated servers.** `mcp-server-running` was
  always `unknown`. It now compares each server's start time against
  the installed code's mtime: `stale` naming the pids that predate the
  install, `ok` otherwise, `unknown` only when `ps` can't say.

### Added
- `dekko.selfcheck`: loaded vs. installed identity, the three-way
  verdict (`classify`), and the outdated-process note.
- `daemon status` reports `outdated` (JSON) / `outdated: yes` (text)
  for a daemon whose dekko was upgraded underneath it, and routed
  commands carry the note on stderr.
- `mapfile.Freshness.process_outdated`, `MapIndex.map_stat`,
  `mapfile.index_matches_disk`.

## [0.43.71] — 2026-09-21

Round 32 Track 4, the last open item of the round. Design and
measurements: `.features/fixes/round31/04-type-constructor-arity.md`.

### Fixed
- **`Name(x)` constructs a Rust tuple struct.** Every type symbol
  carried `params=[]`, because no signature was read off a struct
  definition, and the resolver's arity check reads `[]` as "takes zero
  arguments". So a counted `GroupName(s)` was rejected against
  `struct GroupName(String);` and filed external whenever it reached
  the sole-candidate rung, which is every construction through a glob
  import (`use super::*`, the norm in Rust test modules). A tuple
  struct's positional fields are now its params. Brace and unit
  structs keep none, correctly: `Brace(1)` against `struct Brace { a:
  u8 }` is still rejected, now for a real reason. zed: **+424 call
  edges**, every one a struct target with `Name(` written on the
  caller's line (`MultiBufferOffset` 78, `DevicePixels` 39,
  `MultiBufferRow` 35).
- **A struct that shares its name with an enum's tuple variant is not
  taken on name alone.** dekko indexes enums, not their variants, so
  after `use AutoCompactThreshold::*`, `Percentage(0.9)` had exactly
  one in-repo candidate, gpui's unrelated `struct Percentage`, and
  took it with full confidence. Tuple variants are now recorded as a
  name registry (not symbols: nothing in `map.json`, `unused`,
  `search` or `outline` changes) and the sole-candidate rung stands
  down on a collision. This is why the obvious fix ("ignore arity for
  types") was measured and backed out in 0.43.67: 252 of its 552 new
  edges were this collision. zed: **21 edges removed, all 21 read, all
  21 wrong** (`End(MarkdownTagEnd::..)` on `sum_tree`'s `struct End`
  10 times, `Identifier("a")`, `Text(..)`, `Column(..)`).
- **A Rust dot-call never constructs a type.** `list.CommitList()`
  (Windows COM) no longer resolves to `struct CommitList`. Same rule
  as round 31's F11 (`recv.name()` can't reach a free function), one
  more target kind.

### Changed
- Calls recovered from `assert_eq!(..)` bodies carry their real
  argument count for a bare `Name(..)` again. 0.43.67 withheld it to
  dodge the bug above. Zero edges moved on zed.
- `map.json`: a Rust tuple struct's `params` is no longer empty.
  `MAP.md` signatures are unchanged (`struct X`).
- cline and spring-boot: every edge section identical to 0.43.70.

### Not fixed
- A bare `Left(x)` whose *only* reading is a variant still goes
  external, as before. Resolving it to the owning enum is possible
  (the registry knows the owner) and deliberately not built: it is a
  new edge class that needs its own measurement, and any enum with an
  `Ok`/`Some`/`Err` variant would swallow the prelude's. A missing
  edge, never a wrong one.

## [0.43.70] — 2026-09-21

Round 32 Track 5b, the half of Track 5 the visibility veto can't see.
Design, measurements and the probe that produced them:
`.features/fixes/round31/05b-scope-aware-references.md`.

0.43.69 stopped reference edges between files that can't see each
other. What it could not stop: a local that shadows a symbol in its
*own* file, a local that shadows a name the file really *does* import
(claude-code `utils/ide.ts` imports `errorMessage`, then binds `const
errorMessage` in a catch block), and any local at all in a file with
zero imports, which the veto has to exempt. Measured on 0.43.69:
**6.2% of claude-code's reference sites and 4.1% of cline's were still
locals**, two thirds of them in zero-import files.
`src/utils/bash/bashParser.ts` imports nothing, so its loop character
`c` pointed at `src/buddy/types.ts::c` 387 times.

### Fixed
- **References know what they are bound to.** The extractor now walks
  scopes for Python and JS/TS/TSX and tags each bare-identifier
  reference (`RawRef.bound`: a parameter, a local, or nothing).
  Parameters of every shape, `const`/`let` (block), `var` (function),
  destructuring to any depth, catch bindings, `for..of` heads, Python
  assignment targets, `with`/`except .. as`, walrus, comprehension
  variables, `global`/`nonlocal`. A reference bound to a plain local
  never reaches the resolver's ladder. Three kinds of binding are
  deliberately *not* locals: a module-level name, a nested definition
  the map indexes as a symbol (`const helper = () => ..` inside a
  function), and a function-local lazy import (`const { X } = await
  import("./x")`, about 40 true edges on claude-code that a naive
  scope walk would have cut). Everything fails open: a binding shape
  the table doesn't know leaves the reference untagged, which is the
  old behavior, so this can leave a false edge standing and never
  remove a true one. Reference edges: claude-buddy 695 -> 684,
  claude-code 10,437 -> 10,121, cline 9,517 -> 9,191, **zero edges
  added, zero call edges changed**. Every lost edge in the two risky
  classes (imported-then-shadowed: 30 + 12; same-file function
  targets) was read against source. All false.
- **A pytest fixture parameter is the fixture.** `def
  test_x(short_root): run(short_root)` shadows the `short_root`
  fixture lexically and *is* that fixture semantically, because pytest
  injects by parameter name. 102 of the 119 lexically shadowed
  reference sites in dekko's own repo are this shape, so the tag is a
  tag and not a filter: the resolver keeps the edge for a bound
  *parameter* in a test file whose name matches a decorated function
  in the same file, or in the `conftest.py` of the nearest ancestor
  directory.
- **Regression from 0.43.69: conftest fixtures lost every reference
  edge.** The visibility veto requires an import for a cross-file
  Python edge, and a conftest fixture is the one cross-file name
  Python sees without one. dekko's own repo lost all 47 reference
  edges from tests into `tests/conftest.py`. Call edges survived, so
  `affected` mostly still worked, but a fixture passed along and never
  called in the test was invisible. The fixture rule above restores
  all 47.
- **A file that exports is a module, not a script.** The veto's
  zero-import exemption exists for script-style files that share one
  global scope. A file with no imports *but an exported symbol* is an
  ES module and shares nothing (this is also exactly how TypeScript
  reads such a file), so a free `process` or `performance` in it is
  the runtime global. 21 edges on claude-code and 20 on cline, every
  one `process`, `performance` or `String` pointing at some file's
  same-named function. Files with neither import nor export keep the
  exemption.

### Changed
- `dekko unused` flags 23 more symbols on claude-code and 8 on cline.
  None of them has a single unbound use of its name anywhere in its
  own file (checked by parsing each one). They are object-literal
  methods on tool definitions (`description()`, `prompt()`, `get
  state()`) that were being spared by a same-named local elsewhere in
  the file. 67 of claude-code's 80 such tool methods were already
  flagged; now 77 are, which is at least consistent. That these are
  called through a property and never seen as used is a separate,
  older limit of `unused` on object-literal methods.
- `sanity`'s textual "does this file bind a local of this name" guard
  (`_file_shadows_name`, 0.43.68) is gone. It had to switch the
  "dekko has this as a reference" label off for a whole file, because
  no regex can tell which scope a line sits in. The map can now, so a
  true reference next to a shadowing local gets its label back.
  `_can_see` stays, for maps built by an older dekko.
- Extraction cost: not measurable. `map --full --jobs 0` on
  claude-code 4.3s -> 4.2s, cline 5.8s -> 5.7s, tensorflow (the Python
  walk at scale) 175.5s / 172.4s -> 172.3s / 174.4s, alternating runs
  on an otherwise idle machine, all inside noise. Bindings are found by one more compiled query per file, and
  a reference only pays for a scope lookup when its name is bound
  somewhere in the file.

### Not fixed
- Post-fix probe: 0 false sites on claude-buddy, 1 on claude-code, 17
  on cline (0.1%), all explained. 8 are `process` in true scripts
  (no import, no export: the exemption working as designed). 8 are a
  bare identifier resolving to a same-named **class member**
  (`invoke` from a lazily imported external package landing on
  `DesktopClient.invoke`), which a bare identifier can never name. A
  three-line veto would take them; the design set a bar of 10 edges
  for adding it and this is 8, so it is recorded here instead. 2 are
  module-level destructures that aren't symbols.
- `def f(text=text)`: the default value is read in the enclosing
  scope but is tagged as the parameter. Same for JS default values.
- `table[key]` index reads, still out by the round-23 decision.

## [0.43.69] — 2026-09-21

Round 32 Track 5, found while building Track 2, not by the sweep.
Design and measurements:
`.features/fixes/round31/05-reference-edges-shadowed-locals.md`.

Value-reference edges (what `query uses`, MCP `find_usages` and
`unused` read) were bound by name alone. The ladder references share
with calls ends in "it's the only symbol with that name, take it".
For a call that is a fair guess: `count(x)` on a local is rare. For a
bare identifier it is not: `const count = ...; if (count >= 3)` is
every other line, and each one became a reference to whichever
unrelated `count` the repo defined once. **35% of claude-code's
reference edges and 57% of cline's joined files that cannot see each
other.** `uses error` on claude-code listed 1,272 sites of local
`error` variables.

### Fixed
- **A reference needs a way to see its target.** In Python and
  JS/TS/TSX, an edge to another file now needs an import binding that
  name, and the import has to point into this repo (`import os from
  "node:os"` then `os.tmpdir()` is not a reference to some script's
  `const os`; calls have had that guard for a long time, references
  never did). A veto on the ladder's result, not a filter on its
  candidates: it can only ever remove an edge. Java and Go are left
  alone on purpose: their references are syntactic (`Type::method`,
  type identifiers) and can't be a shadowing local. Files that can't
  see each other: claude-code 35% -> 2.1%, cline 57% -> 2.9%,
  claude-buddy 10% -> 0%. The residue is deliberate: a JS-family file
  with no imports at all (script-style globals) and targets declared
  in a `.d.ts` (ambient) keep their edges.
- **Dynamic imports and CommonJS requires are imports.** `const { X }
  = await import("./x")`, `const { X } = require("./x")`, `const x =
  require("./x")`, and TypeScript's `require("./x") as typeof
  import("./x")` (167 sites in claude-code) bind a cross-file name
  exactly as a static import does, and were recorded nowhere. The
  veto above found this: it dropped 23 true edges on claude-code and
  12 on cline, every one a lazily imported module, until these were
  recorded. Hunting the lost edges for such bindings now finds 0 on
  all three repos. Call resolution gains from it too: claude-code +23
  call edges, cline +40 (tests reaching the module they `await
  import`), and `dekko deps` gains 140 and 107 module edges.
- **A relative import is in-repo, whatever the file stems say.**
  `import { run } from "./index"` and `from "."` sent `run()` to
  `external`. An index file's matching stem is its directory name
  (`acp` for `acp/index.ts`), which `./index` never spells, so the
  binding looked external and the call was thrown to noise. Static
  imports had this bug all along; recording dynamic ones exposed more
  sites (cline lost 9 correct call edges until it was fixed).
- **Eight plain reads the JS/TS reference query never captured:**
  `handle.close()`, `return DEFAULT_PORT`, `if (status)`, `!enabled`,
  `await pending`, `() => fallback`, `for (const e of EXTENSIONS)`,
  `export default styles`, plus TypeScript's `x!`, `x as T`, `x
  satisfies T`. Nobody had noticed, because a module-level `let
  knownChannelsVersion` read only by `return knownChannelsVersion` was
  being kept alive by a *false* edge from another file's same-named
  local. Removing the false edges exposed it: `unused` newly flagged
  53 symbols and 40 of them were alive through exactly these
  positions (measured by parsing each file and tallying the parent
  node of every same-file use). Writes stay out: an assignment target
  or `x++` is not a use. So does a subscript index (`table[key]`), a
  round-23 decision left standing.

### Changed
- **`dekko unused` reports far fewer false positives on JS/TS.**
  claude-buddy 40 -> 19, claude-code 1,956 -> 1,305, cline 2,136 ->
  1,687. 28 of the symbols that left the list were sampled against
  source and all 28 are alive (`loggedExposures.has(feature)`, `for
  (const t of LINUX_TERMINALS)`, `export default meta`). Going the
  other way, 9 and 7 symbols are newly flagged: object-literal methods
  called through a receiver dekko can't type (`conn.isConnected()`),
  which a false edge used to hide. That is `unused`'s documented
  call-blind limit, now visible instead of masked.
- `query uses` / `find_usages` return fewer, truer rows on JS/TS and
  Python. `dekko deps` and `query importers` see dynamic imports and
  requires.
- Every lost call edge was read: claude-code lost 3, all wrong
  (`axios.patch(..)` -> a repo `fn patch`; `getNativeModule()` from
  the external `image-processor-napi` -> an unrelated in-repo port).
  cline lost 0.

### Not fixed
- A local that shadows a *same-file* or an *imported* symbol
  (claude-code `utils/ide.ts` imports `errorMessage`, then `const
  errorMessage = ...` in a catch block). That needs the extractor to
  know scopes. `sanity` keeps its own read-side guard for it.

Spring-boot (Java), awesome-go and tensorflow's C/C++ (0 relative
includes) are unaffected; Rust has no reference edges and no `./`
import sources. Tests: +28 (2,330 passed).

## [0.43.68] — 2026-09-21

Round 32 Track 2. `dekko sanity` labels every grep-only row with a
cause, and "unexplained miss" is the label that costs an agent tokens:
it means "go read this line". Six repos had rows a person classifies
at a glance landing there, or worse, in a confidently wrong bucket.
Design: `.features/fixes/round31/02-sanity-miss-classification.md`.
No count moves. `matches`, `dekko-only` and `grep-only` are identical
before and after on every repo measured; only the cause string on a
grep-only row changes.

### Changed
- **A comment is a comment wherever it sits.** The comment cause used
  to require the line to be within 3 lines of the symbol's definition
  or in its file's header. That gate protected nothing: a comment line
  is never a call site. Far-away comments now read `comment mention —
  not a call site (a comment line, away from the symbol's
  definition)`; the two trusted zones keep their old wording. The
  comment check also moved above the type and string-literal checks,
  so `# Therefore, "tfrun" commands cannot include pipes` stops being
  called a string literal. With the zone gate gone the prefix test had
  to get stricter: a PHP 8 `#[Attribute]` line and a `/* note */
  realCall();` line are no longer comment-shaped.
- **Value references get a name, when they are real.** A row at a
  line the map already holds as a reference edge to the target
  (`names.some(isChrome)`, `process.on("exit", cleanup)` at module
  scope, Java's `.map(Src::getSource)`) reads `passed or stored as a
  value, not called — dekko has this as a reference`. The design
  called this tier "exact, not a guess" and scoped it to JS/TS. Both
  were wrong. Python, Go and Java record references too, so they are
  covered. And reading the rows that moved on claude-code showed the
  first screen full of `if (count >= 3) return;` one line below
  `const count = ...`: reference resolution has no notion of a
  shadowing local, and 58% of the candidate rows sat in files that
  neither define nor import the name. So a recorded edge only counts
  when the file could have made it (defines the target, imports its
  name or its declaring type, or is a same-package sibling in
  Go/Java) and binds no local of that name. claude-code: 1,507
  candidate rows became 166, and every one sampled is true
  (`instanceof ShellError`, `.sort(byIdAsc)`, `setTimeout(doRefresh,
  ...)`). The polluted edges themselves are a resolver bug, written up
  as Track 5 (`05-reference-edges-shadowed-locals.md`), not fixed here.
- **Rust and C++ get a shape-matched fallback, for one shape.** dekko
  records no reference edges for them, so `.map(Prompt::as_str)` and
  `let f = handlers::my_fn;` get a cause that says "line-shape match"
  and names the blind spot. It refuses a `(`, `::<` or `!` after the
  name, so a real missed call can never land in this bucket. Function
  targets only. The design also wanted a bare name in argument
  position (`register(my_fn)`) and a field init (`handler: my_fn,`).
  Both were built, measured on zed, and removed: they moved about
  10,000 rows and nearly all were same-named locals (`Some(buffer)`,
  `(program, args)`, `indent_guide(buffer_id, 1)`), because Rust names
  a getter after what it returns. A `::` in front of the name is the
  one thing a local can never have.
- **Rust struct literals and enum payloads read as type positions**
  for a type target: `let location = AbortMessageLocation {`,
  `AbortMessageLocation(AbortMessageLocation),`, `struct W(pub T);`.
  The type-position cause text widened to say so.
- `--fail-on-unexplained` fails less often. That is the point, but it
  is CI-visible, which is why this is under Changed.

### Fixed
- Rust `.map(Prompt::as_str)` with a *function* target was labelled
  "type annotation": the TS-shaped `: name` template matches the
  second colon of `::`. With the target's kind now known to the
  classifier, the path-value reading wins.

### Not classified, on purpose
- A name inside a longer string (`"settings.json cleanup
  complete."`). The same shape matches `eval("cleanup()")` and
  dispatch by string, which are real references. A classifier that
  admits it doesn't know beats one that guesses.
- A name mid-way through a multi-line Python docstring.
- A bare function name passed or stored as a value in Rust, C or C++
  (see above). Needs scope analysis, not a regex.

### Measured
Every row's cause snapshotted before and after over the `--all` sweep
(2,000-name cap), then the rows that moved were read, not just
counted. That reading is what caught both the shadowed-local edges and
the bare-name locals; the unit tests were green with each of them in.

| repo | matches / dekko-only / grep-only | unexplained before | after | |
|---|---|---|---|---|
| claude-code | 6,256 / 298 / 41,171, identical | 11,305 | 5,083 | −55% |
| spring-boot | 5,449 / 366 / 108,183, identical | 29,411 | 22,755 | −23% |
| zed | 9,811 / 1,202 / 492,810, identical | 27,361 | 22,518 | −18% |

Nearly all of the drop is comments (4,600 rows on claude-code, 4,052
Javadoc rows on spring-boot, 3,280 on zed). Recorded value references
account for 166 rows on claude-code; zed's Rust construction shapes
for 470 (`Edit {`, `SectionHeader(SharedString),`) and its
path-qualified values for 90 (`.map(String::as_str)`,
`cx.listener(Self::backspace)`). spring-boot's method references sit
past the sweep's alphabetical name cap, so that one was checked
directly: `dekko sanity getConfigurationPropertySource` labels
`.map(ConfigDataEnvironmentContributor::getConfigurationPropertySource)`
as a recorded reference.

## [0.43.67] — 2026-09-21

Round 32's one resolver finding (zed). Design:
`.features/fixes/round31/03-rust-macro-path-calls.md`. The agent that
found it blamed the `Type::name` owner rule; tracing its two repros
through the ladder showed one was already fixed by 0.43.64 and the
other was never a resolver bug. Verified by diffing zed's edge sets
before and after, classifying every lost edge against the call the
source actually wrote, and reading every new one. That diff turned up
two more defects, both fixed here.

### Fixed
- **Rust calls recovered from `assert!`/`assert_eq!` bodies keep the
  shape the source wrote.** tree-sitter doesn't parse macro arguments,
  so dekko scans their token stream for calls. It matched the `::` or
  `.` joining a call to its receiver and then threw it away, rendering
  every recovered call `receiver.name` with no argument count. So
  `assert_eq!(p, Point::new(1, 1))` reached the resolver as `Point.new`,
  and every rule that reads a Rust call's shape off its text misread
  it: the `Type::name` owner rule (0.43.62) and the unknown-type veto
  (0.43.64) stood down, the dot-call veto (0.43.63) fired on a call
  that wasn't one, and the ladder took the file's only `new`. 4,287
  such sites on zed, concentrated in tests, which is what `dekko
  affected` reads. Recovered calls now carry their real joiner, the
  whole `::` path (`gpui::Point`, `crate::util`, `Self`), a turbofish
  stepped over (`Vec::<u8>::new` reads `Vec::new`), the method chain
  as receiver (`x.iter().count()` was a *bare* `count()`, which the
  ladder resolves by preferring a free function), and an argument
  count where one can be counted safely. A joiner whose path head
  can't be read (`<Foo as Bar>::make(..)`) emits nothing: a call known
  to be qualified, reported as bare, is known-wrong. Each of these now
  matches what the same call yields when parsed outside a macro.
  `default` and `union` are keyword tokens, so `assert_eq!(x,
  Foo::default())` was never recovered at all; it is now.
  **zed: 866 edges removed, 363 added.** Of the 866: 165 `Type::name`
  paths credited to another type, 275 method chains credited to a free
  function, 419 method calls on a std or foreign value credited to the
  repo's only same-named method (364 of them five names: `.map()` →
  a test module's `map`, `.all()`, `.success()`, `.is_ok()`,
  `.try_recv()`). All four sites in the report are fixed
  (`SelectionsCollection.new` lost its two `Point::new` test callers).
  cline unchanged; spring-boot has no Rust.
- **A lifetimed Rust receiver (`&'a self`, `&'a mut self`) is the
  receiver.** It was read as an ordinary parameter, putting the
  method's arity one too high, so a correct lone target was rejected
  as arity-implausible: `syntax_map.layers(&buffer)` against `fn
  layers<'a>(&'a self, buffer: ..)`. Parsed calls had been losing these
  all along; it surfaced when recovered calls gained an argument
  count. zed: +325 edges, every one to a method with a lifetimed
  `self`, 385 of 391 checked with `.name(` written in the caller, all
  distinctive names (`Connection.exec_bound`,
  `BufferDiffSnapshot.hunks_intersecting_range`,
  `TestClient.build_workspace`).
- **`try_recv`/`try_send` join the Rust std-method denylist.** zed
  defines one `try_recv`. The arity bug above had been rejecting it by
  accident; with that fixed, 66 `rx.try_recv()` calls on ordinary
  channels took it as their sole candidate. Its one real caller is in
  the same file and resolves on that rung, before the guard.

### Known, not fixed
- **`Name(x)` tuple-struct constructions don't resolve when the struct
  is reached without an import hint**, because a type symbol has no
  parameter list and the arity check reads that as "takes zero
  arguments". Treating a type's arity as unknown was tried in this
  change and backed out: it adds 552 edges on zed, and 252 of them go
  to a struct whose name is also an enum variant somewhere (`Left`,
  `Text`, `Image`, `Path`). dekko doesn't index variants, so
  `Left(x)` from a glob-imported enum would land on an unrelated
  `struct Left`. Needs variant indexing first. Recovered macro calls of
  this shape deliberately carry no argument count, so they resolve
  exactly as they did before. Recorded as Track 4 in the design folder.

## [0.43.66] — 2026-09-21

Round 32 (7-repo sweep of 0.43.63) came back clean except for one
defect every agent reproduced, in the command whose job is telling an
agent when to trust dekko. Design:
`.features/fixes/round31/01-sanity-unused-membership.md`.

### Fixed
- **`dekko sanity --unused NAME` checks that NAME was actually flagged
  before saying so.** It ran its full grep sweep for any symbol and
  closed with "flagged unused, but N call-shaped references found
  (possible resolver miss)", for `SpringApplication.run` (4,861 grep
  hits) or zed's `px` (876 callers) as readily as for real dead code.
  An agent reading that concludes the resolver missed thousands of
  calls. It computed `has_dekko_evidence` and then never branched on
  it, and that wasn't even the right question: `dekko unused` also
  spares roots, call-blind languages, and types kept alive by
  heritage or type-position use, so a struct used only as an
  annotation was told "none, this is why it was flagged" about a flag
  that never existed. Both directions are fixed the same way: one
  predicate, `unused.unused_status`, built on the very function
  `find_unused` now runs per symbol, so the two cannot drift apart (a
  parity test walks every symbol in a fixture to pin that). A symbol
  that isn't flagged gets three lines saying why and pointing at plain
  `dekko sanity <target>`, and **no grep sweep**: the sweep's volume
  is what made the false report look authoritative. zed's `px` now
  answers in 2.6s, nearly all of it loading the map. `dekko unused`'s
  own output is byte-identical before and after the refactor on
  claude-code (1,956 results) and zed (11,147).

### Added
- `dekko sanity --unused` takes `--roots GLOB` (repeatable), the same
  flag as `dekko unused`, so a symbol you rooted there doesn't read as
  flagged here.
- `sanity --unused --json` gains `flagged_by_unused` and
  `unused_status` on every path (and `skipped`/`advice` when not
  flagged). Additive: every existing key is still present.

## [0.43.65] — 2026-09-21

Round 31's one open performance question (P4.1): cold-rev-cache `diff`
on tensorflow took 754s, against 274s in round 29. It sat open for
three days as "needs a machine with disk headroom". It was never the
disk.

### Changed
- **`diff`, `affected` and `workset` default to `--jobs 0` (all
  cores), like `map` has since 0.43.53.** The first time one of them is
  asked about a commit, dekko maps that old commit from scratch, and
  these three were still doing it single-threaded. Round 29 flipped
  `map` and left them behind, so the two P4.1 timings were never the
  same configuration: the follow-up pass had already shown the default
  running past 585s unfinished where `--jobs 6` finished in 251s.
  tensorflow, cold `diff HEAD~1`, same session on 0.43.63 (round 32's
  tensorflow agent): **720s sequential**, 277s at `--jobs 4`, 226s at
  `--jobs 6`, **181s / 194s at `--jobs 0`**, so ~3.8x, and more workers
  never ran slower. The new default measured on this build: 325.7s idle
  / 355.6s contended, both taken right after an extraction-cache
  invalidation, so they overstate the steady-state cost. spring-boot, interleaved same-session: 18.9s / 19.0s
  all cores vs. 22.2s / 19.9s sequential, so a mid-size repo is a wash
  with a slight edge, never a loss (0.43.54's pool sizing is what makes
  that safe: workers scale to the work, small repos stay sequential).
  `--jobs 1` still forces a sequential run. A warm call (the rev is
  cached after the first) is unaffected.
- **The "may take a while" note now covers the parallel wait too.**
  It used to stay silent whenever workers were in play. Five minutes of
  nothing on tensorflow is the same "is it hung?" problem round 15
  added the note for, so it prints either way, with its own wording
  and without the now-pointless `--jobs 0` hint.

### Fixed
- **MCP `impacted_tests` and `workset` no longer resolve a cold rev
  single-threaded.** They called `affected.run`/`workset.run` without
  a `jobs` argument at all, so they got the functions' own sequential
  default whatever the CLI did: **718.6s** on a first-touch tensorflow
  call (round 32, measured over stdio), with zero output to the client
  the whole time. No MCP client waits for that. This is the path an agent
  actually hits. They now use all cores.

### Verified
- **0.43.63's name-delta incremental map, re-timed on tensorflow**
  (the one number that release couldn't take, for lack of disk). Same
  files, probe and `--jobs 6` as the round-31 measurement: adding a
  function **223.4s → 46.3s**, removing it **290.6s → 42.8s**, now the
  same cost as a comment edit (40.5s); `--full` is 231.1s. The
  incremental `map.json` after the add equals a `--full` of the same
  tree.

## [0.43.64] — 2026-09-21

Round 31's last silently-wrong-answer item (zed F6b / A6), from the
two-step design in
`test-repos/reports/31-tokentest-7repo-post04355/FIX-PLAN-remaining.md`.
Verified the round-31 way: re-map zed and cline with the old and new
builds, diff edge *sets* (`scripts/map_edge_diff.py`), and check every
lost edge against the source, not a sample.

### Fixed
- **A Rust `Type::name()` path rooted at a type the repo doesn't
  define no longer lands on a repo symbol** (zed F6b). `Vec::new()`,
  `Box::new()`, `Default::default()`, `String::new()` and a
  macro-generated `StyleRefinement::default()` name a std,
  third-party, or macro-minted type, so no repo symbol can be the
  target. The ladder took whatever `new`/`default` sat in the caller's
  file anyway: `Vec::new()` inside `MultiWorkspace` became a call to
  `MultiWorkspace.new`. 0.43.62's owner rule couldn't veto these
  because it needs an in-repo type to name as the owner. The estimate
  going in was ~35 edges. It was **962 on zed**: `Vec` 366, `Box` 229,
  `Default` 141, `String` 126, then 67 other external types. All 962
  were matched to the receiver the caller actually wrote, 0 of them
  name the lost target's own type, and none of the 71 receiver names
  has a `struct`/`enum`/`trait`/`type`/`union` definition or a `use ..
  as` rename anywhere in zed. 0 call edges added. cline's two Tauri
  `main.rs` files: 6 of 6 lost edges wrong (`tauri::Builder::default()`
  → `UpdateStatus.default`), TS untouched. The rule stands down
  whenever the repo could know the name: any in-repo symbol carries
  it, a candidate is a member of it (a macro-generated struct with a
  handwritten `impl`), the file `use`s it from inside the repo, it is
  one or two characters (`T::default()`), or it is an associated-type
  path (`T::ProtoRequest::stop()`, `<Cmd as LspCommand>::ProtoRequest
  ::stop()`). The last two guards came from the same edge diff: a
  first cut that only looked for `as` renames in the calling file lost
  6 *correct* zed edges, because `pub use text::Buffer as TextBuffer;`
  lives in another crate.

### Added
- **Rust `type X = ...;` aliases are indexed as `type_alias` symbols.**
  The prerequisite for the fix above: without an `Alias` symbol,
  `Alias::new()` would read as an unknown type and go external.
  Module-level aliases only (file scope or a `mod` body). The same
  node inside an `impl` block is an *associated type* (`type Output =
  Foo;`) and is deliberately not indexed. Extraction caches rebuild
  themselves on upgrade (the spec fingerprint covers the query).
  Aliases show up in `outline`, `search`, `query symbol` and
  `unused --types` like TS aliases already do. Indexing them exposed
  three places that treated "any type kind" as "a type you can hang
  members or impls on", each now excludes a Rust alias: the owner
  rule's gate (an alias's members live under the aliased type), `impl
  X for Y`'s supertype (zed's `impl ActionHandler for ..`, accesskit's
  trait, resolved to `ui`'s unrelated `type ActionHandler = Box<dyn
  Fn(..)>`), cross-file impl placement (`impl<T> TideResultExt for
  tide::Result<T>` landed on collab's own `pub type Result<..>`), and
  the typed-parameter gate for generic containers (`type Result<T>`
  is the foreign container under a local name). Net heritage change on
  zed: +1, a same-file `impl Dimension for TabStopCount` that is
  literally what the source says.

## [0.43.63] — 2026-09-18

The rest of round 31's open list, from the design in
`test-repos/reports/31-tokentest-7repo-post04355/FIX-PLAN-remaining.md`.
Implemented as three parallel work packages, then integrated and
re-verified on zed, cline and claude-code by diffing edge sets against
the previous maps (`scripts/map_edge_diff.py`, new). That review
changed three of the packages' own rules; each is noted below.

### Fixed
- **`use localmod::X` resolves to the local module, not a same-named
  workspace crate** (zed F12). Rust 2018 resolves a bare first segment
  against local scope first; dekko went straight to the crate table.
  zed: 26 impossible module edges removed, 340 correct ones added, and
  `deps --cycles`' headline cycle shrank from **100 files across 12
  crates to 38 files in 1 crate**. The 100-file cycle was an artifact.
- **A Rust `impl X for Y` can only name a trait** (zed F8). Heritage
  candidates were filtered to "any type kind"; a same-named struct is
  not a legal target. zed `heritage_ambiguous` 86 → 32; `Component`
  subtypes 7 → 66 of 66, matching grep. All 30 lost edges targeted a
  struct.
- **`impl Trait for X` in a different file from `struct X` gets its
  edge** (zed F2). The extractor dropped the clause when the type
  wasn't in the same file; it now emits it with a `subtype_name`, and
  the resolver places it within the same crate (exactly one match, or
  it is counted as unplaced, never guessed). zed +49 heritage edges,
  0 lost. `map.json` gains `heritage_unplaced_subtype_count`.
- **The receiver-type guess no longer reads a generic *argument* as
  the receiver's type** (zed F7). `rows: &BTreeMap<DisplayRow, u8>`
  then `rows.get(..)` resolved to `DisplayRow.get`. Transparent
  wrappers (`Box`/`Rc`/`Arc`/`Ref`, `Optional`, `T | undefined`) still
  pass through; the chain stops at the first opaque type. zed: 138
  guessed edges removed. *Integration review* found the package's
  cross-crate guard also disproved correct edges and fixed it: an
  explicit `use text::BufferSnapshot` or a `text::BufferSnapshot`
  annotation now outranks "this crate defines one too", and a
  *private* same-named struct elsewhere in the crate no longer counts
  as in scope (45 correct zed edges recovered). It also found the rule
  breaking TypeScript inline object types: `input: { bot: Chat;
  client: HubSessionClient }` stopped at `Chat`, dropping 9 correct
  edges on cline. The receiver's own field (`input.client`) now picks
  the type.
- **A Rust dot-call can never reach a free function** (zed F11:
  `.px(..)` → `fn px`, `x.clone()` → a test module's `fn clone`), and
  the std-method denylist gained the iterator/`Option` adaptor names
  that were resolving to repo symbols (zed F9: `.flatten()` →
  `Edit.flatten`, 454 callers; `.chain()` 217). zed: 860 wrong edges
  removed. *Integration review* turned the dot-call rule from a
  candidate pre-filter into a veto on the result: pre-filtering
  removed a free `fn or`, left `EnvVar.or` as the lone survivor, and
  137 `Option::or` calls newly resolved to it. A veto can only remove
  an edge. The widened denylist also stopped `Promise.all(..)`
  resolving to a repo function named `all` (196 wrong edges on
  claude-code).
- **`sanity --group-by-file` groups the full bucket, then applies
  `--limit` to the groups** (zed F10). It used to group whatever rows
  survived truncation, hiding exactly the clustering it exists to show.
  `docs/cli.md` had documented that as intended; corrected.
- `sanity` recognizes ` * ...` JSDoc/Javadoc continuation lines as
  comments, only when a bounded backward scan finds an unclosed `/*`
  (a wrapped `* x` multiplication line must not match).
- `ambiguous`: the "path+qualname alone can't disambiguate" hint no
  longer prints under a single candidate (zed F5).
- MCP `outline` on a directory capped its rows but forwarded one
  sparse-file note per file uncapped: ~59K tokens on claude-code's
  `src/`, now ~3.6K with the omitted count disclosed. `outline` also
  follows the budget-governs-alone rule from 0.43.59.

### Performance
- **Incremental `dekko map` no longer pays full price for adding,
  removing, renaming or re-signing a function.** The round-30 gate
  re-resolved the whole repo on any symbol-set change (tensorflow:
  41s for a body edit, 223-291s for adding or removing one function).
  It is now a name delta: an unchanged file is re-resolved only if one
  of its recorded calls names a changed symbol. Any change to a *type*
  still takes the full path, because receiver- and parameter-type
  lookups consult the index for type kinds only, and excluding type
  deltas removes that whole class of question rather than answering
  it. An audit of every index read the ladder makes found two the
  design had missed, both closed: an added constructor is invisible to
  name scanning (callers call the class), and an aliased import
  resolves under a different name than the call's. Parity (incremental
  `map.json` identical to `--full`) is tested for add / remove /
  rename / re-sign / add-method / add-struct, and held live on
  spring-boot and cline, including an `export`-flag flip that changes
  workspace-package narrowing in *other* files.

### Added
- `scripts/map_edge_diff.py`: compares two `map.json` files by
  `(caller, callee)` pairs. Raw section equality is useless because
  ids are interned ints that shift with the symbol table.

### Known limits
- `Default::default()` / `Vec::default()` and calls on
  macro-generated types still take a same-file `default` (~35 edges on
  zed). Fixing it safely needs Rust `type` aliases indexed first
  (designed as A6, not implemented).
- The std-method denylist is not language-gated. That is what fixed
  `Promise.all`, and it only ever suppresses a single-candidate guess
  with no structural evidence, but a repo method named `find`/`count`
  called on an untyped receiver now reads as external.
- `heritage_unplaced_subtype_count` is in `map.json` only; no query
  output discloses it yet.

## [0.43.62] — 2026-09-18

A regression 0.43.61 exposed, caught the same day by the zed coverage
follow-up's A/B against the previous build (finding F6 in
`test-repos/reports/31-tokentest-7repo-post04355/coverage-followup/zed.md`).

### Fixed
- **A Rust `Type::name(..)` path can only resolve to a member of
  `Type`** (or a default method of a trait). The ladder used an
  explicit type receiver as *positive* evidence only: exactly one
  `Type.name` won, and anything else fell through to the generic
  ladder with the full candidate list. So `Point::default()` where
  `Default` is derived (no `Point.default` symbol exists) took the
  file's only other `default`, `ScrollHandle.default`. 0.43.61 made
  this visible by no longer short-circuiting `crate::`-imported
  receivers to `external`; the missing rule was older than that. Now
  candidates are narrowed to `Type`'s own members, which outrank trait
  defaults, with the written argument count breaking ties between an
  inherent `fn zero()` and a trait impl's `fn zero(_cx)`. Nothing left
  means no plausible repo target: `external`. A free function, an
  unrelated struct (`Enum::Variant(..)` landing on a same-named
  struct), or another type's method can no longer be chosen.
  zed vs. 0.43.61: **952 call edges removed, 3,612 added**. Every
  removed edge was checked mechanically against its source line: 950
  name a different receiver type than the resolved owner, and the
  other 2 moved to the right crate's `Key.new`. Of the added edges,
  2,226 have receiver == owner and 1,169 are trait default/UFCS
  methods (`TitleBarSettings::get_global(cx)` → `Settings.get_global`);
  zero name a mismatched non-trait owner.
- **`use crate::Foo;` prefers the importing file's own crate** when
  the repo has two `Foo`s. `Foo::build()` had landed on an unrelated
  crate that isn't even a dependency. Zero in-crate candidates (a
  crate root re-exporting another crate's type, e.g. `editor`'s `pub
  use multi_buffer::MultiBuffer;`) is no evidence and changes nothing.
  Applies to bare imported names and pure `Type::name` paths only.

Three regressions in these fixes themselves were caught by
re-mapping zed before any unit test existed, and each is now a test:
`Point::zero()` drifting to a same-file trait impl, a UFCS
`RangeExt::overlaps(&a, &b)` drifting to a rival trait whose arity
fit the explicit `self`, and `Store::global(cx).read(cx)` landing on
the crate's one free `fn read`.

### Known limits (found, not fixed)
- `Default::default()`, `Vec::default()`, `FxHashMap::default()` and
  calls on macro-generated types still take a same-file `default`
  when one exists (~35 edges on zed). The receiver names no in-repo
  type, and Rust `type` aliases aren't indexed, so "unknown type ⇒
  external" would also drop real `Alias::new()` edges. Needs alias
  indexing first.

## [0.43.61] — 2026-09-18

Found by round 31's zed coverage follow-up
(`test-repos/reports/31-tokentest-7repo-post04355/coverage-followup/zed.md`),
which was asked to attack 0.43.59's heritage tiebreak and did.

### Fixed
- **Rust `use crate::…` / `super::…` / `self::…` imports are in-repo
  by definition.** `use crate::{AgentTool};` names a trait the crate
  root merely re-exports (`pub use thread::*;`). The "does this import
  point into the repo" test compares the source's segments against
  file stems, drops `crate` itself, and was left with `AgentTool`,
  which is no file's stem. So the clause was filed `external`, and
  because external is not ambiguous, the row vanished from `query
  subtypes` with no hint: **`AgentTool` showed 11 of 35
  implementors.** A file reaching the same trait through a glob
  (`use crate::prelude::*;`) resolved fine, which was the tell. The
  same short-circuit hit receiver-qualified calls (`use crate::{Vim};`
  then `Vim::take_forced_motion(cx)`). zed: **+213 heritage edges
  (`AgentTool` 35 of 35), +2,388 resolved call edges, 0 lost in
  either**; `heritage_external` 2,503 → 2,289. Same defect class as
  0.43.57's workspace-package fix, in a different language.
- **0.43.59's hintless fixture-decoy tiebreak no longer resolves code
  that lives beside the fixture.** It pointed 7 clauses in zed's
  dylint UI tests (`tooling/lints/ui/*.rs`) at the real `gpui` trait.
  Their paths carry no `test_fixture` marker, but they are compiled
  with `--extern=gpui=<fixture rlib>`, so their `use gpui::*;` *is*
  the stand-in. They were honestly ambiguous before 0.43.59, so this
  was a regression that release introduced. The build flag is
  unknowable statically; shared directory depth is a usable proxy. A
  clause whose file shares a deeper directory prefix with a fixture
  candidate than with the real one now stays ambiguous. zed after
  both changes in this round: `Render` 349 resolved, 9 ambiguous (all
  9 beside the fixture), versus 174 / 184 before 0.43.59. **This
  corrects 0.43.59's "355 of 358" figure**, 6 of which were these
  wrong edges.

### Known limits (found, not fixed)
- An `impl Trait for X` block in a *different file* from `struct X`
  (`text_finder/render.rs` implementing for a struct in
  `text_finder.rs`) produces no heritage edge at all: Rust heritage
  resolves its subtype side by same-file name lookup. 3 of zed's 396
  `impl Render for` blocks. Tracked in the round-31 OPEN-ISSUES.

## [0.43.60] — 2026-09-18

Found by round 31's tensorflow coverage follow-up
(`test-repos/reports/31-tokentest-7repo-post04355/coverage-followup/tensorflow.md`).

### Fixed
- **Shell function calls are extracted, so bash functions stop
  reading as dead code.** tree-sitter-bash's call node is `command`,
  which the generic (Tier-2) extractor's call heuristic never matched.
  Not one call was extracted from any `.sh` file, so every bash
  function had fan-in 0, `query callers` came back empty, and `dekko
  unused` listed live functions with no caveat: tensorflow's `tfrun()`
  (27 call sites), and on claude-buddy 5 of 5 flagged bash functions
  were false positives (`pick_reaction` alone has 27 call sites).
  `command` nodes are now collected, including inside `$(...)` and at
  script top level. Only an identifier-shaped command name is kept: a
  path, an expansion or a flag in command position (`./build.sh`,
  `"$TOOL"`) can never be a call to a repo-defined function, and
  keeping them would add an `external` entry per script path.
  Requires the `[all]` extra, like all shell parsing. Re-map to pick
  it up (the version bump invalidates the extraction cache).
- **`dekko unused` no longer judges a language it extracted no calls
  from.** The bash bug was one instance of a general blind spot: ~55
  generic-grammar languages share the same node-type heuristic, and
  wherever it misses a grammar's call node, every function in that
  language is "unused" by construction. `unused` now skips symbols in
  any generic-tier language with zero extracted calls repo-wide and
  says so: `note: N symbol(s) not evaluated (lang N) -- dekko
  extracted no calls from any file in that language here ...`, also
  on the "no unused symbols" path and in `--json` `caveats`. Tier-1
  languages are never skipped: each has a dedicated call query, so a
  Tier-1 file with no calls really has none.
- MCP: a required argument sent with the wrong type reports
  `argument 'symbol' must be a string, got int` instead of `missing
  required argument`, which sent callers hunting for a key they had
  already supplied.

## [0.43.59] — 2026-09-18

Round 31 close-out: the remaining P2/P3 items from
`test-repos/reports/31-tokentest-7repo-post04355/OPEN-ISSUES.md`.
Two of these change which bucket a call or heritage clause lands in;
**no existing resolved call edge changes** (verified identical on
cline, claude-code and zed).

### Fixed
- **A call whose only same-named repo symbol is rejected is `external`,
  not `ambiguous`** (round 31 cline.md §4.2, the `at` entry).
  `arr.at(-1)` against the repo's only `at`, a local two-parameter
  `at(r, c)`, is correctly refused by the arity guard. But it was then
  filed as ambiguous, against a candidate list of one. A collision
  needs two live candidates; this call has none. It showed up as the
  self-contradictory `ambiguous --by name` row "86 sites, avg 1.0
  candidates" and made `query symbol at` claim "+86 additional call
  site(s) resolved ambiguously" about calls that provably cannot be
  its own. Same defect class, and same remedy, as the round-22
  `_NOISE` split. cline: 542 of 6,271 ambiguous entries had exactly
  one candidate, now 20; claude-code: 990 → 0. A sample of 14 was
  checked by hand: all 14 rejections were right
  (`Buffer.byteLength(s, "utf8")`, `vscode.commands.executeCommand`,
  a zero-arg call to a local closure). The 20 left on cline are the
  cross-*language* sole-candidate case, which stays ambiguous by an
  earlier, deliberate design decision.
- **Rust heritage: the fixture-decoy tiebreak no longer needs the file
  to name the crate** (round 31 zed.md / OPEN-ISSUES P2.1). On zed's
  motivating example only 174 of 358 `impl Render for` clauses
  resolved. All 184 ambiguous ones had the *identical* two candidates,
  the real `crates/gpui/src/element.rs::Render` and the
  `tooling/lints/test_fixture/gpui` stand-in, and reached `Render`
  through a glob (`use ui::prelude::*;`) or a re-exporting crate
  (`use ui::Render;`), so there was never a crate name to build the
  round-24 hint from. The same convention now applies as a last
  resort: real code doesn't implement a test fixture's stand-in
  trait. Exactly one non-synthetic survivor is still required, two
  real crates sharing a name stay ambiguous, and every edge resolved
  this way is counted in the already-disclosed
  `heritage_synthetic_tiebreak_count`. A clause written *inside* a
  fixture tree is left ambiguous: live-testing caught 8 edges from
  zed's `test_fixture/render_consumer` wrongly pointed at the real
  trait before that guard existed. zed: **+306 heritage edges, 0
  lost, heritage-ambiguous 384 → 78; `Render` 355 of 358 resolved**
  (the other 3 are that fixture consumer).
- **`dekko map` no longer counts unparsed files as mapped** (round 31
  claude-buddy.md S2). Without the `[all]` extra, a repo's bash files
  yield no symbols, yet the summary read `mapped 57 files (... bash
  12 ...)`. The top line now counts and lists only what was parsed,
  and a new line names the unparsed languages with the fix:
  `NOT parsed (no symbols, no edges): bash 12 -- grammar not
  installed; ... pip install 'dekko[all]'`. The existing `skipped: no
  grammar installed N` line is unchanged.

### Changed
- **`query --budget N` with no `--limit` lets the budget govern**
  (round 31 claude-code.md). `--limit` (rows, default 50) and
  `--budget` (tokens) are independent caps, and both bound by default:
  `query callers getGlobalConfig --budget 20000` returned 51 of 203
  rows. An explicit budget without an explicit limit now lifts the row
  default; an explicit `--limit` is always honored; neither flag keeps
  the 50-row default. The MCP query-backed tools follow the same rule
  for an explicit `budget` argument.
- **`query supertypes`/`subtypes` pick the one type among an
  ambiguous name's candidates** (round 31 spring-boot.md). A bare name
  shared by a class and its own constructors isn't ambiguous for a
  heritage query, since only the type is a valid target. The choice is
  printed to stderr. Two or more type candidates stay ambiguous, and
  every other action is unchanged.
- **`dekko unused` leads with a warning when most of the list is
  dispatch candidates** (round 31 spring-boot.md: 2,323 of 3,281,
  71%). The existing caveat fired correctly, but as the last line
  under thousands of rows, on exactly the interface-heavy repo shape
  where the list is mostly *not* dead code. At ≥50% (and ≥20
  candidates) a `warning:` now prints directly under the header;
  `--json` carries it as `dispatch_majority_warning`.

## [0.43.58] — 2026-09-18

The rest of round 31's P1 list, plus one P3 that turned out to share
a classifier with it. Analysis:
`test-repos/reports/31-tokentest-7repo-post04355/P1.1-workspace-package-imports.md`.

### Fixed
- **`dekko deps` / the module graph resolve workspace-package imports
  to a source file** (round 31 P1.1b, the module-graph half of
  0.43.57's fix). `import ... from "@cline/llms"` was still listed
  under `external`. A declared workspace member's bare specifier now
  resolves to the package's *source* entry: the manifest's `exports`
  (subpaths and single-`*` patterns included) and
  `source`/`types`/`module`/`main` fields, each mapped from build
  output back to source (`./dist/index.js` → `src/index.ts`; the
  `dist/` tree is gitignored and never in the map), then the
  `src/<subpath>` / `<subpath>` conventions. General `exports`
  conditions are tried before environment-specific ones (`browser`,
  `worker`, `react-server`, ...) regardless of manifest order, so a
  manifest that lists `browser` first doesn't send every importer to
  `index.browser.ts`. An entry that can't be found stays `external`
  rather than guessing. On cline: **+347 module edges, −347
  externals, zero `@cline/*` specifiers left external**; call and
  heritage graphs untouched.
- **A relative import of a dotted filename keeps its import hint**
  (round 31 cline.md §4.1 Bug B). Import sources are split on `.` as
  well as `/` (right for `pkg.mod.name`), so the stem of
  `catalog.generated-access.ts` could never appear among the segments
  and `import { x } from "./catalog.generated-access"` told the
  resolver nothing. A name defined twice in the repo then went
  ambiguous even though the file's own import was unambiguous, and the
  target read as fan-in 0. Dotted stems now get a component-wise
  comparison (extensionless, ESM `.js`-for-`.ts`, and C/C++
  `foo.pb.h` spellings); the two external-import guards use it too.
  Gated on the stem containing a dot, so undotted files match exactly
  as before. cline's reported case: fan-in 0 → 7, `sanity` matches
  0 → 9. This is why three other repos couldn't reproduce it: it takes
  a dotted non-test source file *and* a colliding name. It is **not**
  the same root cause as P1.1, contrary to the round-31 guess.
- **`dekko sanity` recognizes multi-line `export { ... }` lists and
  packed specifier lines as import statements.** Two misclassifications
  with one cause: the multi-line member check only accepted a line
  that was exactly one bare `name,` under an `import {` opener. A
  barrel's re-export list (`export {` + `getGeneratedModelsForProvider,`)
  fell through to "generic name in a dense repo", which told an agent
  a specific 30-character identifier was a common word (the mislabel
  half of cline Bug B). And a line packing several specifiers
  (`searchBuddy, renderBuddy, SPECIES,` / `type Species, type Rarity,`)
  fell through to "unexplained miss" (round 31 claude-buddy C1 /
  OPEN-ISSUES P3.6, which two one-name-per-line repos could not
  reproduce, for exactly that reason). The opener now also accepts
  `export {`, `export type {` and `import Default, {`; the member
  line is any pure specifier list naming the symbol as a whole word.
  Object-literal shorthand and call-shaped lines still don't match.

## [0.43.57] — 2026-09-18

Round 31 P1.1. Unlike 0.43.56's two disclosure fixes, this one
**changes what dekko resolves**, on JS/TS monorepos that declare
workspaces. Every other repo is byte-identical. Analysis and numbers:
`test-repos/reports/31-tokentest-7repo-post04355/P1.1-workspace-package-imports.md`.

### Fixed
- **Workspace-package imports are no longer mistaken for npm
  dependencies** (round 31 cline.md §4.1 Bug A). `import type {
  ApiHandler } from "@cline/llms"` names a *package*, not a file, so
  the resolver's "does this import point into the repo" test, which
  only ever compared the specifier's segments against repo file
  stems, called it external. The call or heritage clause landed in
  `external` before the candidate ladder ran. Reported as one missing
  `query subtypes ApiHandler` row; measured on cline it was 2,610
  import bindings across 690 files. dekko now reads the repo's
  declared workspaces (npm/yarn/bun `workspaces`, array or
  `{"packages": [...]}`; `pnpm-workspace.yaml`; `**` and `!negation`
  globs) into a package-name → directory table, treats a matching
  import as in-repo, and narrows a colliding name to the candidates
  living under the imported package (exported ones preferred). Zero
  in-package candidates means a cross-package re-export, which is no
  evidence, so the rest of the ladder runs untouched. Only *declared
  members* count: cline ships a stub package literally named `vscode`
  that is no workspace member and must not capture real `from
  "vscode"` imports. On cline: **+975 resolved call edges, +14
  heritage edges, −882 false externals**.
- **12 confidently wrong edges on cline now point at the right
  package.** The old stem test could also pass by coincidence:
  `import { ClineAccountService } from "@cline/core"` "resolved" into
  `apps/vscode/.../ClineAccountService.ts` purely because that file's
  stem equals the imported name. Package-scoped narrowing now runs
  before the stem hints, so the package the name was actually
  imported from wins.

### Changed
- `.dekko/resolved-calls.json.gz` carries a `workspace_hash`. A
  renamed `package.json` or edited `workspaces` glob changes which
  imports count as in-repo without touching any source file, which
  the incremental call-resolution cache's path-set and symbol gates
  can't see. A mismatch is a cache miss (one full re-resolve). Caches
  written by earlier builds were already invalidated by the version
  bump.

### Known limits
- The module graph (`dekko deps`, `query importers`) still reports a
  workspace package as `external`. Entry-point resolution
  (`exports`/`main` point at `dist/`, not source) is a separate
  change, tracked as round-31 P1.1b.
- A receiver-qualified call through an imported namespace
  (`Llms.getProvider()`) is un-blocked from `external` but not
  narrowed: the namespace is routinely re-exported from another
  package, so its import says nothing about where the member lives.

## [0.43.56] — 2026-09-18

Round 31's two cross-language disclosure defects, both found by the
7-repo sweep in `test-repos/reports/31-tokentest-7repo-post04355/`.
Neither is a resolution change: dekko resolves exactly what it did
before, and now says so honestly where it previously reported a
confident-looking number it could not back up.

### Fixed
- **`dekko sanity`'s buckets now reconcile with the grep command it
  prints** (round 31, found independently on all five language
  families tested — JS/TS, TS/TSX, Java, Python/C++, Rust). A
  symbol's own declaration line, and every other same-bare-named
  symbol's, is filtered out of the sweep before the
  matches/dekko-only/grep-only split — correctly, since a declaration
  is not a call site and never was a miss to explain. But the
  exclusion was *silent*, so `matches + grep-only` never summed to the
  hit count of the `grep:` command printed one line above it. The
  shortfall equalled the number of colliding same-bare-name
  declaration lines, which on an overload-heavy repo is never zero:
  spring-boot's `SpringApplication` constructor came up 2 short of its
  own grep's 983 hits, `Binder.bindOrCreate` 4 short of 40. An agent
  reconciling `sanity`'s numbers by hand — the entire purpose of the
  command — found an unexplained gap every time and had no way to tell
  a filtered declaration from a dropped result. Both text and `--json`
  now carry an `excluded_declarations` count, a `grep_hits_swept`
  total, and a note explaining the exclusion; `--unused` mode, which
  filtered identically, gets the same treatment.
- **`dekko deps --file` no longer reports a bare `imports (0)` for a
  file that wires its imports up at runtime** (round 31, confirmed on
  five repos across four language families). dekko resolves static
  imports only; files depending entirely on `importlib`/`LazyLoader`
  (Python), dynamic `import()` (JS/TS), `include!` (Rust), or
  `Class.forName`/`ServiceLoader` (Java) therefore resolve to zero
  edges — accurate, but presented as a confident zero with no caveat.
  Worst measured case was tensorflow's `keras/utils/version_utils.py`,
  where 6 of 9 real edges were invisible behind that zero. dekko still
  does not resolve these edges (a genuinely hard static-analysis
  problem, not a bug); it now scans the named file for the constructs
  it provably cannot follow and says so, naming each construct and its
  occurrence count. The disclosure is evidence-gated — it appears only
  when such a construct is actually present, never on every zero — and
  reads as "the count above covers static imports only" when static
  edges do exist.

## [0.43.55] — 2026-09-17

### Performance
- **Process pools now fork instead of spawn where provably safe**
  (round 30, Track 3 (c)) — on macOS every pool worker previously
  received a private, pickled-and-unpickled copy of the resolution
  indices (~205 MB pickled per worker on a tensorflow-scale repo,
  serially, per pool). Pools now run under an explicitly chosen
  multiprocessing context: `fork` when the parent is provably
  single-threaded on POSIX (the CLI and MCP server), `spawn` otherwise
  (the daemon — its status thread makes fork unsafe — and Windows,
  which has no fork). Fork workers share the parent's indices
  copy-on-write, eliminating the per-worker transfer entirely.
  Measured on interleaved runs: spring-boot resolve **6.70s → 4.66s**
  median (fork won every pair, non-overlapping ranges), tensorflow
  **176.4s → 163.4s** median (fork won all three interleaved pairs).
  `map.json` output is byte-identical either way. Choosing the context
  explicitly also pins `fork` on Linux ahead of Python 3.14's
  `forkserver` default flip, which would have silently reintroduced
  per-worker pickling there. A `BrokenProcessPool` under fork retries
  under `spawn` (a fork-specific failure and CPU contention are both
  covered by one bounded fallback), so a host where fork misbehaves
  degrades to the previous behavior at the cost of one attempt.
  `DEKKO_POOL_START_METHOD=spawn` opts a problem host back out. The
  shared-pool follow-up ((d) in the round-30 design docs) was closed
  WONTFIX by its own measurement gate.

## [0.43.54] — 2026-09-16

### Performance
- **Incremental `dekko map` now reuses call resolution for unchanged
  files** (round 30, Track 1) — `resolve()` was a pure function of the
  *whole* file list and ran unconditionally on every map, so an
  incremental run only ever saved tree-sitter extraction. Editing one
  file out of 9,942 cost 93% of a full rebuild, and on a large repo
  repo-wide resolution was the floor no amount of parallelism could get
  under. Per-file call resolution is now cached in
  `.dekko/resolved-calls.json.gz`, so an edit re-resolves only the files
  that changed. **tensorflow: a one-line edit went from 229s to 46.6s
  (4.9x).** Reuse is gated on the global resolution inputs being
  *provably* identical to the cached run — no file added, deleted, or
  renamed, and no changed file altering its resolution-relevant symbols —
  so an unchanged file's resolution is identical by construction rather
  than by heuristic. Anything else (a new/renamed/deleted symbol, a
  signature change, a path-set change, a dekko or resolver-source change)
  falls back to the previous full repo-wide resolve. Output is unchanged:
  a test suite asserts `map.json` is identical between incremental and
  `--full` runs across a range of edit shapes. Only the call pass is
  cached; refs/heritage/imports/throws/catches are still recomputed in
  full. The new artifact is small — symbol ids are interned, so it lands
  at ~3% of the extraction cache's size (17 MB next to tensorflow's
  559 MB `cache.json`).
- **A byte-identical no-op `dekko map` still short-circuits** before
  resolution as before, so it writes no resolve cache. A deleted or
  corrupt `resolved-calls.json.gz` is therefore repaired on the next run
  that does real work, not on a no-op run; a corrupt file always reads as
  a cache miss rather than an error.
- **Resolution passes no longer build oversized worker pools** (round
  30, Track 3b) — the parallel gate was a flat 5,000-item floor, so any
  pass clearing it got the *full* requested worker count regardless of
  how little work there actually was. In practice that meant a pass with
  ~7,000 items was handed an 11-worker process pool, and because each
  worker unpickles its own private copy of the whole repo symbol index
  under `spawn` (~90-205 MB depending on repo), nearly all of that time
  was pool setup rather than resolution. spring-boot's reference pass
  (8,430 items) took 5.84s to do work that takes 0.11s sequentially;
  tensorflow's throw pass (7,376 items) took 22.52s to do work that
  takes 0.92s. A new `_pool_workers` now scales the worker count to the
  work available (≥75,000 items per worker, calibrated from a measured
  worker sweep) and runs fully sequentially rather than building a
  single-worker pool. Total resolve time: **19.65s → 5.32s on
  spring-boot (3.7x), 288.40s → 176.99s on tensorflow**. Output is
  unchanged — worker count has never affected `map.json`, and a test now
  asserts that across every count the new logic can pick. This also
  explains why round 29's `--jobs 0` default flip underdelivered: the
  extra cores were being spent on index transfer, not resolution.

### Changed
- **`--jobs N` is now an upper bound rather than a target.** The work
  available can lower the actual worker count below what you asked for
  (e.g. `--jobs 11` on a repo whose call count justifies 3 workers runs
  3). `--jobs 1` still forces fully sequential as before. This affects
  wall-clock and worker counts only, never output.

## [0.43.53] — 2026-09-14

### Fixed
- **Incremental (non-`--full`) `dekko map` silently zeroed
  `throws`/`catches`/`env_reads`/`type_aliases` repo-wide** (round 29,
  Track 1, CRITICAL) — `cache.py`'s `_filemap_from_dict` manually
  listed the `FileMap` fields it reconstructed and was never updated
  for the four newer ones, so every file served from the extraction
  cache (i.e. every file *not* touched since the last run) came back
  with those fields empty. One ordinary edit-and-remap cycle was
  enough to make `query throws`/`query catches`/`query env` return
  confident false negatives for the whole repo, through all three
  entry points (bare CLI, daemon auto-regen, MCP `refresh_map`),
  while the run summary, `dekko status`, and symbol/call-edge counts
  all looked completely healthy. The on-disk cache always held the
  data (the write side serializes generically), so the fix is purely
  read-side; a new round-trip parity test asserts every `FileMap`
  field survives serialization, so a future field addition can't
  silently repeat this. Found independently on 5 of 7 eval repos in
  round 29; verified post-fix on claude-buddy, cline, spring-boot,
  zed, and tensorflow (baseline counts now survive incremental runs
  exactly). Maps generated by 0.43.30–0.43.52 should be regenerated
  (`dekko map --full`) once on this version; the version bump itself
  invalidates the extraction cache, so a plain `dekko map` after
  upgrading also fully re-parses.
- **The "no rev-cache for this commit ... may take a while"
  disclosure never reached a daemon-routed caller** (round 29,
  Track 2, HIGH) — with a daemon running, a first-touch
  `diff`/`affected`/`workset` against an uncached rev either had the
  note suppressed entirely (the daemon client's own `--jobs 0`
  override made the daemon-side check conclude "parallel, nothing to
  disclose") or buffered until the multi-minute resolve finished —
  either way, the silent-wait-that-looks-like-a-hang problem the
  round-15 note exists to prevent. The client now prints the
  disclosure itself, before dispatching the request, with wording
  that matches what will actually run (all-cores vs. sequential),
  sharing one message helper with the in-process path so the two
  can't drift. The detached daemon's stdout/stderr, previously left
  attached to the long-gone terminal that ran `daemon start`, now
  redirect to `.dekko/daemon.log`.
- **`dekko deps` on an all-Go repo led with a bare "0 resolved import
  edges"** (round 29, item 4a — requested three rounds running) — a
  new in-band note now explains when the zero comes from languages
  whose imports dekko deliberately doesn't resolve to in-repo files
  (Go's fully-qualified module paths), mirroring `query throws`'s
  existing scope-disclosure framing, in both text and `--json`
  output.
- **Coverage/symlink disclosure notes were missing from several read
  commands** (round 29, item 4b) — `deps`, `stats`, and `search`
  never surfaced the skipped-file coverage note at all, and `query
  env`'s text-mode success path never computed it (only its
  empty-result path did). All four now show the same note the other
  read commands already print.
- **`sanity --unused` never set `generic_name_caution` for
  collision-prone bare names like `error`** (round 29, item 4c) —
  the data-driven collision signal added in round 28 was wired into
  the `--all` sweep and the plain-target path but not into
  `--unused`'s own check, which still called the generic-name test
  with the collision flag hardcoded off. Now threaded through,
  matching the other call sites.

### Changed
- **`dekko map` now defaults to `--jobs 0` (all cores)** (round 29,
  Track 3, maintainer-approved) — previously the explicit `dekko map`
  invocation defaulted to sequential even though the auto-regen path
  every read subcommand uses on a stale map has requested all cores
  since round 11, so an incremental remap after a one-line edit on a
  large repo could run *longer* than a full parallel rebuild
  (tensorflow: 12m24s vs. 5m03s in round 29's measurements). With the
  flip, the same tensorflow edit-and-remap cycle takes 216s vs.
  243.6s for `--full --jobs 0` — incremental now beats the full
  rebuild everywhere measured (spring-boot 31.1s ≈ 31.0s, zed 22.1s
  vs. 25.1s). Pass `--jobs 1` for a sequential run; small repos and
  small deltas stay sequential automatically (the parallel pools only
  engage past existing size thresholds), so tiny-repo runs are
  unaffected (claude-buddy: 1.09s cold / 0.77s incremental, slightly
  faster than before).

## [0.43.52] — 2026-09-14

### Fixed
- **`dekko map <subdir-of-an-already-mapped-repo>` silently forked a
  second, orphan `.dekko/` root** (round 28, Track 6, LOW) — `dekko
  map`'s two positional arguments are `[DIR] [SUBPATH]`, so a single
  argument that happens to be a subdirectory of an already-mapped repo
  reads as "re-map just this subtree" but was instead treated as
  "start a brand-new, independent repo root," nested silently inside
  the subdirectory with no error or warning — spring-boot's report
  only noticed via `git status` surfacing the untracked nested
  directory. `dekko map` now detects this and refuses (exit 2) with a
  suggested corrected command; a new `--force-new-root` flag opts into
  the independent-root behavior explicitly, for the legitimate case (a
  vendored subproject deliberately mapped in isolation). A
  subdirectory that is itself a distinct git repo (a real submodule)
  is never flagged. Verified on spring-boot's real Gradle multi-module
  layout: `dekko map core/spring-boot` now rejects with a correct
  suggested command instead of silently creating
  `core/spring-boot/.dekko/`, while `dekko map . core/spring-boot`
  (the correct two-arg form) is unaffected.

## [0.43.51] — 2026-09-14

### Fixed
- **A symlinked source file was silently double-indexed, phantom-
  duplicating every symbol it defines** (round 28, Track 2,
  MEDIUM-HIGH) — `walker.discover()` had no symlink-handling code path
  at all, so a symlink to a sibling source file got its target's
  content fully re-parsed a second time under the symlink's own path,
  with matching line numbers and signatures — silently corrupting
  fan-in/ambiguity counts with no warning. Symlinked files are now
  skipped by default (reason `"symlink"`, reported in the run summary
  and a new `symlink_excluded` coverage note, mirroring the existing
  `too_large`/`vendored_excluded` notes), matching `git`/`ripgrep`
  convention. A new `--follow-symlinks` flag restores the previous
  behavior for callers who genuinely want it (e.g. npm/pnpm workspace
  symlinks), correctly invalidating a cached map when toggled. Verified
  independently on the two repos that reproduced this in round 28:
  claude-buddy (`buddyStateDir`) and spring-boot (`SpringApplication`)
  — reproducing the original phantom-ambiguity bug exactly via
  `--follow-symlinks`, confirming the default fix suppresses it, and
  confirming a byte-identical no-op on both repos' real, unmodified
  trees.

## [0.43.50] — 2026-09-11

### Fixed
- **Rev-cache corruption could silently persist a false "100% of repo
  changed" result** (round 28, Track 1, HIGH) — `diff`/`affected`/
  `workset` cached the historical-rev side of a comparison under
  `.dekko/rev-cache/<sha>.json` assuming a cache hit was always safe
  since a commit's tree is immutable, but a transient read failure
  while exporting the old tree silently produced an all-empty-string
  body-hash map that then compared as "everything changed" against the
  new side, and got cached forever. Three-layer fix: `revcache.save()`
  now refuses to persist a snapshot whose body map is entirely empty
  for a non-empty symbol set (logging a `note:` instead); `diff.
  old_snapshot()` now takes a per-SHA lock (`filelock.
  try_named_lock`, generalized from `try_regen_lock`) so two concurrent
  builds for the same rev serialize instead of racing; `diff.compare()`
  now warns to stderr whenever every shared symbol across a common set
  of 500+ reports changed with nothing added/removed, since that
  pattern also matches a pre-existing corrupted cache entry from before
  this fix. Verified against tensorflow, including reproducing the
  original symptom byte-for-byte against a hand-corrupted cache entry
  and confirming the new stderr warning fires.
- **`dekko deps` misresolved bare, repo-root-relative JS/TS imports as
  external** (round 28, Track 3, MEDIUM) — `_resolve_import_js` only
  resolved a bare specifier via a tsconfig/jsconfig path alias, so a
  repo-root-relative bare import with no governing alias (`import {
  ... } from 'src/bootstrap/state.js'`) fell straight to `external`
  even when the target file existed in the repo. Added a third
  resolution attempt, gated on the specifier containing a `/` (so a
  single-segment specifier like `'lodash'` still resolves to
  `external`, as a real npm package name should): try the bare
  specifier as a path relative to the repo root, using the same
  extension/index-file candidate ladder relative imports already use.
  Verified on claude-code: `dekko deps --file src/main.tsx`'s external
  count dropped from 41 to 10, matching the 34 real internal files the
  originating report identified.
- **`sanity`'s type-annotation classifier didn't cover Rust** (round
  28, Track 4, MEDIUM) — `CAUSE_TYPE_ANNOTATION` existed since round
  25 for TS/JS but excluded Rust's grammar entirely, so every Rust
  type-position hit (`impl Trait for Type`, turbofish `Type::<Concrete>`,
  a bare reference-type mention, a `-> Type` return position) fell
  through to `CAUSE_UNEXPLAINED`, making `sanity --all
  --fail-on-unexplained` unusable as a CI gate on Rust repos with any
  type-name reuse. Added `"rust"` to `_TYPE_ANNOTATION_GRAMMARS` plus
  five new templates covering `impl Trait for Type`, plain inherent
  `impl Type`, turbofish, `-> Type` return positions, and bare
  reference-type mentions inside nested parameter lists. Verified on
  zed: the master report's `NavHistory` repro went from 3/8 to 8/8
  grep-only hits correctly classified.
- **`sanity`'s generic-name caution relied on a static, hand-curated
  word list** (round 28, Track 5, MEDIUM) — `_is_generic_name` checked
  a fixed 28-word list even though the map already computes real,
  per-repo collision data via `dekko ambiguous`. `_is_generic_name` now
  also consults `ambiguous.collision_names()` (computed once per
  `sanity` invocation), additively: any name that has genuinely
  collided 2+ ways in this repo's own call graph now gets the
  directional caution, whether or not it's in the curated list. Also
  extended `_LOCAL_DECL_TEMPLATE`'s local-binding coverage to
  `catch (error) {` parameter bindings and bare interface/type field
  declarations (`error?: string;`), the same underlying "local
  binding, not a reference" shape. Verified on cline: all seven of the
  report's uncurated collision names (`resolve`, `close`, `invoke`,
  `dispose`, `clear`, `error`, plus the already-curated `delete`) now
  correctly report `CAUSE_GENERIC_NAME`.

## [0.43.49] — 2026-09-09

### Fixed
- **`TcpLoopbackTransport` port-file read race (CI flake)** — the port
  file was written with `Path.write_text()` (truncate-then-write, not
  atomic), so a reader could observe the file mid-write as empty or
  partial JSON, hit a `JSONDecodeError`, tear down, and then see "no
  port file at all" on a subsequent read — surfaced intermittently as
  `test_tcp_loopback_transport_accept_loop_parity` failing in CI
  (macOS/py3.10 leg). Fixed by writing the port file through the
  existing `atomic_write_bytes` helper (temp file + `os.replace()`,
  already used by `render/mapfile.py`), so a reader only ever sees
  "absent" or "fully valid," never partial. Verified with 30
  consecutive runs of the previously-flaky test.

## [0.43.48] — 2026-09-09

### Fixed
- **License-boilerplate divider lines still leaking into extracted
  purpose (round-27, Track 1 follow-up)** — the round-27 boilerplate
  regex fix closed the gap on spring-boot's header text but not on
  tensorflow's, whose header ends in a 78-character `=` divider line
  inside the same `/* ... */` comment block. A divider is pure
  punctuation, so no text regex can match it; `_comment_first_line`
  stopped there and surfaced the divider itself as the file's
  "purpose." Fixed structurally: `extractor.py` now recognizes and
  skips divider-only lines (repeated punctuation with no alphanumeric
  content) alongside the existing boilerplate-text regex, in both
  `_string_first_line` and `_comment_first_line`. See
  `test-repos/reports/27-round27-tokentest-7repo/TRACK1-TRACK5-REDESIGN.md`.
- **Fully-qualified Rust `std::`/`core::`/`alloc::` paths still not
  recognized as external (round-27, Track 5 follow-up)** — the
  round-27 fix added a multi-segment check to
  `_receiver_is_external()` in `resolver.py`, but it was dead code:
  `_heritage_rust_impl`/`_split_callee_text` in `extractor.py` already
  flattens a receiver like `std::fmt::Display` down to just `"std"`
  before the resolver ever sees it, so no `::` survives to split on.
  Fixed by reading the unflattened `call.text` instead (which still
  carries the full qualified path on `RawCall`/`RawHeritage`),
  splitting on the literal `::` separator and gating the check to
  Rust via `languages.spec_for_path` to avoid cross-language false
  positives. Confirmed on zed: `impl std::fmt::Display for SharedUri`
  now resolves `(external)` instead of colliding with an unrelated
  in-repo `Display` enum.

## [0.43.47] — 2026-09-08

### Fixed
- **`dekko unused` false positives on type-kind symbols used only in
  type position (round-27, Track 4)** — the default scan
  (`kinds="callables"`) evaluated every symbol kind, including
  interfaces/type aliases/enums/structs, against call-based evidence
  only; since types are never "called," a type-kind symbol used
  constantly as a field/param/return type but never invoked was
  reported unused unless `--kinds types` was passed explicitly.
  `_used_keys()` now always unions `_used_keys_types()`'s
  heritage/type-usage evidence into the default scan regardless of
  `kinds` (Option B from the round-27 design doc: same scan
  population, better evidence, no breaking change to `--kinds`
  scoping). `--kinds` help text and `TESTING-GUIDE.md` updated to
  match. See
  `test-repos/reports/27-round27-tokentest-7repo/TRACK4-OPTION-B-DESIGN.md`.

## [0.43.46] — 2026-09-08

### Fixed
- **License-boilerplate leak into extracted file purpose (round-27)** —
  `_BOILERPLATE_HEADER_RE` in `extractor.py` only recognized the first
  three lines of a standard Apache-2.0 header, so later header lines
  (the "obtain a copy," "AS IS," and "limitations under the license"
  lines) leaked through as the file's extracted "purpose," corrupting
  `outline`, `summary`'s directory rollup, and `workset`'s file
  listings. Confirmed on spring-boot (100% of 8,659 files) and
  tensorflow (10,733 occurrences). Regex extended to cover the full
  Apache-2.0 header plus the equivalent MIT and BSD-2/3-Clause
  boilerplate lines pre-emptively. See
  `test-repos/reports/27-round27-tokentest-7repo/`.
- **Stale rev-cache entries silently reported as phantom diffs
  (round-27)** — rev-cache entries in `revcache.py` carried no
  extractor-spec version stamp, so an entry built by an older dekko
  binary was served forever afterward, with `diff`/`workset`/`affected`
  comparing the live map against it and reporting schema drift as a
  genuine code change. Fixed by stamping entries with `spec_hash` (the
  same `spec_fingerprint()` mechanism `mapfile.py` already uses for
  `map.json` itself) and treating a mismatch as a cache miss.
- **`console.warn(...)`-style ambient-global calls misattributed as
  ambiguous (round-27)** — `_is_noise_call()` in `resolver.py` checked
  a call's method name against five existing denylists but never
  checked whether the receiver itself was a well-known ambient/global
  object (`console`, `process`, `window`, `document`, etc.), so
  `console.warn(...)` resolved against unrelated same-named free
  functions instead of being recognized as noise. Added an
  `_AMBIENT_GLOBAL_RECEIVERS` short-circuit.
- **Fully-qualified `std::`/`core::`/`alloc::` Rust paths not
  recognized as external without a local `use` binding (round-27)** —
  `_receiver_is_external()` only recognized a receiver as external via
  a local `use`-bound import, so a fully-qualified inline path like
  `impl std::fmt::Display for X` (which binds no `use std;`) fell
  through and silently resolved against an unrelated in-repo
  same-named symbol. Added a `_RUST_STD_NAMESPACE_ROOTS` short-circuit
  for `std`/`core`/`alloc` path roots.

## [0.43.45] — 2026-08-31

### Fixed
- **TypeScript `type X = ...` alias indexing (round-26)** —
  `type_alias_declaration` had no extraction query pattern, so no
  `Symbol` was ever produced for type aliases; only interfaces and
  classes counted as "types." `query_symbol`, `find_type_usages`,
  the heritage graph, `unused-types`, and `stats` were all blind to
  them despite type aliases being the dominant type-declaration
  idiom in real TS/TSX codebases. Found via round-26 fable-5 eval
  against `test-repos/claude-code` (2,484 unindexed aliases there).
  Fixed by adding a `type_alias_declaration` query pattern to
  `languages.py`, wiring `Symbol.kind = "type_alias"` through
  `extractor.py`/`model.py`, and adding it to `TYPE_KINDS` in
  `query.py`; every downstream consumer already gated generically on
  `TYPE_KINDS`, so the fix propagated automatically — heritage
  resolution now resolves real edges instead of falling back to
  `(unresolved)`, and `unused` correctly surfaces dead type aliases.
  See `.features/plans/round26/ts-type-alias-indexing.md`.

## [0.43.44] — 2026-08-31

### Fixed
- **`get_context_pack` false hop-2 callers/callees (round-26)** —
  `build_pack`'s BFS in `contextpack.py` threaded bare symbol ids
  through the frontier, so at hop ≥2 `_neighbors()` expanded both
  `calls_in` and `calls_out` regardless of which direction a node
  was reached in, pulling in a hop-1 caller's unrelated callees (or
  a hop-1 callee's unrelated callers) as spurious hop-2 neighbors.
  Found via round-25/26-style eval against `test-repos/zed`. Fixed
  by threading `(sym_id, direction)` tuples through the frontier via
  a new `_neighbors_in_direction()`, locking expansion to the
  reached direction at hop ≥2. See
  `.features/plans/round26/context-pack-false-callers.md`.

## [0.43.43] — 2026-08-31

### Added
- **TS/JS `tsconfig.json`/`jsconfig.json` path-alias resolution
  (round-25 plan 07, finding #6)** — `resolve()`/`resolve_imports()`
  gain an optional `root` param used to discover config files
  (`walker.find_config_files`), parse them with a new dependency-free
  JSONC-lite stripper, and build a scoped `paths`/`baseUrl` alias
  table with `extends`-chain merging (cycle-guarded) and nearest-
  scope monorepo precedence. `_resolve_import_js` now consults this
  table for wildcard/exact alias matches before falling back to an
  unresolved bare specifier. Verified against `test-repos/cline`:
  alias-based import edges went from 0 to 2,024 resolved on
  `apps/vscode/src`. Heritage's separate `_hint_match` alias gap is
  left as noted future work. See
  `.features/plans/round25/07-tsconfig-path-alias-resolution.md` and
  `test-repos/reports/25-fable5-7repo-eval/MASTER-REPORT.md`.

## [0.43.42] — 2026-08-31

### Added
- **Session-start hook hard ceiling on oversized maps (round-25 plan
  05)** — `SESSION_MAP_HARD_CEILING` (20,000 tokens) added to
  `session_start`: above the ceiling, the hook now emits a
  disclosure-only note instead of a truncated map body; between the
  ceiling and the existing round-13 `SESSION_MAP_BUDGET`, prior
  behavior is unchanged. See
  `.features/plans/round25/05-session-start-hook-token-cap.md` and
  `test-repos/reports/25-fable5-7repo-eval/MASTER-REPORT.md`. Closes
  out round 25's plan backlog (01-06, all implemented).

## [0.43.41] — 2026-08-31

### Fixed
- **Daemon cold-cache timeout under-parallelized on rev-cache misses
  (round-25 plan 04)** — on a genuine rev-cache miss for
  `diff`/`affected`/`workset`, if the caller never explicitly chose
  `--jobs`, the daemon now forwards `jobs=0` (all cores) via a copy
  of the request args, leaving the caller's original choice intact
  for any fallback-to-direct-execution path.
  `DaemonRequestAbandonedError` now carries the `jobs` value actually
  sent, so the abandonment message only suggests `--jobs 0` when the
  abandoned request ran single-threaded. See
  `.features/plans/round25/04-daemon-coldcache-timeout-parallelism.md`
  and `test-repos/reports/25-fable5-7repo-eval/MASTER-REPORT.md`.
  (Fix 3 from that plan is out of scope.)

## [0.43.40] — 2026-08-31

### Added
- **Arity-gated call resolution (round-25 plan 06, fix 2)** —
  `Param` now records `has_default`/`variadic`; a new
  `RawCall.arg_count`/`RawRef.arg_count` is captured across all 9
  Tier-1 language `call_query`s (Java given its own two-pattern
  edit); the resolver's single-candidate acceptance in
  `_pick_candidate` now gates on arity plausibility, falling through
  to ambiguous (with an empty candidate list, not the rejected one)
  when a call's argument count doesn't fit any overload's parameter
  shape. Tier-2/generic-grammar languages are out of scope. See
  `.features/plans/round25/06-structural-layer2-arity-resolution.md`
  and `test-repos/reports/25-fable5-7repo-eval/MASTER-REPORT.md`.

## [0.43.39] — 2026-08-31

### Fixed
- **JS/TS EventEmitter/EventTarget misattribution (round-25 plan 03,
  fix 1)** — added `on`, `once`, `off`, `emit`, `addListener`,
  `removeListener`, `addEventListener`, `removeEventListener`, and
  `dispatchEvent` to the resolver's builtin-method-name set, closing
  a false-positive call-resolution misattribution
  (`CdpClient.on`/`stderrStream.on(...)`) reproduced from cline. See
  `.features/plans/round25/03-single-candidate-misattribution-resolver.md`
  and `test-repos/reports/25-fable5-7repo-eval/MASTER-REPORT.md`.
  (Fix 2, structural layer-2 arity resolution, is deferred pending a
  separate design.)

## [0.43.38] — 2026-08-31

### Fixed
- **Rust crate-decoy tiebreak in import resolution (round-25 plan 02)**
  — `resolve_imports()` now builds its Rust crate-root index with the
  collision-aware `_rust_crate_roots_index_all` instead of a
  single-winner index, and a new `_prefer_non_synthetic_crate_root`
  (mirroring round 24's match-side fix) resolves bare-crate-name
  imports by preferring the self-crate, then the sole non-synthetic
  root, falling back to external rather than guessing when a genuine
  collision remains. See
  `.features/plans/round25/02-deps-crate-decoy-tiebreak.md` and
  `test-repos/reports/25-fable5-7repo-eval/MASTER-REPORT.md`.

## [0.43.37] — 2026-08-31

### Fixed
- **Sanity classifier taxonomy gaps (round-25 plan 01)** — closed four
  gaps in `dekko sanity`'s grep-miss classification: added Java/Kotlin
  import templates, TS/JS type-annotation detection, local-binding/
  string-literal detection, and deterministic cross-file bare-name
  collision detection (guarded to require 2+ distinct declaring files
  so single-declaration names aren't flagged). See
  `.features/plans/round25/01-sanity-classifier-taxonomy-gaps.md` and
  `test-repos/reports/25-fable5-7repo-eval/MASTER-REPORT.md`.

## [0.43.36] — 2026-08-31

### Fixed
- **Round-25 one-liner batch** — ten small correctness/UX fixes found
  during the round-25 7-repo eval, all fixed same-day. See
  `test-repos/reports/25-fable5-7repo-eval/MASTER-REPORT.md` for full
  per-finding detail:
  - `query --sites --json` now forwards `related_total`/`related_label`
    into the JSON payload, matching the text path.
  - `sanity` no longer flags a receiver as an import-mismatch when the
    hit's file is the declaring type's own file.
  - `query importers`'s not-found suggestions now use the same
    bare-import-source form as successful matches, instead of raw
    source text.
  - `unused --kinds` help text now accurately describes evidence-based
    dead-code detection instead of implying a functions/methods-only
    scan.
  - `unused --dispatch`'s `check_command` and text hint now emit a
    disambiguated `path:qualname:line` target.
  - `sanity` no longer misclassifies a header-only C/C++ declaration
    (prototype) as a call.
  - `workset`/`summary`/`orient`'s shared file-level doc extraction now
    skips leading copyright/license boilerplate before picking a
    description.
  - `hooks uninstall` now removes `.claude/settings.json` (and the now-
    empty `.claude/` dir) when nothing but dekko's own hooks remain,
    instead of always rewriting an empty-ish file.
  - `hooks install`/`uninstall` now detect and preserve the existing
    indent style of `.claude/settings.json` instead of always
    rewriting it with 2-space indentation.
  - `query cohesion --budget` now prints a floor-exceeded note to
    stderr when the requested budget is below the result's token
    floor, mirroring `lean --budget`'s disclosure.

## [0.43.35] — 2026-08-28

### Fixed
- **Daemon-routed command timeouts under-provisioned on large repos**
  — `_TIMEOUT_BYTES_PER_SECOND` in the daemon's cold-cache timeout
  scaling was recalibrated from 5.5M to 1.1M bytes/sec, based on
  round-24's tensorflow measurement, so large repos get realistic
  timeout budgets instead of premature failures. See
  `.features/plans/round24/12-daemon-timeout-messaging-followup.md`.

## [0.43.34] — 2026-08-28

### Changed
- **Output self-disclosure hints** — round 24 found agents missing
  built-in disambiguation tools because nothing in the primary
  output pointed at them:
  - `query symbol`'s fan-in line now notes `--sites`/`sanity` when
    fan-in is nonzero.
  - `dekko context` gains `--all-imports` to skip the relevance
    filter; the "+N more imports" line now names the filter
    criterion and the flag that bypasses it.
  - `dekko sanity` gains `--group-by-file`, rolling up grep-only
    mismatches by file with a cause breakdown.
  - `--min-shared`'s help text now suggests lowering it to 1 on
    small repos.
  See `.features/plans/round24/11-output-self-disclosure-hints.md`.

## [0.43.33] — 2026-08-28

### Changed
- **`dekko deps` accepts a `FILE` positional as an alias for
  `--file`** — matches the verb-placement convention used elsewhere
  in the CLI; giving both `FILE` and `--file` is a usage error. See
  `.features/plans/round24/10-cli-verb-placement-consistency.md`.

## [0.43.32] — 2026-08-28

### Changed
- **MCP tools accept `name` as an alias for `symbol`** —
  `query_symbol`, `get_callers`, `get_callees`, `get_supertypes`,
  `get_subtypes`, and `add_note` now accept either argument key;
  `symbol` takes precedence if both are given. Round 24 found agents
  guessing `name` for these tools since it's the more common MCP
  convention. See
  `.features/plans/round24/09-mcp-tool-arg-naming.md`.

## [0.43.31] — 2026-08-28

### Changed
- **`dekko export --scope {symbol,file}` renamed to `--granularity`**
  — round 24's 7-repo eval found 4 of 7 sessions guessed `--scope`
  meant "scope the graph to one symbol" (what `dekko context` does)
  rather than its actual meaning, node granularity for the whole
  rendered graph. `--granularity` is now the primary flag; `--scope`
  remains a hidden, fully functional alias that still works but emits
  a stderr deprecation notice. See
  `.features/plans/round24/08-export-scope-rename.md`.

## [0.43.30] — 2026-08-28

Fixes and small improvements from the round-24 7-repo eval
(`test-repos/reports/24-tokentest-7repo-post04328/MASTER-REPORT.md`);
see `.features/plans/round24/` for the design docs behind each item.

### Fixed
- **C++ "most vexing parse" constructor-argument calls were dropped**
  — `Type name(Ctor(), deleter);` (idiomatic RAII construction, e.g.
  `std::unique_ptr<TF_Status, D> s(TF_NewStatus(), del);`) misparses
  under tree-sitter-cpp's most-vexing-parse ambiguity as a local
  function declaration, silently dropping the constructor call from
  the call graph. A second extraction pass recovers these calls for
  `c`/`cpp` files. See
  `.features/plans/round24/01-cpp-vexing-parse-ctor-calls-dropped.md`.
- **Daemon-routed `diff`/`affected`/`workset` timeouts under-provision
  on a cold rev-cache build** — the client timeout was scaled off
  `map.json`'s byte size, a proxy that's stale and wrong for a
  rev-cache miss, whose cost tracks the target rev's git-tracked file
  count instead. On tensorflow this under-provisioned the timeout
  ~5.3x, making the daemon path strictly worse than `--no-daemon`.
  The timeout is now scaled by the target rev's tracked-file count
  on a genuine rev-cache miss; hits and all other commands are
  unchanged. See
  `.features/plans/round24/02-daemon-cold-revcache-timeout-miscalibration.md`.
- **Heritage resolver misattributed a Rust trait to a decoy
  fixture/vendor crate** — `query subtypes`/`supertypes` couldn't
  distinguish a real workspace crate root from a same-named
  fixture/vendor crate, causing near-total under-resolution on
  repos like zed (`gpui::Render`, 1/383 implementors found). A
  crate-root collision now resolves to the non-fixture/vendor
  candidate when exactly one side qualifies, and discloses when the
  tiebreak fired via a new `heritage_synthetic_tiebreak_count` note
  on `query subtypes`/`supertypes` output (bumps the map schema to
  v11). See
  `.features/plans/round24/03-heritage-crate-decoy-tiebreak.md`.
- **`dekko sanity`'s comment-mention check missed file-header
  mentions far from the symbol's definition** — a module-header
  comment naming a symbol tens of lines before its definition fell
  outside the existing 3-line adjacent-comment proximity gate,
  producing a false "unexplained miss" (confirmed on
  claude-buddy's `path.ts`/`buddyStateDir`). A new
  `_in_leading_header_comment()` check recognizes an uninterrupted
  comment run from line 1 through the hit line as a legitimate
  module-header mention, alongside the existing adjacent-comment
  check. See
  `.features/plans/round24/07-sanity-comment-mention-file-header-gap.md`.

### Added
- **`dekko unused --dispatch`** — `dekko unused` false-positives on
  polymorphic `this.method()`/`self.method()` dispatch, since the
  static resolver can't always bind a dynamic-dispatch call to its
  implementation. `sanity --unused` already caught these, but agents
  skip that step. `unused` now surfaces an always-on advisory caveat
  count for flagged symbols that are also unresolved dispatch
  candidates (reusing the existing `ambiguous_in` table `--suspect`
  is built on), plus an opt-in `--dispatch` section listing them. See
  `.features/plans/round24/04-unused-dispatch-shaped-candidate-flag.md`.

### Docs
- Clarified in `test-repos/TESTING-GUIDE.md`: `query importers`
  footer-arithmetic spot-checks must exclude the footer line itself
  (`Meter`'s counters are algebraically tied and can't drift; the
  apparent mismatch came from a bare `wc -l`), and `--json` on
  ambiguous/not-found error paths intentionally stays plain-text on
  stderr project-wide (documented in `docs/cli.md` and
  `report_unresolved`'s docstring, not a bug). Both were re-filed
  non-bugs from round 23; the notes should stop the re-filing. See
  `.features/plans/round24/05-query-importers-footer-arithmetic-recheck.md`
  and `.features/plans/round24/06-ambiguous-json-error-contract-recheck.md`.

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
