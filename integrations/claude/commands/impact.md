---
description: Which tests a change impacts, with a ready-to-run test command
argument-hint: "[REV] [--possible]"
allowed-tools: Bash(dekko:*)
---

## Impacted tests

!`dekko affected $ARGUMENTS`

## Your task

The report above was generated programmatically by `dekko affected`
(reverse call-graph reachability from the changed symbols, plus an
import-edge fallback). Do NOT re-derive it by grepping test files for
the changed names.

1. Relay the headline (`N impacted test files vs REV`) and the files
   grouped by the tier tag printed in brackets: `[direct]` calls
   changed code itself, `[transitive]` reaches it through the call
   graph, `[import]` only imports a changed file. Name one or two of the
   impacted symbols per file where that helps the user see why.
2. The line(s) after the list that start with a test runner
   (`pytest ...`, `cargo test ...`, `go test ...`, the repo's
   `package.json` test script, `./gradlew ...`, `mvn ...`) are the
   ready-to-run command. Show them verbatim in a code block. If the
   user asked to run the tests, run that command; otherwise offer to.
3. If a runner line ends with `# +N more impacted test files not
   shown`, the report was budget-capped and the command is incomplete.
   Rerun `dekko affected $ARGUMENTS --budget 0` and take the command
   from that output before running anything.
4. `no impacted tests vs REV` means no changed callable reaches a
   test file. Say so plainly. If the user expected a change to show,
   the default REV is the commit the map was generated at (else
   `HEAD`), so a change already committed needs an older base:
   suggest `/impact <base-branch>` or `/impact HEAD~N`.
5. A `note:` counting tests that reach the change only through an
   ambiguous call: relay the count as "may also be impacted" and say
   that `/impact REV --possible` lists them, nearest the changed code
   first. They never join the command line.
6. An error naming the rev (`cannot export git rev ...`) means an
   unknown rev or not a git repo: relay it and ask for a valid rev.
7. These are leads, not verdicts: static analysis misses fixtures and
   dynamic dispatch. If the list looks thin for the size of the
   change, say that and point at the `dekko-verify` skill.
