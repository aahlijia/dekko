---
name: dekko-verify
description: Sanity-check a suspiciously low or zero call-graph result from dekko (get_callers, get_callees, find_usages, impacted_tests, unused) before concluding "no callers" or "dead code." Trigger whenever such a result looks surprising given the symbol's apparent importance, or before deleting/renaming a symbol based on a zero-caller result, or when the repo mixes languages/has any unsupported-language files.
---

# Verifying a low-confidence dekko answer

dekko's call-graph resolution is real but conditional — repeated
hands-on evaluation rounds (see this repo's own
`test-repos/reports/`) keep finding the same failure shape: a
**confident wrong answer**, not a visible error. A caller trusts "0
callers" more than it should. This skill exists to catch that before
it leads to deleting live code or missing a real impact.

## When to double-check before trusting a result

Reach for one targeted `grep -rn <name>` (not a full re-read) as a
sanity check — not a full re-verification — when any of these apply:

- **A cross-package/cross-module qualified call is involved.**
  `pkg.Func()`-style calls (Go), `namespace::func()` (C++), or any
  call where the receiver is a same-repo package/module rather than a
  local variable are a known resolver blind spot — confirmed missing
  4 real call sites on a live repo as recently as this project's own
  round-13 eval. Same caution applies to trait/interface dispatch
  (Rust `dyn Trait` calls, Java/Kotlin interface methods) — the
  resolver ladder only reliably matches an explicit `Type::method()`
  or `Type.method()` form.
- **The result doesn't disclose ambiguity.** A real ambiguous call
  should say so (`N call(s) resolved ambiguously`), not just be
  silently absent from the count. If a symbol you expect to be widely
  used shows a low count with *no* ambiguity disclosure, that's more
  suspicious than a low count *with* one.
- **The repo has any unsupported/partially-parsed language files** —
  check `dekko stats` or the map-build summary for an "unsupported"
  note. Files dekko can't parse are tracked (not silently dropped),
  but a symbol only ever called from an unparsed file will still read
  as zero-caller.
- **`get_callers`/`get_callees` used their default `--no-tests`
  filter.** An empty result may just mean "no *non-test* callers" —
  check whether `include_tests`/`--include-tests` was applied before
  concluding dead code.
- **A dense-repo common short method name** (`new`, `then`, `map`,
  `iter_mut`, or similarly generic names in a 10k+-symbol repo) —
  resolver precision degrades under high symbol density; treat a
  count from these as directional, not exact.
- **You're about to delete or rename based on `dekko unused`'s
  dead-code list.** Same blind spots apply; a callback passed
  by reference rather than called directly, or a call from an
  unparsed file, can both read as "no inbound calls."

## What "good" looks like

If the grep sanity check agrees with dekko's count (or turns up
nothing dekko missed), trust the structural answer and move on —
this is a spot check, not a mandate to re-verify every query. Most
dekko answers on supported languages with unambiguous calls are
correct; this skill is for the specific conditions above, not a
blanket "always grep after dekko."

## Boundaries

- This is about **call-graph relation tools** (`get_callers`,
  `get_callees`, `find_usages`, `impacted_tests`, `unused`) —
  `outline`/`query_symbol`/`search_code` describe what's in the repo,
  not what calls what, and don't share this failure mode the same
  way.
- One grep, scoped to the symbol name, is enough to sanity-check —
  don't fall back to reading whole files or re-deriving the call
  graph by hand; that defeats the point of using dekko at all. See
  `dekko-orient` for the general "reach for dekko before grep" guidance
  this skill is a narrow exception to.
