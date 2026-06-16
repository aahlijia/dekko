---
name: dekko-orient
description: Orient with dekko's structural tools before reading a repo's files. Use when starting work in a repo that has a .dekko/ map: read less of the repo by outlining files, bundling changes, and querying symbols instead of reading whole files. Applies to any repo with a .dekko/ directory.
---

# Orienting with dekko (read less of the repo)

When a repo has a `.dekko/` map, dekko exposes the codebase's structure
without spending tokens on reading whole files. Reach for these
structural tools first; fall back to reading source only for the lines
you are actually about to edit.

## Orient first

At the start of work in a mapped repo, get the shape of the codebase
before diving in:

```
dekko summary
```

or `dekko orient` (the same digest with a short steering preamble), or
read the `dekko://summary` MCP resource. This names every directory's
purpose, the load-bearing and orchestrator symbols, the entrypoints, and
the largest files — a few hundred tokens instead of a full read.

## Read less of the repo

Before reading a whole file, get its shape for ~1/10 the cost:

```
dekko outline path/to/file.py
```

(module doc + every symbol's signature, first doc line, and line number —
no bodies). `dekko outline <dir>` rolls up a directory.

For a change or a PR, bundle everything you need under one budget in a
single call instead of assembling it by hand:

```
dekko workset [REV]          # or: dekko workset --symbol NAME
```

(impacted tests + touched-file outlines + call-graph packs for the most
central touched symbols).

To locate and understand a symbol and its neighborhood:

```
dekko query symbol <sym>     # signature, callers, callees, notes
dekko context <sym>          # a budgeted call-graph pack
```

To scope which tests a change touches: `dekko affected [REV]`.

## Boundaries

- These are structural aids, not a replacement for reading the exact
  lines you are about to change. Outline to navigate; read to edit.
- Orientation is **stateless** — re-run it when you need it; do not
  assume an earlier digest still reflects the current tree.
- Every list tool takes a `--budget`; its footer reports the token cost
  and what was omitted, so you can spend tokens deliberately.
