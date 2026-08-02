# cline

Source: `test-repos/reports/fable-tokentest-cline.md`

Large real TypeScript monorepo (VS Code extension + CLI + SDK):
**2,730 mapped files, 19,542 symbols**, effectively ~4 sub-projects
(`vscode`, `cli`, `cline-hub`, `sdk`) glued together.

## Results

| Task | dekko tokens | Old-way tokens | Ratio |
|---|---:|---:|---|
| A. Cold-start orientation | 1,202 | ~4,020 (`find -maxdepth 3` alone: 7,546) | ~3.3x |
| B. Outline `SdkController.ts` (~80 methods, 2,104 lines) | 1,803 | ~21,037 | ~11.7x |
| C. "How is Controller initialized" (`get_callers`/`get_context_pack`) | 2,981–3,768 | ~6,821 | nominally cheaper, **wrong answer** (see notes) |
| D. Impact analysis on `Controller.initTask` | ~60 | ~420 | cheaper only because it dropped 78% of real callers |
| E. Usage search: `find_usages("spawn")` | ~260 | ~1,505 | cheaper only because it found 0% |

## Notes — this repo produced the headline correctness finding

- `outline` on a big file with real named methods (`SdkController.ts`)
  is the clean, uncomplicated win: ~11.7x cheaper, complete and
  accurate — dekko's best case.
- **Task D is the most consequential result in the whole 7-repo
  batch.** `get_callers("Controller.initTask")` returned only 2 of 9
  real call sites (~22% recall) — it reliably tracked `this.method()`
  calls but silently missed all 7 cross-file calls made through a
  typed variable/parameter (`controller.initTask(...)` where
  `controller: Controller`). The 60-vs-420-token "win" in the table
  above is an artifact of an incomplete answer, not a real saving: an
  engineer trusting `get_callers` here would ship a change believing
  `initTask` has 2 callers when it has 9.
- **Task C** showed the same root cause from a different angle:
  `query_symbol("Controller.constructor")` reported `fan-in: 0` even
  though the constructor is called once, via `new Controller(context)`
  — `new X(...)` construction wasn't recorded as a call edge to the
  constructor.
- **Task E** is a second, independent bug: `find_usages("spawn")`
  found 7 Rust hits and zero of the ~40+ real TypeScript
  `child_process`/`Bun` spawn calls, because one unrelated internal
  `function spawn(...)` declaration elsewhere in the repo appears to
  shadow/suppress external-reference tracking for that name
  **repo-wide** rather than just in its own file. `find_usages
  ("readFile")`, hitting the same underlying shadow condition, handled
  it correctly by refusing outright with a clear error — `spawn`
  should have done the same instead of returning a small, wrong,
  plausible-looking result.
- Both bugs (call-graph undercounting through typed variables/`new`,
  and shadow-suppressed `find_usages`) are recorded in the synthesis's
  fix-status table as fixed in a later pass — see
  `benchmarks/real-world-repos/README.md`'s "Correctness caveats"
  section.
