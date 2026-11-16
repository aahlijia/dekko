# Cursor

dekko works in [Cursor](https://cursor.com), the IDE and the `agent`
CLI, the same way it works in Claude Code and Codex: a plugin brings
the MCP server and the skills, an opt-in block in `AGENTS.md` sets a
standing usage policy, and opt-in hooks push context into the session.
The skills, the policy and the session preamble are the same text the
other agents read. Install the `dekko` CLI first (see
[install.md](install.md)).

## The plugin

```sh
dekko install cursor      # copy the plugin into ~/.cursor/plugins/local/dekko
dekko uninstall cursor    # remove that copy again
```

Cursor has no command that installs a single plugin from disk, so
`dekko install cursor` copies the plugin bundled inside the dekko
package into `~/.cursor/plugins/local/dekko/`, the folder Cursor loads
local plugins from. Then:

- **Reload the Cursor window** (Command Palette, *Developer: Reload
  Window*) and restart any running `agent` session.
- **On a Teams or Enterprise plan**, an admin has to allow local plugin
  imports first. Enterprise turns it off by default.
- **If the `agent` CLI doesn't pick it up**, point it at the copy:
  `agent --plugin-dir ~/.cursor/plugins/local/dekko`.

It's a real copy, not a symlink: Cursor only loads a symlink whose
target sits inside that folder. The copy's MCP server entry gets the
absolute path of your `dekko` executable, because an IDE started from
the Dock or Start menu may not see the `PATH` your shell does. If
`dekko` isn't on `PATH` when you install, the entry keeps the bare name
and the install says so.

The install only ever replaces its own earlier copy. If
`~/.cursor/plugins/local/dekko` is a symlink, or holds a plugin with a
different name, it stops (exit 2) and leaves it alone. The new copy is
built next to the old one and swapped in at the end, so a failed
install leaves the old one working.

The plugin gives Cursor:

- **The MCP server.** `dekko serve --mcp --root ${workspaceFolder}`, so
  it serves the project you have open. Cursor's model calls MCP tools
  through one generic tool, with server `dekko` and the tool's name
  (`query_symbol`, `outline`, ...). The tool set is the one in
  [claude-code.md](claude-code.md#the-mcp-server).
- **Twelve skills.** The eight shared skills from
  [claude-code.md](claude-code.md#skills), plus four that stand in for
  Claude Code's slash commands (see [Skills](#skills)).

**Upgrades.** The copy doesn't change when dekko does. After upgrading
dekko, run `dekko install cursor` again and reload the window.
`dekko doctor` flags a copy whose version doesn't match.

### MCP server only

```sh
dekko install cursor --mcp-only               # this repo's .cursor/mcp.json
dekko install cursor --mcp-only --root DIR    # another project's
dekko uninstall cursor --mcp-only
```

For the tools without the skills. This merges a `dekko` server into the
project's `.cursor/mcp.json` and leaves every other server in the file
alone. Rerunning it is safe. If the file isn't valid JSON, dekko stops
rather than overwrite it; `--force` replaces it. Uninstalling removes
only dekko's entry, and the file and an emptied `.cursor/` folder too
when nothing else is left in them.

Cursor asks you to approve a project MCP server once before it starts
it. In the `agent` CLI, `agent mcp enable dekko` approves it.

It's project scope only. In the global `~/.cursor/mcp.json`,
`${workspaceFolder}` wouldn't name your project.

`--root` and `--force` only mean something with `--mcp-only`; on their
own they exit 2. `--dry-run` isn't available on either form, since both
edit files directly.

## Skills

Cursor picks a skill up on its own when the task matches its
description, or you can call one by name with `/`:

```
/dekko-orient where is the map loaded?
/dekko-impact HEAD~1
```

Claude Code's four slash commands run a shell command before the
prompt, which Cursor doesn't do, so the plugin turns off Cursor's
command discovery and ships four skills in their place. Each one tells
the agent to run the dekko command itself and report the result:

| Skill | Claude Code equivalent | Runs |
| --- | --- | --- |
| `dekko-map` | `/map` | `dekko map --if-stale . [ARGS]`, then `dekko summary --budget 2000` |
| `dekko-impact` | `/impact` | `dekko affected [REV] [--possible]`, with a ready-to-run test command |
| `dekko-doctor` | `/doctor` | `dekko doctor`, relaying the table and each problem row's fix command |
| `dekko-sanity` | `/sanity` | `dekko sanity <target>` (or `--unused NAME`, `--all`), a grep cross-check of a low or zero count |

## A persistent usage policy (opt-in)

Cursor always applies the root `AGENTS.md` and `CLAUDE.md` as standing
instructions, so the policy block dekko already writes works as is:

```sh
dekko install agents-md       # or: dekko install claude-md
```

Both write the same marker-bounded block; see
[codex.md](codex.md#a-persistent-usage-policy-in-agentsmd-opt-in) for
how it behaves. There's no separate Cursor rules file: it would repeat
the block in any repo that already has `AGENTS.md`.

## Push hooks (opt-in)

The same push hooks Claude Code gets (described in
[claude-code.md](claude-code.md#push-hooks-opt-in)) work in Cursor with
`--agent cursor`:

```sh
dekko hooks install --agent cursor                      # session-start only (the default)
dekko hooks install --agent cursor --enable session-start --enable prompt-submit --enable pre-read --enable pre-bash
dekko hooks install --agent cursor --enable pre-bash --strict   # deny instead of ask
dekko hooks uninstall --agent cursor                    # remove all dekko hooks
```

This writes `.cursor/hooks.json` in the repo. Your own entries stay
where they are and dekko's are appended after them; a file dekko
creates gets `"version": 1`, and an existing `version` is kept.
Uninstalling removes only dekko's entries, and deletes the file (and an
emptied `.cursor/`) when nothing but `version` is left. Each entry has a
60-second timeout, since session-start may regenerate a stale map
first.

Cursor reloads hook files as soon as they change. It runs project hooks
only in a trusted workspace; in the headless `agent` CLI that means
passing `--trust`.

All four events are available:

- **`session-start`**: the steering preamble plus a budget-capped
  `lean` map. Cursor throws away session context past 10,000
  characters, so dekko keeps the whole thing under 9,500 and shrinks
  the map to fit. When even the narrowest map of the repo won't fit,
  which happens on mid-size repos, it sends the preamble with a note
  to run `dekko lean --budget N` or `dekko summary` instead.
- **`prompt-submit`**: a short pointer to the files most relevant to
  the new prompt.
- **`pre-read`**: when the agent reads a large mapped file whole, the
  model gets a note that `dekko outline` costs a fraction of it. The
  read still goes ahead.
- **`pre-bash`**: catches a repo-wide `grep`/`rg`/`ag`, a `find -name`
  hunt, or a `cat`/`head`/`sed`/`nl` read of a large stretch of a
  mapped file. Cursor asks you before running it, and the agent gets
  the dekko command that answers the same question. Under `--strict`
  the command is denied instead. A line-ranged read of at most 200
  lines never matches.

Every hook is fail-silent: a missing or stale map, or any error, means
no output, and the session carries on as if the hook weren't there.

**Multi-root workspaces.** The hooks serve the first folder of the
workspace.

## What Cursor imports from Claude Code

Cursor reads Claude Code setups on its own: the plugins installed in
Claude Code, `CLAUDE.md`, `.claude/skills/`, and the hooks in
`.claude/settings.json`. If you use both tools, dekko's Claude Code
install is already partly active in Cursor. What that means:

- **Hooks are safe either way.** A dekko hook always answers in the
  format of the tool that called it, judged from what that tool sent,
  so a Claude Code hook running inside Cursor answers in Cursor's
  format. One difference: an imported `pre-bash` arrives through
  Cursor's general tool hook, which can't ask you. The only other
  answer that could carry dekko's hint would approve the whole command,
  so it stays silent and the command goes through Cursor's normal
  approval. Under `--strict` it still denies. For the prompt with the
  dekko command, install the native hooks
  (`dekko hooks install --agent cursor --enable pre-bash`).
- **dekko can load twice.** With the Claude Code plugin and the Cursor
  plugin both installed, Cursor may list dekko's skills and MCP server
  twice. With dekko hooks in both `.claude/settings.json` and
  `.cursor/hooks.json`, each hook fires twice and the context arrives
  twice. `dekko install cursor` and `dekko hooks install --agent cursor`
  warn when they see this, and `dekko doctor` flags it.

To stop the doubling, remove one of the two, or turn off the import in
Cursor under **Settings > Agents > Third-Party Imports**. The `agent`
CLI has no such setting.

## `dekko doctor`

`dekko doctor` (or the `dekko-doctor` skill) checks the Cursor install
too. The Cursor rows appear only when Cursor is actually installed:
`cursor-agent` or `cursor` on `PATH`, an `agent` on `PATH` that lives
in Cursor's install, `/Applications/Cursor.app` on macOS, or dekko's
own plugin copy. A leftover `~/.cursor/` on its own doesn't count.

| Row | Checks | Fix it names |
|---|---|---|
| `cursor-plugin` | The plugin copy in `~/.cursor/plugins/local/dekko` is dekko's, at this dekko's version (`stale` otherwise), with an MCP command that resolves to an executable | `dekko install cursor` |
| `cursor-mcp` | A `dekko` server in the project or global `.cursor/mcp.json` with a command that resolves, or the plugin row is ok | `dekko install cursor --mcp-only` |
| `cursor-policy` | The policy block is in the root `AGENTS.md` or `CLAUDE.md` | `dekko install agents-md` |
| `cursor-hook:<event>` | One row per event in `.cursor/hooks.json`; `stale` when an entry differs from what this dekko writes | `dekko hooks install --agent cursor --enable <event>` |
| `cursor-claude-overlap` | `advisory` when dekko loads twice through the Claude Code import (both plugins, or hooks in both files) | remove one, or turn off Third-Party Imports |

## Known gaps

- **The session ledger is best-effort.** On Claude Code,
  `prompt-submit` reads the session transcript to skip files already in
  context and to shorten its list as the context window fills.
  Cursor's transcript has no token counts, so dekko estimates them, and
  whether it recognizes Cursor's file reads hasn't been checked
  against a real Cursor session yet.
- **Background and cloud agents** run remotely without the `dekko` CLI
  or a map, so none of this applies there.
