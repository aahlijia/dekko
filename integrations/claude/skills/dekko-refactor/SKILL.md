---
name: dekko-refactor
description: List every site a rename, move, or signature change must touch before editing, then prove nothing was missed after. Trigger when about to rename a function, method, class or type, rename or move a file or module, change a parameter list or return type, move a symbol between files, or remove a symbol, in a repo with a `.dekko/` directory; and again after those edits, before running tests. dekko's call graph has the exact call sites, type annotations, implementors and import lines, so this replaces grepping for the old name and reading each hit.
---

# Refactoring with dekko (find every site first)

A rename or signature change fails in one way: a site nobody listed.
Grep for the old name over-matches strings and comments and misses
aliased imports; dekko's map has the exact call, annotation, heritage
and import edges. List the sites from the map, edit only those lines,
then ask the map what still points at the old name. Tool names below
are the bare MCP names (`dekko-orient` has the prefixes, the CLI
fallback, and target syntax).

## Before editing: the site list

Pin the target first with `query_symbol` (append `:LINE` if the reply
says it's an overload set) and read its `note:` lines. Then list the
sites for the kind of change:

| Change | Sites to list | Use |
|---|---|---|
| Rename or re-sign a function/method | every call site | `get_callers` with `sites=true include_tests=true` (CLI `dekko query callers <sym> --sites`) |
| Rename or reshape a class/struct/interface/trait | annotations that name it, implementors, constructor calls | `find_type_usages <T>`, `get_subtypes <T>` with `transitive=true include_tests=true`, `get_callers` as above; or one `workset symbol=<T> type_impact=true` (CLI `--symbol <T> --type-impact`) |
| Rename or move a file/module, or rename an exported name | import lines, including aliases | CLI only: `dekko deps --file <path>` (which files import it), `dekko query importers <import.source>` (one row per imported member, with `(as alias)`) |
| Change what it raises | handlers that catch it | CLI only: `dekko query catches <ExcType>`, `dekko query throws <sym>` |

Rules that make the list complete:

- **`include_tests=true` is mandatory here.** The MCP defaults hide
  test callers and test subtypes; a test is a site to update.
- **No omitted rows.** Pass `budget=0` or raise it until the footer
  stops saying `omitted`. An omitted row is a missed edit.
- **`importers` takes an import string** (`pkg.util`, `./utils`,
  `org.foo.Bar`), not a path; `deps --file` takes the path. A row
  ending `(as fn)` means that file calls the symbol as `fn`: the
  import line and those calls are both sites.
- **Read only the sites.** `get_context_pack` with `with_source=true`
  for the target's own body, a line-ranged Read for each site row.
- A low count on a widely used or short, common name: run
  `check_ambiguous` or `dekko-verify` before trusting it.

## After editing: prove nothing was missed

Every tool regenerates the map from your edits before answering, so
ask it directly:

1. **`find_usages <old name>`** (CLI `dekko query uses <old>`). A
   call to a name that no longer exists in the repo is recorded as an
   external call, so this lists exactly the direct-name sites still
   to fix. Empty means done for direct calls. For a signature change
   with no rename, re-run `get_callers` with `sites=true
   include_tests=true` and check each row against the new signature
   instead.
2. **`dekko query importers <old.source>`** for import lines still
   naming the old module or member. This is the only place an aliased
   import shows up: `find_usages` sees the alias's calls under the
   alias, not the old name.
3. **`impacted_tests`** (CLI `dekko affected`) and run the command it
   prints. `dekko affected --possible` adds tests that reach the
   change only through an ambiguous call.
4. **`dekko note list --orphaned`**: a note keyed to the old id
   survives the rename as an orphan. Re-anchor or remove each one, per
   `dekko-notes`.
5. **One grep for the old name in what dekko doesn't model:** docs,
   comments, strings, config keys, CLI flags, serialized field names,
   `getattr`/registry-by-name lookups. This grep is for text, not for
   call sites; the map already covered those.

## Boundaries

- dekko lists sites and checks the result; it doesn't perform the
  edit. This is not an IDE rename.
- Calls through trait/interface dispatch, `dyn Trait`, or a name built
  at runtime don't appear as call edges; when the target is such a
  method, treat the list as a floor and see `dekko-verify`.
- `find_usages` records calls only. A missed *type annotation* after a
  type rename shows up in the language's type checker or compiler, not
  in step 1; run that too.
