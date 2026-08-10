# Claude Code (and Cline)

## The `/map` plugin

```sh
/map            # map the whole repository
/map src/       # map a subtree only
```

`dekko --claude-install` wires up both the `/map` command and the MCP
server (see below); the plugin just runs the installed `dekko` CLI, so
install the package first — see [install.md](install.md).

## Push hooks (opt-in)

Everything above is *pull* — it only helps once the agent knows to
ask. `dekko hooks` adds an opt-in *push* layer: three Claude Code hook
events, enabled individually, that inject context automatically:

```sh
dekko hooks install                        # session-start only (the default)
dekko hooks install --enable session-start --enable prompt-submit --enable pre-read
dekko hooks uninstall                      # remove all dekko hooks
```

- **`session-start`** — a steering preamble plus a budget-capped `lean`
  map, so the first turn already has a navigation map.
- **`prompt-submit`** — for the new prompt, a short pointer to the most
  task-relevant files not already read, so the agent doesn't `grep` blind.
- **`pre-read`** — a non-blocking advisory to `outline` a large file
  first, before a whole-file `Read`.

Installing writes to `.claude/settings.json` (restart Claude Code to
activate). Every handler is fail-silent — a stale map or hook error
never blocks a session or a tool call, it just produces no output.

## The MCP server

`dekko serve --mcp` speaks the Model Context Protocol over stdio
(newline-delimited JSON-RPC, no SDK dependency), so an agent can ask
"who calls X?" with a tool call instead of reading `MAP.md`. It exposes
14 tools:

| Tool | Backs |
| --- | --- |
| `search_code` | free-text relevance search over every symbol (BM25 by default; `scorer: "embedding"` opt-in with `dekko[search]`) |
| `query_symbol` | signature, doc, fan-in/out, notes |
| `get_callers` / `get_callees` | callers/callees, with call sites |
| `find_usages` | references to an external name |
| `get_context_pack` | a symbol's neighborhood, budget-capped |
| `outline` | a file's structure without bodies |
| `workset` | one bundle for a change (`rev` or `symbol`) |
| `summary` | repo digest |
| `impacted_tests` | test files impacted by changes |
| `add_note` / `list_notes` | symbol-anchored notes |
| `map_status` / `refresh_map` | freshness check / regenerate |

`dekko --claude-install` registers this automatically for Claude Code.
For a standalone registration: `dekko --mcp-install` (runs
`claude mcp add dekko -- dekko serve --mcp`).

**Note:** a running `dekko serve --mcp` process holds its code in memory
for its whole lifetime — restart it after any dekko upgrade or source
change, or its output can silently disagree with the CLI.

## Cline

```sh
dekko --cline-install      # merge dekko into cline_mcp_settings.json
dekko --cline-uninstall    # remove just the dekko entry
```

Cline has no plugin system, so only the MCP tools are available (no
`/map`-equivalent slash command). See `dekko --cline-install --help`
for scope/config overrides if auto-detection picks the wrong file.
