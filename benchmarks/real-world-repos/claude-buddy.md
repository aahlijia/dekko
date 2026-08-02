# claude-buddy

Source: `test-repos/reports/fable-tokentest-claude-buddy.md`

Mid-sized real TypeScript repo (a Claude Code companion/MCP server):
**57 mapped files, 662 symbols**.

## Results

| Task | dekko tokens | Old-way tokens | Ratio |
|---|---:|---:|---|
| A. Repo orientation (`summary`) | 683 | ~4,925 | ~7.2x |
| B1. Outline `cli/tui.tsx` (69 named symbols) | 1,036 | ~23,459 | ~22x |
| B2. Outline `server/index.ts` (anonymous-callback file) | 43 | ~23,459 | misleading — see notes |
| C. Callers/callees of `ensureCompanion` (fan-out 13) | 488 | 481 | ~wash |
| D. Context bundle for `awardXp` (XP award flow) | 236 | ~1,317 | ~5.6x |
| E. Usage search (`execSync`) | 246 | 469 (65% signal) | ~1.9x, plus precision |
| F. `workset` bundle (`awardXp`) | 583 | ~3,861 | ~6.6x |

## Notes

- `workset` and `get_context_pack` were the strongest tools in this
  repo — 5–7x cheaper in every case tested, and each one collapsed a
  multi-step grep-then-read-then-read workflow into a single call.
- `outline` is excellent on files with real named declarations
  (`tui.tsx`: ~22x, complete and accurate) but has a genuine blind
  spot on files structured as chains of anonymous callbacks. This
  repo's MCP server entrypoint, `server/index.ts` (1,344 lines, ~25
  `server.tool("x", ..., async () => {...})` registrations), outlined
  at just 43 tokens — technically accurate (there are only 5 named
  top-level symbols) but it **silently hides the file's actual
  content**. A plain `grep -n 'server.tool('` (~200 tokens) both cost
  about the same and was actually complete — the one case in this
  whole test batch where grep unambiguously beats dekko's outline.
  The same blind spot showed up as a misleading fan-in number:
  `ensureCompanion` reported `fan-in: 1` via `query_symbol` even
  though `get_callers` lists 15 real call sites, because all 15 live
  inside those same anonymous callbacks and collapse to one "module
  level" pseudo-caller.
- `find_usages` beat grep on precision, not just size: of grep's 23
  `execSync` hits, 8 were noise (import statements, a comment); all
  15 of dekko's hits were genuine calls.
- Per the synthesis's fix-status pass: the fan-in half of this blind
  spot (`ensureCompanion` reading 1 vs. 15 real call sites) is fixed —
  a callback wired up by reference is now tracked separately and
  counted. `outline` now also flags a file as anomalously thin for its
  size when this shape is detected, though it still doesn't expand the
  callback bodies themselves. See
  `benchmarks/real-world-repos/README.md`'s "Correctness caveats."
