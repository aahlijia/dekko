# tensorflow

Huge real polyglot repo (Python/C++/…): **14,285 mapped files, 157,845
symbols** — the largest repo in this test batch.

## Results

| Task | dekko tokens | Old-way tokens | Ratio / verdict |
|---|---:|---:|---|
| Repo orientation (`summary` vs. `cloc`) | ~1,987 | ~1,075 (`cloc .`, LOC/lang only) | old way cheaper in raw tokens, but structurally can't produce fan-in/coupling data |
| Understand `ops.py` (6,288-line core file, 8% coverage) | `outline` @ budget 6000: 4,545 | `Read` full file: 58,677 | ~12.9x, but incomplete |
| Locate `Graph` class + real callers | `query_symbol`+`get_callers`: ~811 | `grep -rn "Graph("`: 61,656 | ~76x, plus correctness (86 real edges vs. 2,571 noisy substring hits) |
| Usage count of numpy `array` calls | `find_usages`: 54 (1 hit, wrong) | `grep -rn "np\.array("`: 167,181 (5,967 real hits) | cheap only because it silently failed to find the real usages |
| Task-oriented bundle for a high-fan-in symbol | `workset`: ~21,566 (3.6x over its own 6,000 budget; tripped host output cap) | n/a | budget-enforcement failure, not a clean win |

## Notes

- `map_status`/`summary` scaled cleanly to this repo's size — one
  call each, low token cost, with real architectural signal (fan-in
  ranked hotspots, per-directory coupling, a disclosed parse-error
  list) on a 14K-file, 157K-symbol, 12-language repo.
- The `Graph` class lookup is the clearest large-scale win in this
  test batch: **~76-80x fewer tokens** than `grep "Graph("`, and
  correct — grep's 2,571 raw substring hits need manual
  disambiguation that dekko's resolved call edges don't.
- Two results in the table above are **not real wins** — they're
  wrong answers that happen to look cheap:
  - `find_usages("array")` matched only 1 of 5,967 real `np.array(...)`
    call sites, because an in-repo symbol sharing the bare name
    `array` (`np_array_ops.py::array`) appears to shadow/suppress
    external-reference resolution for that name.
  - `workset` on a high-fan-in symbol blew **3.6x past its own stated
    6,000-token budget** (~21,566 tokens) and tripped the host's own
    output-size cap — the `impacted_tests` enumeration inside
    `workset` was bypassing token-budget accounting entirely and
    printing every matching test path verbatim.
- The `ops.py` outline (Task 2) is a real but incomplete win: it
  stopped at 8% coverage of a 316-symbol file because the default
  `limit=200` bound before the raised `budget=6000` did — two
  independent truncation caps that don't visibly reconcile with each
  other in the first footer.
- Per the synthesis's fix-status pass, the `find_usages` shadow bug
  and the `workset` budget-bypass bug are both recorded as fixed —
  see `benchmarks/real-world-repos/README.md`'s "Correctness caveats."
  The `budget`/`limit` outline interaction was checked and found to
  already report the correctly-binding cap in the current codebase.
