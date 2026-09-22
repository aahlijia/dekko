# Benchmarks — Active Context Layer

Measurement harness for the overarching goal **G★** of the Active Context
Layer (the design note behind hooks/orient/ledger, §7; a local `.dev/`
document, not tracked in git): dekko's context layer must **reduce the
tokens an agent spends to work a task**, at equal task success. If it
doesn't, the feature failed.

This directory is **not** part of the installed package (the wheel only
ships `src/dekko`). It is a benchmark, run by hand or in CI.

## What it measures

For a fixed set of representative tasks against a repo, it compares the
token cost of the naive **whole-file-read baseline** against the **dekko
tool** that delivers equivalent context. Both sides use the same
`estimate_tokens`, pinned to `chars4` for stable numbers.

| Task kind | Baseline (what an agent reads) | dekko |
|---|---|---|
| `outline` | the whole file | `dekko outline FILE` |
| `context` | the symbol's file + its callers' files | `dekko context SYM` |
| `workset` | the touched file + impacted test files | `dekko workset --symbol SYM` |
| `lean` | — (no clean baseline) | `dekko lean` — reported as absolute cost + coverage |

## Run it

```sh
# Against this repo (needs a map: `dekko map` first)
python benchmarks/measure.py --root .

# Machine-readable
python benchmarks/measure.py --root . --json
```

Representative output (dekko 0.43.79 mapping its own source, 2026-09-22):

```
  outline integrations/cli.py (large file): 22812 → 1551  (-93%)
  outline render/render_lean.py: 7955 → 1381  (-83%)
  context fit_to_budget (hot symbol): 130555 → 783  (-99%)
  context build_pack: 24678 → 813  (-97%)
  workset --symbol blended_scores: 87698 → 1352  (-98%)
  lean (whole-repo map): 5015 tok — 170 files, 4283 symbols
overall: 273698 → 5880 tokens across 5 tasks (-98%)
```

The `context` and `workset` baselines are large because their naive
equivalent is "read the symbol's file plus every file that calls it";
`fit_to_budget` and `blended_scores` are hot symbols with many callers,
so that baseline grows with the codebase while the pack does not. The
default task list in `measure.py` names paths under `src/dekko/`, so
it needs updating when a targeted module moves; a stale path reports
as `unresolved` rather than a bogus ratio.

## The live half (session-start / prompt-submit hooks)

The comparison above is the *strategy* cost. The other half of G★ is the
**live per-session** cost: with the `session-start`/`prompt-submit` push
hooks (`dekko hooks install` — see
[docs/claude-code.md](../docs/claude-code.md#push-hooks-opt-in)) on vs.
off, how many tokens does an identical piece of work actually consume?
That number is read straight from the session transcript via the ledger:

```sh
python benchmarks/measure.py --root . --session /path/to/session.jsonl
```

Run the same task with hooks disabled and enabled, and diff the
`consumed_tokens` the ledger reports. G★ holds only if **on < off**.

## Regression guard

`tests/test_benchmark.py` runs a miniature version of this harness against
a synthetic repo and asserts the core invariant (dekko `outline`/`context`
cost strictly less than the whole-file baseline), so the value proposition
stays falsifiable and is checked on every test run.

## Real-world repos

The harness above runs on a small synthetic repo, by design (fast,
deterministic, checked on every test run). For how the same value
proposition holds up on large, real, unmodified open-source codebases,
see [`real-world-repos/`](real-world-repos/README.md) — a 7-repo
token-cost comparison (dekko vs. Read/Grep) across awesome-go,
claude-buddy, claude-code, cline, spring-boot, tensorflow, and zed.
It carries two measurements: the original 2026-08-03 study on a
0.20-era dekko (kept as the methodology and the "where it's smallest"
analysis), and the 2026-09-21/22 re-measurement on 0.43.77 from the
same repos, which is the current headline. The correctness caveats
the original study raised were all fixed in the intervening rounds;
that section says which and where.
