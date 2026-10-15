---
name: dekko-sanity
description: Cross-check a dekko callers/uses result against a targeted grep before trusting a low or zero count. Trigger when the user asks to sanity-check a dekko result, before trusting "no callers" or "dead code" from dekko, or invokes this skill directly with a target (`<target> [--usages] [--include-tests]`, `--unused NAME`, or `--all`).
---

# Sanity check

Run this yourself from the repo root, with the user's target and
flags:

```sh
dekko sanity <target> [--usages] [--include-tests]
dekko sanity --unused NAME
dekko sanity --all
```

The report is generated programmatically by that command. Do NOT
re-derive any of it yourself, and do not re-grep the repo by hand.

1. Relay the match / dekko-only / grep-only buckets to the user.
2. For any grep-only hit, relay the named `cause` verbatim. Do not
   invent a different explanation.
3. If everything matched (`clean: no grep-only misses`), say so
   plainly; this is a spot check, not a verdict that something is
   wrong.
4. In `--unused NAME` mode the report is reference evidence for a
   symbol `dekko unused` flagged dead, each hit tagged by shape
   (`spread`/`typeof`/`subscript`/`call`/`other`). Any evidence means
   "not safe to delete on dekko's word alone"; relay it as such.
5. In `--all` mode the report is a repo-wide triage summary: relay the
   cause histogram and the symbols with unexplained misses, and offer
   `dekko sanity <symbol>` on any of them for the full per-bucket
   detail.
