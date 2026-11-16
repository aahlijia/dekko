---
name: dekko-doctor
description: Diagnose dekko environment and install issues (PATH shadowing, map freshness, MCP registration, plugin, hooks, the AGENTS.md/CLAUDE.md policy block). Trigger when a dekko command or MCP tool fails, dekko seems missing or out of date, the user asks whether dekko is set up correctly, or invokes this skill directly.
---

# Doctor report

Run this yourself from the repo root:

```sh
dekko doctor
```

The report is generated programmatically by that command. Do NOT
re-derive any of it yourself.

1. Relay the table verbatim to the user.
2. For any row marked `missing` or `stale`, surface the exact fix
   command it names. Do not invent your own.
3. Do not attempt to fix anything yourself unless the user asks.
