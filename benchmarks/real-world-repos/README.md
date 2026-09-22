# Real-world token-efficiency benchmarks

A hands-on benchmark: dekko's MCP tools vs. a plain `Read`/`Grep`
workflow ("the old way"), run against **7 real, unmodified open-source
repositories** under `test-repos/` — no synthetic fixtures. Each repo
got an independent pass over a handful of realistic tasks (repo
orientation, outlining a large file, tracing a symbol's callers/callees,
searching for an external API's usage sites, bundling a change's
context). Both sides were measured in tokens (`chars/4`, cross-checked
against dekko's own self-reported estimates, which consistently matched
within a few percent).

This page carries two measurements:

- **Current numbers (dekko 0.43.77)**, the next section: the
  2026-09-21/22 re-measurement from evaluation rounds 33 and 34, on the
  same seven repos at their current size. This is the headline.
- **The original 2026-08-03 study** (everything from "Repos tested"
  down, and the seven per-repo write-ups linked there): run on a
  0.20-era dekko, before the fixes of eval rounds 07 through 34. Kept
  because its "where the win is smallest" and "what predicts the ratio"
  analysis still holds, and because its correctness caveats document
  failure shapes worth knowing. Its numbers are that snapshot, not the
  current state.

## Current numbers (dekko 0.43.77)

Measured in rounds 33 (0.43.71) and 34 (0.43.77) of the evaluation
program, 2026-09-21/22, one independent agent per repo, `chars/4`
tokens, same task shapes as the original study. Repo scale at the time
of measurement:

| Repo | Language | Files | Symbols |
|---|---|---:|---:|
| awesome-go | Go | 10 | 89 |
| claude-buddy | TypeScript | 57 | 687 |
| claude-code | TypeScript/TSX | 1,900 | 17,779 |
| cline | TypeScript (monorepo) | 2,730 | 21,884 |
| spring-boot | Java (monorepo) | 9,942 | 69,423 |
| tensorflow | Polyglot (Python/C++/…) | 14,285 | 171,706 |
| zed | Rust (workspace) | 2,177 | 60,406 |

