# spring-boot

Source: `test-repos/reports/fable-tokentest-spring-boot.md`

Large real Java monorepo: **9,647 mapped files, 66,458 symbols**.

## Results

| Task | dekko tokens | Old-way tokens | Ratio / verdict |
|---|---:|---:|---|
| A. Repo orientation (`summary`) | ~1,957 | ~10,000–20,000+ | ~5–10x cheaper, but see caveat below |
| B. Outline `SpringApplication.java` | 1,970 | 17,580 | ~8.9x cheaper |
| C. Callers/callees of `prepareContext` | 759 | ~18,460 (incl. disambiguation read) | ~24x cheaper |
| D. Small-file understanding (`AutoConfigurations`) | 795 | 724 | old way slightly cheaper and more complete |
| E. Fan-in of `AutoConfigurations.of` | 0 (wrong) | 412 files (noisy, unresolved) | both unreliable — see notes |
| F. External usage (`Objects.requireNonNull`) | 493 | 711 | ~1.4x cheaper, richer output |
| G. `workset` bundle (`SpringApplication`, coarse class seed) | 2,981 | 17,580+ | marginal value over `outline` alone was small |

## Notes

- On a genuinely large repo, dekko's per-file and per-symbol
  structural queries delivered the same understanding at **~9x–24x
  fewer tokens**, and the savings grow with file size and repo scale
  — Task C's disambiguation problem (which method overload is meant)
  doesn't even exist in a 10-file repo.
- **Task D is a useful "smaller is different" data point**: for an
  84-line, self-contained file, a direct `Read` (724 tok) was cheaper
  and more complete than `get_context_pack` (795 tok) — the pack's
  overhead (import list, formatting, caller-list padding) isn't repaid
  on a file this small.
- **Task E is the most important finding for this repo.**
  `AutoConfigurations.of` reported fan-in **766** via `summary`/
  `query_symbol`'s aggregate ranking, but **0** via `get_callers` for
  the identical symbol — and the same panel's other top entries
  (`isEqualTo` at 5,583, `isTrue` at 1,061) look like raw/ambiguous
  name-match counts on generic method names rather than resolved call
  edges. Two tools that both claim to answer "who calls this"
  silently disagreeing on the same symbol is the most trust-damaging
  pattern found in this test batch — worse than any single wrong
  number, because it wasn't visible without independently
  cross-checking.
- **Task G** shows `workset`'s "one call replaces N calls" pitch
  degrading at the high end: seeding it with a whole class added
  almost nothing beyond `outline` alone (2,981 vs. 1,970 tokens —
  `workset` actually cost *more*).
- Java method overloads (`SpringApplication.run` x3) can't be
  disambiguated by the `file:symbol` string alone — there's no
  line/arity selector, so ambiguous names dump every candidate.
- Per the synthesis's fix-status pass, the fan-in/`get_callers`
  disagreement in Task E was investigated and found to be already
  resolved in the current codebase (both surfaces now read from the
  same resolved-edge tables) — see
  `benchmarks/real-world-repos/README.md`.
