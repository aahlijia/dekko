---
name: dekko-delegate
description: Brief a subagent with dekko's map before dispatching it into a repo that has a `.dekko/` directory. Trigger before any Agent/Task tool call, Workflow `agent()`, teammate message, or scripted `claude -p` run that will explore, review, implement, or test in such a repo. A subagent starts with an empty context and no session-start orientation, so without a brief it re-explores from scratch with grep and whole-file reads; one budgeted dekko digest in the prompt replaces that, and telling it the tools exist makes it use them.
---

# Briefing subagents with dekko

The parent session was oriented at start-up; a subagent never is. It
doesn't know the repo has a map, so it greps, reads whole files, and
spends its first several thousand tokens rebuilding a picture the map
already holds. Fix both halves in the prompt: give it the digest, and
tell it to use the tools.

## Procedure

1. **Compute the digest once, scoped to the task.** Run it in Bash
   and paste the output verbatim; don't summarize it by hand.

   | Agent's job | Paste | Budget |
   |---|---|---|
   | Explore, answer questions, locate code | `dekko orient --budget 800` | ~800 tokens; ~1200 on a repo with more than ~10 top-level directories |
   | Implement or review a change | `dekko workset [REV] --budget 2000` (or `--symbol <sym>`), with `--task "<what the agent must do>"` | 1500-3000; the 6000 default is sized for the parent, not a brief |
   | Work on one symbol or file | `dekko context <sym> --budget 800` or `dekko outline <path>` | ~800 |
   | Several agents on one subtree each | one `dekko orient` for all, plus `dekko outline <dir>` per agent | orient once, outline ~500 each |

   With no Bash, the MCP `summary` (budget 800) or `workset` tools
   give the same digest; add the "Tools" paragraph below yourself,
   since only `dekko orient` prints a steering preamble.

2. **Put the digest and the instructions in the prompt**, under
   their own heading so the agent treats them as given, not as
   something to re-derive:

   ```
   ## Repo map (from dekko; already computed, do not redo)
   <pasted output>

   ## Tools
   This repo has a .dekko/ map. Use the dekko MCP tools
   (mcp__plugin_dekko_dekko__* or mcp__dekko__*; the dekko-orient
   skill applies) or `dekko <cmd>` in Bash, before any grep or
   whole-file Read: search_code to find code by what it does,
   outline for a file's shape, get_callers / impacted_tests for
   impact, get_context_pack with with_source for the lines to edit.
   Every tool regenerates a stale map itself; never run `dekko map`
   by hand, and never stop or kill dekko processes (`dekko serve
   --mcp`, `dekko daemon`): they belong to the parent session.
   ```

3. **Ask for a map-shaped report.** Tell the agent to return symbol
   names and `path:line` references, not pasted source, so you follow
   up with `query_symbol` / `get_context_pack` instead of re-reading
   what it read.

4. **Fanning out several agents:** compute `dekko orient` once and
   reuse the text; give each agent its own `outline <dir>` or
   `workset --symbol` for its slice. If the agents will run many
   bare `dekko` CLI commands, start `dekko daemon start` first (see
   `dekko-daemon`) so each call doesn't reparse map.json.

## Why the budget matters

A brief is paid once per agent; an un-oriented agent pays for its own
exploration and then reports it back to you, so you pay twice. Keep
the digest under about a tenth of the agent's expected context: 800
tokens of orientation is the usual right size, a 2000-token workset
for an implementer. Raise it only when the agent's first report shows
it still had to explore.

## Boundaries

- The digest orients; it doesn't replace the agent reading the exact
  lines it edits. Say so if the agent tends to skip that.
- Subagents inherit the parent's MCP tools but not its hook output or
  its ledger; a brief written for one repo doesn't carry to another
  `--root`.
- Don't paste `MAP.md` or a raw `dekko://summary` resource: both are
  unbounded and a large repo's runs tens of thousands of characters.
