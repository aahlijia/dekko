# awesome-go

Source: `test-repos/reports/fable-tokentest-awesome-go.md`

Small, real Go repository (a list-site generator): **10 mapped files,
89 symbols, 73 call edges** — a root `main.go` (static-site builder +
GitHub/GitLab metadata fetcher), two CI helper scripts, and two small
library packages.

## Results

| Task | dekko tokens | Old-way tokens | Ratio |
|---|---:|---:|---|
| A. Repo orientation (`summary`) | 308 | ~15,271 | ~50x |
| B. Outline `main.go` | 492 | ~4,670 | ~9.5x |
| C. Function neighborhood (`fetchProjectMeta`) | 176–294 | ~1,539–6,000+ | ~8.7x+ |
| D. External-API usages (`http.NewRequest`) | 296 | 163 (wrong, 60% recall) | ~break-even in tokens, but the cheap answer was wrong |
| E. Outline a test file | 167 | 2,059 | ~12.3x |
| F. `workset` bundle | 617 | ~6,136 | ~10x |

## Notes

- Every "orientation" and "neighborhood" task landed at **~8x–50x
  fewer tokens** than the equivalent Read/Grep workflow, with `outline`
  and `summary`'s self-reported token footers matching an independent
  `wc -c / 4` count almost exactly.
- **Task D is the one genuinely interesting result.** This shell's
  `grep` (aliased to `ugrep`) silently skips dot-directories by
  default and missed 4 of 10 real `http.NewRequest` call sites living
  in `.github/scripts/check-quality/main.go` — a 60% recall answer
  that looked complete. `find_usages` found all 10. The naive "old
  way" wasn't just more expensive here — it was **wrong**, which
  matters more than the raw token count.
- Two small, repo-specific caveats worth knowing rather than
  correctness bugs: (1) `summary`'s fan-in figure includes test-file
  callers while `get_callers`'s default excludes them, so the two
  numbers can look like they disagree; (2) struct/type symbols
  (`RepoMeta`) always report `fan-in: 0, fan-out: 0` because dekko
  only tracks call edges, not type-usage edges — don't read a
  type's 0/0 as "unused."
