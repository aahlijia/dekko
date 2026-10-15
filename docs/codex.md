# OpenAI Codex

dekko works in [OpenAI Codex](https://github.com/openai/codex) the same
way it works in Claude Code: one plugin brings the MCP server and the
skills, an opt-in block in `AGENTS.md` sets a standing usage policy, and
opt-in hooks push context into the session. The skills, the policy and
the session preamble are the same text Claude Code reads, written so
either agent can follow them. Install the `dekko` CLI first (see
[install.md](install.md)).

## The plugin

```sh
dekko install codex           # register the bundled plugin with Codex, then restart Codex
dekko uninstall codex         # remove it again
dekko install codex --dry-run # print the codex commands without running them
```

`dekko install codex` runs `codex plugin marketplace add` on the plugin
bundled inside the dekko package, then `codex plugin add dekko@dekko`.
If the add fails because the marketplace is already registered (a
reinstall), it runs `codex plugin marketplace upgrade dekko` instead.
`dekko uninstall codex` runs `codex plugin remove dekko@dekko` and
`codex plugin marketplace remove dekko`; a step that finds nothing to
remove is a warning, not a failure, so it's safe on a partial install.
`--dry-run` works on both. The `codex` CLI has to be on `PATH`.

The plugin gives Codex:

- **The MCP server.** `dekko serve --mcp`, started in the directory you
  launched Codex from, so start Codex at the repo root. Codex shows its
  tools to the model as `mcp__dekko__<tool>` (`mcp__dekko__query_symbol`,
  `mcp__dekko__outline`, ...). Every tool also takes a `root` argument
  for a repo other than the server's working directory. The tool set is
  the one in [claude-code.md](claude-code.md#the-mcp-server).
- **Twelve skills.** The eight shared skills from
  [claude-code.md](claude-code.md#skills), plus four that stand in for
  Claude Code's slash commands (see [Skills](#skills) below).

**Upgrades.** Codex copies the plugin into its own cache
(`$CODEX_HOME/plugins/cache/`, where `CODEX_HOME` defaults to
`~/.codex`) and refreshes that copy whenever the plugin's version
changes. Every dekko release bumps it, so after upgrading dekko and
restarting Codex, the new skills are what Codex loads. A running
`dekko serve --mcp` keeps its old code until Codex restarts it.

### MCP server only

```sh
dekko install codex --mcp-only     # codex mcp add dekko -- dekko serve --mcp
dekko uninstall codex --mcp-only   # codex mcp remove dekko
```

For the tools without the skills. This writes Codex's own
`config.toml` through the `codex` CLI; dekko never edits that file
itself. If you have both this and the plugin, Codex uses the
`config.toml` entry. Both run the same command, so that's harmless.

## Skills

Codex picks a skill up on its own when the task matches its
description, or you can name one in a prompt. Plugin skills are
namespaced, so the mention is `$dekko:<skill>`:

```
$dekko:dekko-orient where is the map loaded?
$dekko:dekko-impact HEAD~1
```

Codex plugins have no slash commands, and Codex skips a Claude-style
command that runs a shell command before the prompt, which all four of
dekko's do. So the plugin ships four Codex-only skills in their place.
Each one tells the agent to run the dekko command itself and report
the result:

| Skill | Claude Code equivalent | Runs |
| --- | --- | --- |
| `dekko-map` | `/map` | `dekko map --if-stale . [ARGS]`, then `dekko summary --budget 2000` |
| `dekko-impact` | `/impact` | `dekko affected [REV] [--possible]`, with a ready-to-run test command |
| `dekko-doctor` | `/doctor` | `dekko doctor`, relaying the table and each problem row's fix command |
| `dekko-sanity` | `/sanity` | `dekko sanity <target>` (or `--unused NAME`, `--all`), a grep cross-check of a low or zero count |

Claude Code never loads these four; it has the real slash commands.

## A persistent usage policy in AGENTS.md (opt-in)

Codex reads `AGENTS.md` as standing instructions for every session in
the project. dekko can add its usage policy there:

```sh
dekko install agents-md       # add/update the marker-bounded usage-policy block
dekko uninstall agents-md     # remove just that block, leave the rest of the file
```

It's the same block `dekko install claude-md` writes into `CLAUDE.md`,
between the same `<!-- dekko:usage-policy:start -->` / `...:end`
markers, and it works the same way: re-running replaces the block in
place, and uninstalling restores the file byte for byte (it deletes
the file if the block was all it held). It's a separate opt-in from
the plugin because it edits a file you own. `AGENTS.md` is also read by
other agents (Cursor, Amp, Jules, ...), and the text is written for any
of them.

If `CLAUDE.md` is a symlink to `AGENTS.md` (or the other way round),
installing both targets still leaves one block, and the link stays a
link.

Two Codex rules can hide the block, and the install warns about both:

- **The 32 KiB budget.** Codex reads at most 32 KiB of `AGENTS.md` by
  default (`project_doc_max_bytes`), and one budget covers every
  `AGENTS.md` from the project root down to the directory you start
  in, root first. The block sits at the end of the root file, so a root
  `AGENTS.md` over 32 KiB loses the block first. The install warns when
  the root file alone is over. Nested `AGENTS.md` files can still use
  up the budget in a session started in a subdirectory; dekko doesn't
  check those.
- **`AGENTS.override.md`.** In any directory that has one, Codex reads
  it *instead of* `AGENTS.md`. With one at the root, the block is
  never read.

## Push hooks (opt-in)

The same push hooks Claude Code gets (described in
[claude-code.md](claude-code.md#push-hooks-opt-in)) work in Codex with
`--agent codex`:

```sh
dekko hooks install --agent codex                          # session-start only (the default)
dekko hooks install --agent codex --enable session-start --enable prompt-submit --enable pre-bash
dekko hooks install --agent codex --enable pre-bash --strict   # deny instead of advise
dekko hooks uninstall --agent codex                        # remove all dekko hooks
```

This writes `.codex/hooks.json` in the repo. It never touches
`.codex/config.toml`, and it stops (exit 2) if that file already
defines hooks in a `[hooks]` table, because Codex warns when one
config layer defines hooks in both places. Uninstalling deletes
`.codex/hooks.json` if nothing else is left in it.

Three events are available:

- **`session-start`**: the steering preamble plus a budget-capped
  `lean` map. Codex normally spills hook context past about 2,500
  tokens to a temp file and shows the model only a preview; dekko's
  entry raises that limit to 20,000 tokens, the same ceiling the hook
  holds itself to, so Codex sees what Claude Code sees.
- **`prompt-submit`**: a short pointer to the files most relevant to
  the new prompt.
- **`pre-bash`**: catches a repo-wide `grep`/`rg`/`ag`, a `find -name`
  hunt, or a `cat`/`head`/`sed`/`nl` read of a large stretch of a
  mapped file, and offers the dekko command that answers the same
  question.

There's no `pre-read`: Codex has no Read tool. Its model reads files
through the shell, so `pre-bash` covers those reads.
`dekko hooks install --agent codex --enable pre-read` exits 2.

### `pre-bash` on Codex

Codex has no "ask the user" decision for a hook, so `pre-bash` works
differently from Claude Code:

- **By default it advises.** A match adds the dekko-equivalent command
  to the model's context, and the shell command still runs.
- **`--strict` denies.** The command is blocked, and the reason names
  the dekko command to run instead.

Line-ranged reads are left alone either way. On Codex,
`sed -n '120,180p' file` *is* how the model reads part of a file, and
it's what dekko's own skills tell it to do, so a read of at most 200
lines never matches, however large the file: `sed -n 'A,Bp'`,
`head -n N`, or a read piped into one (`nl -ba file | sed -n 'A,Bp'`).
A longer range is costed by the lines it prints, not the whole file.

### Trust

Codex only runs project hooks in a project you've trusted, and it asks
you to review each hook before it runs:

- The next time you start Codex after installing, it shows **"Hooks
  need review"**. Approve dekko's hooks there.
- **Any change to a hook needs another review.** Codex's approval
  covers the whole hook entry, so reinstalling with or without
  `--strict` (uninstall first) asks again, and the hook doesn't run
  until you approve it. dekko keeps the entries it writes for Codex
  fixed between releases, so upgrading dekko doesn't reset approval.
- **`codex exec` skips unreviewed hooks silently.** A scripted run
  in a repo where nobody has approved the hooks in the interactive
  Codex just runs without them, with no warning.

Every hook is fail-silent: a missing or stale map, or any error, means
no output, and the session or command carries on as if the hook
weren't there.

## `dekko doctor`

`dekko doctor` (or the `dekko-doctor` skill) checks the Codex install
too. The Codex rows appear only when Codex is actually installed: the
`codex` binary is on `PATH`, or `$CODEX_HOME/plugins/cache/` exists.
A leftover `~/.codex/` directory on its own doesn't count. The rows
cover:

- whether the dekko plugin is installed (`codex plugin list`);
- whether Codex has a `dekko` MCP server registered, from the plugin
  or `--mcp-only` (`codex mcp list --json`);
- whether the root `AGENTS.md` has the usage-policy block, and whether
  it's over 32 KiB or shadowed by an `AGENTS.override.md`;
- one row per hook in `.codex/hooks.json`, flagging an entry that
  differs from what this dekko version writes (Codex will want it
  reviewed again).

Each missing row names the `dekko install ...` or
`dekko hooks install --agent codex ...` command that fixes it.

## Known gaps

- **No session ledger yet.** On Claude Code, `prompt-submit` reads the
  session transcript to skip files already in context and to shorten
  its list as the context window fills. Codex's transcript is a
  different format that dekko doesn't read yet, so on Codex the hook
  still points at the most relevant files, but it doesn't skip ones
  already read and doesn't shorten as the session grows.
- **Codex cloud tasks** run in a remote container without the `dekko`
  CLI or a map, so none of this applies there.
