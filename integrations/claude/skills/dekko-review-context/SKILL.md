---
name: dekko-review-context
description: Give a code-review or PR-description flow a structural head start before reading a diff line by line. Trigger when the user asks for a PR description, a pre-merge summary, or invokes a code-review flow (`/code-review`, `/review`, or an equivalent) against a diff in a repo with a `.dekko/` directory.
---

# Structural context before reviewing a diff

A reviewing agent that reads a diff cold misses what a call graph
already knows: what else calls the changed code, what tests should
run, and where dekko's own resolver is unsure. `workset`,
`impacted_tests`, and `check_ambiguous` already answer these — this
skill composes them into review context, run *before* the diff itself,
so the read is top-down (structure first, then lines) instead of
bottom-up.

## Procedure

| Need | Use |
|---|---|
| What changed and what calls it | `mcp__dekko__workset [rev]` (or `dekko workset [REV]`) |
| What tests should run | `mcp__dekko__impacted_tests [rev]` (or `dekko affected [REV]`) |
| Where the resolver itself is unsure | `mcp__dekko__check_ambiguous` (or `dekko ambiguous`) |
| Just the changed-symbol list with callers, no outlines | `dekko diff [REV]` (CLI only) |

1. **Determine the diff scope.** A git rev range, or the same default
   `workset`/`impacted_tests` already use: the commit the map was
   generated at, else `HEAD`. Don't ask the user to specify this
   unless the default is ambiguous for the request.
2. **Call `workset [rev]`.** Bundles touched-file outlines plus
   call-graph packs for the most central touched symbols under one
   token budget — this becomes the "what changed and what calls it"
   section of the review context. Pass `task="<what the PR claims to
   do>"` so ranking favors the symbols the description is about. If
   a touched symbol is a class/interface/struct/trait whose shape
   changed, follow up with `workset symbol=<Type> type_impact=true`
   (CLI `--symbol <Type> --type-impact`) to pull in every type-usage
   site and implementor, not just direct callers.
3. **Call `impacted_tests [rev]`.** Reverse call-graph reachability
   from the changed symbols, more reliable than grepping test files
   for the changed symbol's name — this becomes the "what tests
   should run" section. If it ends with a note counting tests that
   reach the change only through an ambiguous call, carry that count
   into the section as "may also be impacted" (`dekko affected
   --possible` lists them).
4. **Call `check_ambiguous`.** It's repo-wide, not diff-scoped, so
   cross-reference its top-colliding names/files against the touched
   symbols from step 2 yourself. Any overlap gets flagged explicitly
   in the review output: "dekko's resolver disclaims confidence near
   `<file>`/`<name>` — verify this part of the diff by hand," rather
   than silently trusting a clean-looking diff in a resolver-weak
   area.
5. **Fold all three into the review/PR-description prompt before
   reading the diff line by line.** Present structural context first,
   then read the actual diff — not appended as an afterthought.

## Boundaries

- This does not replace `/code-review` or an equivalent review flow — it's a context *supplement* that fires before or
  alongside one, the same way `dekko-orient` supplements normal
  navigation without owning it. It composes into whatever review/PR
  flow is already invoked, not a review output format of its own.
- Structural context, not a substitute for reading the diff — use it
  to prime the read, not to skip it.
- `check_ambiguous` is repo-wide; the cross-reference against touched
  symbols is reasoning you do, not a tool that already joins the two.
