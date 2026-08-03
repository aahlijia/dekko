# zed


Huge real Rust workspace: **2,178 mapped files, ~60,000 symbols**.

## Results

| Task | dekko tokens | Old-way tokens | Ratio |
|---|---:|---:|---|
| Outline `editor.rs` (12,554 lines, 586 symbols) | 1,996 | 115,109 | **~58x** |
| Callers/callees of `Editor::new_internal` (fan-out 74) | 914 | ~8,635 | ~9.4x |
| `workset` for `Workspace::new` init flow | 2,984 | ~5,903+ (surgical) / ~164,571 (full file) | ~55x vs. full-file read |
| `get_callers` on the highest-fan-in symbol (`ParentElement.child`, fan-in 1,085) | 599 (top 13, ranked, budget=600) | 5,668 raw matches, unranked, no way to know which 13 matter | dekko wins on both cost and usability |

## Notes

- Outline-before-read was the single biggest win measured here: both
  large-file tests landed the outline at **~2% of a full `Read`**
  (1,996 vs. 115,109 tokens; 420 vs. 18,873 tokens for `main.rs`), and
  the outline already contains the line numbers needed for a targeted
  follow-up read.
- `workset` reproduced what would otherwise be an outline + a caller
  lookup + a callee lookup + an impacted-tests scan in one call, at a
  fraction of the combined cost.
- Truncation was self-reporting even at extreme fan-in: the
  1,085-caller `ParentElement.child` lookup told the user exactly how
  much was omitted and how to get more (`raise --budget`), rather than
  silently dropping data.
- **`find_usages` is the clear weak spot on this repo, and looks
  Python-tuned rather than Rust-aware.** Four of five probes on
  genuinely-external stdlib calls failed outright: `spawn` and
  `canonicalize` were rejected as "internal symbol" even though the
  real calls are to `std::thread::spawn`/`std::fs::canonicalize`;
  `Mutex` and `println` returned "no external reference match" with
  unhelpful near-miss suggestions. The one probe that succeeded,
  `read_dir`, returned only 4 results in a single file — a follow-up
  `grep -rn "read_dir"` found 124 raw matches, with well over a dozen
  additional genuine call sites (`std::fs::read_dir`, `smol::fs::read_dir`,
  `.read_dir()` method-call form) that were missed entirely. For
  Rust specifically, `find_usages` under-reports rather than
  over-reports — the worse failure mode for an impact-analysis tool.
- A bare ambiguous name (`main`, ~90 candidates across this
  many-binary workspace) dumped every candidate unconditionally before
  failing (~1,110 wasted tokens) rather than ranking/truncating the
  list the way caller/callee lists already do.
- `root` had to be passed explicitly — the first call issued without
  it failed even though a prior `map_status` call had just confirmed
  a fresh map for this repo.
- Per the synthesis's fix-status pass, the ambiguous-candidate dump is
  now capped with a "+N more" note, and `find_usages` now attaches a
  caveat whenever a shadowing in-repo symbol may make a result
  incomplete — see `benchmarks/real-world-repos/README.md`'s
  "Correctness caveats." The underlying Rust stdlib-path recognition
  gap in `find_usages` (`std::thread::spawn`, `.read_dir()` method-call
  form, etc.) was not part of that fix pass and remains open.
