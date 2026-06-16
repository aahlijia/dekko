# Benchmarks — Active Context Layer

Measurement harness for the overarching goal **G★** of the Active Context
Layer (see `.dev/design-active-context-layer.md` §7): dekko's context
layer must **reduce the tokens an agent spends to work a task**, at equal
task success. If it doesn't, the feature failed.

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

Representative output (dekko mapping its own source):

```
  outline cli.py (large file): 12827 → 1335  (-90%)
  context fit_to_budget (hot symbol): 16614 → 742  (-96%)
  workset --symbol blended_scores: 9830 → 685  (-93%)
  lean (whole-repo map): 4019 tok — 88 files, 954 symbols
overall: 56003 → 4507 tokens across 5 tasks (-92%)
```

## The live half (arrives with hooks, step 4)

The comparison above is the *strategy* cost — it exists today, before the
hooks. The other half of G★ is the **live per-session** cost: with hooks
on vs. off, how many tokens does an identical piece of work actually
consume? That number is read straight from the session transcript via the
ledger:

```sh
python benchmarks/measure.py --root . --session /path/to/session.jsonl
```

Once the SessionStart / UserPromptSubmit hooks land, run the same task
with hooks disabled and enabled, and diff the `consumed_tokens` the ledger
reports. G★ holds only if **on < off**.

## Regression guard

`tests/test_benchmark.py` runs a miniature version of this harness against
a synthetic repo and asserts the core invariant (dekko `outline`/`context`
cost strictly less than the whole-file baseline), so the value proposition
stays falsifiable and is checked on every test run.
