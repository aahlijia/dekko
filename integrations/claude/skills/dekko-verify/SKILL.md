---
name: dekko-verify
description: Sanity-check a suspiciously low or zero call-graph result from dekko (get_callers, get_callees, find_usages, impacted_tests, unused) before concluding "no callers" or "dead code," or a heritage/throws-provenance result (query supertypes, query subtypes, query throws) that labels something (external) when you expect it to be in-repo. Trigger whenever such a result looks surprising given the symbol's apparent importance, before deleting/renaming a symbol based on a zero-caller result, when the repo mixes languages/has any unsupported-language files, or when an (external)/(unresolved) label lands on a name you're confident is first-party code.
---

# Verifying a low-confidence dekko answer

dekko's call-graph resolution usually fails as a **confident wrong
answer**, not a visible error, and "0 callers" is easy to over-trust.
This skill catches that before it deletes live code or hides a real
impact. It covers resolved relationships (callers, callees, usages,
impacted tests, `unused`, heritage, throws), not `outline`,
`query_symbol` or `search_code`, which only describe what exists.

## How to check

Run `dekko sanity` (the `/sanity` command). It compares dekko's answer
with one targeted grep sweep, buckets the results into matches /
dekko-only / grep-only, and names a likely cause for every grep-only
miss:

```
dekko sanity <symbol>              # a get_callers result
dekko sanity <name> --usages       # a find_usages result
dekko sanity --unused <symbol>     # a `dekko unused` "dead" verdict
```

One run is enough. Don't read whole files or rebuild the call graph by
hand; that defeats the point of dekko.

## When to double-check

Spot-check, don't re-verify everything, when any of these apply:

- **Dynamic dispatch or an unusual qualified call.** Trait/interface
  dispatch (Rust `dyn Trait`, Java/Kotlin interface methods) is the
  main blind spot: the resolver reliably matches only an explicit
  `Type::method()` / `Type.method()`. Qualified calls through a
  same-repo namespace or module usually resolve, but one routed
  through a re-export or alias can still drop.
- **A low count with no ambiguity disclosure.** A real ambiguous call
  says so (`N call(s) resolved ambiguously`). A widely used symbol
  with a low count and *no* disclosure is more suspicious than one
  with it.
- **Unsupported or partially parsed files.** `dekko stats` or the map
  summary notes them. A symbol called only from an unparsed file
  reads as zero-caller.
- **`get_callers`' default test filter.** It hides test callers (and
  says so in a footer). Pass `include_tests=true` (CLI: leave
  `--no-tests` off) before concluding dead code.
- **A generic short name in a dense repo** (`new`, `then`, `map`,
  `iter_mut` in a 10k+-symbol repo): treat the count as directional.
- **Deleting or renaming on `dekko unused`'s word.** A callback passed
  by reference or a call from an unparsed file reads as unused. A row
  ending in `[dispatch?]` (`dispatch_candidate` in `--json`) shares
  its name with an interface/trait method or is a getter/handler read
  as a property: treat it as alive until `sanity --unused` says
  otherwise. `--unused` tags each piece of evidence by shape
  (`spread`/`typeof`/`subscript`/`call`/`other`).
- **A thin `impacted_tests` / `dekko affected` list.** It follows
  resolved calls only; tests reached through an ambiguous call are
  counted in a `note:` line, and `dekko affected --possible` lists
  them.
- **An `(external)` or `(unresolved)` label on a name you know is
  first-party**, from `query supertypes`/`subtypes` or `query throws`.
  A syntax shape the extractor doesn't model yet can mislabel an
  in-repo entry rather than drop it. `sanity` doesn't cover this;
  check with `dekko query symbol <name>` or one `grep -rn <name>`.

## What "good" looks like

If the sweep agrees with dekko, or finds nothing dekko missed, trust
the structural answer and move on. Most answers on supported languages
with unambiguous calls are correct; this is for the conditions above,
not a blanket "always grep after dekko."