| Task type | Repo | dekko tokens | Read/Grep tokens | Ratio |
|---|---|---:|---:|---:|
| Repo orientation (`summary`) | awesome-go | 359 | ~16,328 (read every `.go` file) | ~45x |
| Repo orientation (`summary`) | claude-buddy | ~349 | ~113,514 (read every source file) | ~325x (extreme baseline; a disciplined agent reading 5-6 entrypoints lands at 15-30x) |
| Repo orientation (`summary`) | cline | ~839 | thousands, still no hotspot/fan-in data | order of magnitude |
| Repo orientation (`summary`) | tensorflow | 917 | 5,000+, incomplete | qualitative: hotspots, entry points, and the 46%-ambiguous caveat in one call |
| Outline a large file | awesome-go (`main.go`) | 492 | 4,671 | ~9.5x |
| Outline a large file | claude-buddy (`cli/buddy-shell.ts`) | ~672 | ~7,328 | ~10.9x |
| Outline a large file | claude-code (`src/screens/REPL.tsx`, 5,005 lines) | ~1,154 | ~223,963 | **~194x** |
| Outline a large file | cline (`SdkController.ts`, 84 KB) | 1,803 | ~21,023 | ~11.7x |
| Outline a large file | spring-boot (`SpringApplication.java`, full outline) | 4,193 | 17,580 | ~4.2x (76%; round 33's "89%" was a budget-truncated outline) |
| Outline a large file | tensorflow (`direct_session.cc`, 2,186 lines) | 1,711 | ~21,112 | ~12.3x |
| Outline a large file | tensorflow (`eager/backprop.py`, 1,345 lines) | 946 | ~12,574 | ~13.3x |
| Outline a large file | zed (`gpui/src/window.rs`, 425 symbols) | ~4,756 | ~69,753 | ~14.7x |
| Outline a directory | zed (`crates/git_ui/src`, 32 files) | ~5,219 | ~418,520 | ~80x |
| Callers of a symbol (`--sites`) | awesome-go (`Generate`, same-named method nearby) | 79 | 5,227 | ~66x, and correct where grep conflates |
| Callers of a symbol (`--sites`) | claude-buddy (`generateBones`) | ~298 | ~756 (grep, 39 raw hits) | ~2.5x, plus separating 6 real calls from 23 test assertions |
| Callers of a symbol (`--sites`) | claude-code (`errors.ts:errorMessage`) | ~602 | ~18,521 (grep, 323+ raw hits) | ~30.8x |
| Callers of a symbol | cline (`Controller.postStateToWebview`, fan-in 77) | 786 default / 2,762 all 77 rows | ~6,563 (grep, 245 raw hits) | ~8x / ~2.4x |
| Callers of a symbol | tensorflow (`convert_to_tensor`, one of two overloads) | 89 | ~64,563 (grep, 2,328 raw hits) | ~700x, not like-for-like: dekko answers "which overload" and grep cannot |
| Callers of a symbol (`--sites`) | zed (`MultiWorkspace.new`, 22 sites in 8 files) | ~809 | ~381,446 (read the 8 caller files) / ~636 (naive grep) | ~471x vs. reading; parity vs. grep, but call-graph-correct |
| External-API usage | claude-code (`chalk`, namespace-style import) | ~798 | ~8,099 (grep `chalk.`) | ~10.2x; before 0.43.75 this query returned nothing at all |
| Bundled context (`workset`) | awesome-go (`ToHTML`) | 263 | 4,104 (file + 4 impacted tests) | ~15.6x |
| Bundled context (`context`) | cline (`postStateToWebview`, 1 hop) | ~770 | unbounded (the 84 KB file plus several caller files) | — |

Round 33 summarized the range as roughly 80-98% savings on small repos
and 16-275x on large ones; round 34 as 10x-325x depending on task and
repo. The shape from the original study holds: outline ratios scale with
file size, call-graph ratios scale with how noisy the same-named grep
is, and the floor is a small, grep-friendly local symbol (about 2.5x).
Full per-task detail, timings, and the six Low findings of round 34 are
in `test-repos/reports/33-*/` and `34-*/` (local, not tracked in git).

This is distinct from `benchmarks/measure.py` (the synthetic
regression harness that runs on every `pytest` invocation, `benchmarks/README.md`
above this directory). That harness keeps the core G★ invariant
("dekko costs less than the whole-file baseline") falsifiable on a toy
repo; **this** directory is the one-off study on real, large,
messy codebases, several orders of magnitude bigger than anything the
regression suite touches.


## The original study (2026-08-03, dekko 0.20-era)

Everything below is the first measurement, kept for its analysis. Read
the numbers as a historical snapshot; the current ones are above.

### Repos tested

| Repo | Language | Files | Symbols |
|---|---|---:|---:|
| [awesome-go](awesome-go.md) | Go | 10 | 89 |
| [claude-buddy](claude-buddy.md) | TypeScript | 57 | 662 |
| [claude-code](claude-code.md) | TypeScript/TSX | 1,902 | 15,360 |
| [cline](cline.md) | TypeScript (monorepo) | 2,730 | 19,542 |
| [spring-boot](spring-boot.md) | Java (monorepo) | 9,647 | 66,458 |
| [tensorflow](tensorflow.md) | Polyglot (Python/C++/…) | 14,285 | 157,845 |
| [zed](zed.md) | Rust (workspace) | 2,178 | ~60,000 |

Each repo's write-up (linked above) has the full per-task table and
notes; this page is the cross-repo summary.

## Cross-repo results, by task type

| Task type | Repo | dekko tokens | Old-way tokens | Ratio |
|---|---|---:|---:|---:|
| Repo orientation (`summary`) | awesome-go | 308 | ~15,271 | ~50x |
| Repo orientation (`summary`) | claude-buddy | 683 | ~4,925 | ~7.2x |
| Repo orientation (`summary`) | claude-code | ~600 | ~1,521 (no fan-in/entrypoint data at any cost) | not apples-to-apples — strictly superior |
| Repo orientation (`summary`) | cline | 1,202 | ~4,020 (`find -maxdepth 3` alone: 7,546) | ~3.3x |
| Repo orientation (`summary`) | spring-boot | ~1,957 | ~10,000–20,000+ | ~5–10x |
| Repo orientation (`summary` vs. `cloc`) | tensorflow | ~1,987 | ~1,075 (`cloc`, LOC-only, no structure) | old way cheaper in raw tokens, qualitatively inferior |
| Repo orientation (`summary`) | zed | ~1,981 | n/a (no equivalent measured) | — |
| Outline a large/central file | awesome-go (`main.go`) | 492 | 4,670 | ~9.5x |
| Outline a large/central file | claude-buddy (`tui.tsx`) | 1,036 | 23,459 | ~22x |
| Outline a large/central file | claude-buddy (`index.ts`, callback-heavy) | 43 | 23,459 | misleading — outline hid real content |
| Outline a large/central file | claude-code (`main.tsx`) | 1,017 | 200,981 | **~197x** |
| Outline a large/central file | cline (`SdkController.ts`) | 1,803 | 21,037 | ~11.7x |
| Outline a large/central file | spring-boot (`SpringApplication.java`) | 1,970 | 17,580 | ~8.9x |
| Outline a large/central file | tensorflow (`ops.py`, partial, 8% coverage) | 4,545 | 58,677 | ~12.9x (incomplete) |
| Outline a large/central file | zed (`editor.rs`) | 1,996 | 115,109 | ~58x |
| Outline a small file | awesome-go (test file) | 167 | 2,059 | ~12.3x |
| Outline a small file | spring-boot (`AutoConfigurations`, via context pack) | 795 | 724 | old way cheaper |
| Symbol lookup + callers/callees | awesome-go (`fetchProjectMeta`) | 176–294 | 1,539–6,000+ | ~8.7x+ |
| Symbol lookup + callers/callees | claude-buddy (`ensureCompanion`) | 488 | 481 | ~wash |
| Symbol lookup + callers/callees | claude-code (`QueryEngine`) | ~700 | ~12,265 | ~17x |
| Symbol lookup + callers/callees | cline (`initTask`) | 60 | 420 | cheaper only because it dropped 78% of real callers |
| Symbol lookup + callers/callees | spring-boot (`prepareContext`) | 759 | ~18,460 | ~24x |
| Symbol lookup + callers/callees | tensorflow (`Graph` class) | ~811 | 61,656 | ~76x |
| Symbol lookup + callers/callees | zed (`new_internal`) | 914 | ~8,635 | ~9.4x |
| External-API usage search | awesome-go (`http.NewRequest`) | 296 | 163 (wrong, 60% recall) | naive grep was faster but wrong |
| External-API usage search | claude-buddy (`execSync`) | 246 | 469 (65% signal) | ~1.9x, plus precision |
| External-API usage search | cline (`spawn`, broken) | 260 | 1,505 | cheaper only because it found 0% |
| External-API usage search | spring-boot (`requireNonNull`) | 493 | 711 | ~1.4x |
| External-API usage search | tensorflow (`np.array`, broken) | 54 | 167,181 | cheaper only because it found 1/5,967 |
| Bundled context (`workset`/context pack) | awesome-go | 617 | ~6,136 | ~10x |
| Bundled context (`workset`) | claude-buddy | 583 | ~3,861 | ~6.6x |
| Bundled context (`workset`, coarse class seed) | spring-boot | 2,981 | 17,580+ | ~outline-equivalent, little marginal value |
| Bundled context (`workset`, high-fan-in seed) | tensorflow | ~21,566 (3.6x over budget) | n/a | budget-enforcement failure, not a clean win |
| Bundled context (`workset`) | zed | 2,984 | ~5,903+ | ~55x vs. full-file read |

## Where the win is biggest

- **Outlining a large, named-declaration-heavy file is dekko's best
  case, everywhere it was tried**: ratios run from ~9x up to **~197x**
  (claude-code's 4,683-line `main.tsx`) and **~58x** (zed's
  12,554-line `editor.rs`). Outline cost stays roughly flat (a few
  hundred to ~2,000 tokens); full-file cost scales linearly with file
  size — so the bigger and more central the file, the bigger the win.
- **Precise call-graph lookups** (`query_symbol` + `get_callers`/
  `get_callees` on an unambiguous symbol) land in the **~9x–80x**
  range vs. grep, and the gap widens with repo size and name-collision
  risk: tensorflow's `Graph` class lookup was ~76-80x cheaper than
  `grep "Graph("` precisely because grep returns thousands of noisy
  substring matches at that scale.
- **Repo orientation (`summary`) is a reliable ~3x–50x win**, and more
  importantly the one task type where dekko produces information
  (fan-in-ranked hotspots, directory coupling, detected entry points)
  that no realistic manual reconnaissance — README, `ls`, `find`,
  `cloc` — produces at *any* token budget.
- **Savings grow with repo/file scale.** The same "lookup a central
  symbol" task shape went ~8.7x on awesome-go's 10-file repo, ~76-80x
  on tensorflow's 14,285-file repo. The larger/noisier the repo, the
  more grep's raw-text-match noise costs to manually disambiguate.

## Where the win is smallest, or not real

- **Small, self-contained files**: spring-boot's 84-line
  `AutoConfigurations.java` was cheaper to `Read` directly (724 tok)
  than to fetch via `get_context_pack` (795 tok) — the pack's overhead
  (imports, formatting, caller-list padding) isn't repaid on a file
  small enough to read directly.
- **Small, already-grep-friendly local symbols**: claude-buddy's
  `ensureCompanion` was a near-wash (488 vs. 481 tokens) — once a
  function's body is short and its callers are easy to grep, the
  token edge shrinks to near zero. The remaining value is precision,
  not cost.
- **Callback-registration-heavy files**: claude-buddy's
  `server/index.ts` (an MCP server entrypoint, ~25 anonymous
  `server.tool(...)` handlers) outlined at only 43 tokens but
  **silently omitted the file's actual content**, while a targeted
  `grep -n 'server.tool('` (~200 tokens) was both cheaper and
  complete — the one case in the whole corpus where grep is
  unambiguously the better tool.
- **Broken/negative "savings"**: several External-API-usage rows
  above look like wins for dekko purely because it returned fewer,
  *wrong* tokens (awesome-go's naive-grep comparison, cline's
  `spawn`, tensorflow's `np.array`). A fast, cheap, wrong answer is
  not a real saving — see Correctness caveats below.
- **`workset` on a coarse/high-centrality seed**: a whole-class seed
  (spring-boot) added almost nothing beyond `outline` alone; an
  extremely-high-fan-in seed (tensorflow) blew 3.6x past its own
  budget. `workset`'s "one call replaces N calls" pitch holds best for
  a single, moderately-connected method-level seed and degrades at
  the extremes.

## What predicts the ratio

1. **File/symbol size** (positive predictor) — dekko's structural-query
   cost stays roughly flat regardless of file size; raw reads scale
   linearly, so the bigger the file, the bigger dekko's relative win.
2. **Naming ambiguity in the target repo** (positive predictor,
   conditional on correct resolution) — the more same-named
   symbols/overloads exist, the more a raw grep needs manual
   disambiguation, and the bigger dekko's edge — *provided* the
   resolver handles the case correctly (see caveats below, where the
   same ambiguity instead breaks dekko).
3. **Coding style** (swing factor) — codebases dominated by named
   top-level declarations (typical Go/Java/Rust, most TS) are
   `outline`'s best case; codebases heavy on anonymous-callback/
   registration patterns (MCP servers, route handlers) are its worst.
4. **Target size relative to the read need** — for small files
   (roughly under ~1,000 tokens raw), a direct `Read` competes with or
   beats a context-pack/outline call, because dekko's fixed per-call
   overhead (imports, formatting, footers) isn't amortized.

## Correctness caveats (read before trusting a ratio)

A handful of the ratios above are void: dekko looked "cheap" only
because it silently returned an incomplete or wrong answer, not
because it did less real work for the same result. The synthesis
identified 13 distinct issues across the 7 reports; the two most
consequential were:

- **Under-counted callers** for calls made through a typed variable,
  parameter, or `new X(...)` construction (hit in cline and
  spring-boot) — the resolver reliably tracks `this.method()`-style
  calls but was missing this shape.
- **`find_usages` near-zero recall** when an in-repo symbol shadows
  the external reference being searched for (hit in cline,
  tensorflow, and zed) — a confident-looking but badly incomplete
  result set, with no warning.

Both failure modes shared the same signature: no error, no truncation
footer, no low-confidence flag — indistinguishable in shape from a
correct, complete answer.

**Status as of 0.43.77 (2026-09-22):** all of the above is fixed and
re-verified on the same repos. The typed-variable / `new X(...)` caller
gap and the `workset` budget bypass were closed in the 2026-08-02 pass
(rounds 02-05). `find_usages` went through several rounds: shadowing
now attaches a caveat or refuses with a clear error instead of returning
a small wrong set; round 33's Track 3 (0.43.75) made it match import
bindings and module paths, which turned claude-code's `chalk` query
from "no matches" into the ~10x row above; round 34 re-verified it on
zed (`anyhow`, `Vec::new`/`Box::new` external paths) and tensorflow
(correctly rejecting a shadowed in-repo name). The `outline` blind spot
on anonymous-callback files is disclosed rather than silent: a file
that is anomalously thin for its size gets a `note:` line. Round 34
found zero new High/Medium/Critical issues across all seven repos.
Any number in the original study that traces back to one of these
tasks is a before-fix data point; the table at the top of this page is
the current one.

See each repo's own write-up for the task-level detail and, where
relevant, the specific bug that shaped that repo's numbers.
