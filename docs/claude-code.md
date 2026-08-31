# Claude Code (and Cline)

## The `/map` plugin

```sh
/map            # map the whole repository
/map src/       # map a subtree only
```

`dekko --claude-install` wires up both the `/map` command and the MCP
server (see below); the plugin just runs the installed `dekko` CLI, so
install the package first — see [install.md](install.md).

## The `/doctor` command

```sh
/doctor
```

A read-only diagnostic (`dekko doctor` under the hood, also runnable
standalone from a shell): what's actually wired up in this project
right now, in one report — no more piecing it together from `dekko
status`, `dekko hooks install --help`, and manually checking `command
-v dekko`. Checks:

- **PATH shadowing** — whether the `dekko` a bare `dekko` invocation
  resolves to on `$PATH` is the same binary this process is actually
  running. This is the single most-repeated friction point across past
  eval rounds: a stale globally-installed `dekko` silently answering
  from the wrong build, with no "command not found"-style error to
  flag it. This is the automated version of the manual `command -v
  dekko` check in `test-repos/TESTING-GUIDE.md` §0.
- **Map freshness** — the same signal `dekko status` reports.
- **MCP registration** (`claude mcp list`) and whether a `dekko serve
  --mcp` process is currently running (best-effort, POSIX only).
- **Plugin registration** (`claude plugin list`).
- **Hooks** — which of the four events (`session-start`,
  `prompt-submit`, `pre-read`, `pre-bash`) are wired in
  `.claude/settings.json`.
- **The CLAUDE.md usage-policy block** — present or absent.

Every check degrades independently to `unknown` on its own failure (a
missing `claude`/`pgrep` binary, an unreadable settings file) instead
of aborting the report. `dekko doctor` never auto-fixes anything and
always exits `0` — the findings are advisory, not errors; each
`missing`/`stale` row names the exact command to fix it. `--json` emits
the same findings as structured data for scripting.

## The `/sanity` command

```sh
/sanity resolve
/sanity Path --usages
```

Automates the `dekko-verify` skill (below): rather than relying on an
agent to notice a call-graph result looks "suspiciously low" and
remember to run a manual grep sanity check, `dekko sanity <target>`
(also runnable standalone) does it every time, deterministically. It
runs the same `query callers <target>` dekko would answer with (or
`query uses <target>` under `--usages`), runs one scoped grep sweep
across the repo, and diffs the two hit sets into matches/dekko-only/
grep-only buckets — every grep-only hit is labeled with a likely
cause drawn from `dekko-verify`'s own blind-spot list (a cross-package/
qualified call, an unparsed-language file, a likely unrelated
external-library method sharing the target's bare name, a test-only
call site, or a short/generic target name), never a guess presented as
certain. Always exits `0`; a nonempty grep-only bucket is a finding to
relay, not an error — see
[cli.md](cli.md#sanity-checking-a-lowzero-call-graph-result) for the
full bucket/cause breakdown and every flag.

`dekko sanity --all` (CLI only, no `/sanity`-equivalent slash-command
flag) runs the same callers/grep cross-check across every fan-in
symbol in the repo instead of one human-picked target — a repo-wide
sweep for catching a classification regression or a systemic
grep-vs-dekko disagreement, not a per-symbol spot check. See
[cli.md](cli.md#dekko-sanity---all--sweeping-every-symbol-instead-of-one-target).

## A persistent usage policy in CLAUDE.md (opt-in)

`dekko hooks` (below) injects per-turn context an agent can weigh
against convenience and ignore. `CLAUDE.md` content is documented as
overriding default agent behavior instead — a materially stronger,
one-time lever:

```sh
dekko --claude-md-install     # add/update a marker-bounded usage-policy block
dekko --claude-md-uninstall   # remove just that block, leave the rest of the file
```

Writes a short block bounded by `<!-- dekko:usage-policy:start -->` /
`...:end` markers into the project's `CLAUDE.md`, pointing Claude at
`search_code`/`outline`/`get_callers`/`workset` before `grep`/`Read`.
Idempotent: re-running `--claude-md-install` replaces the block in
place rather than duplicating it. This is a separate, explicit opt-in
from everything else on this page — it edits a file you own and read,
unlike `.claude/settings.json` or the plugin registration.

## Push hooks (opt-in)

Everything above is *pull* — it only helps once the agent knows to
ask. `dekko hooks` adds an opt-in *push* layer: four Claude Code hook
events, enabled individually, that inject context or intervene
automatically:

```sh
dekko hooks install                        # session-start only (the default)
dekko hooks install --enable session-start --enable prompt-submit --enable pre-read
dekko hooks install --enable pre-bash                    # ask before a grep/find/cat fallback
dekko hooks install --enable pre-bash --strict           # ...and deny instead of ask
dekko hooks uninstall                      # remove all dekko hooks
```

- **`session-start`** — a steering preamble plus a budget-capped `lean`
  map, so the first turn already has a navigation map. The map targets
  a 2,000-token budget but can render up to its path-only floor (the
  narrowest possible per-file listing) when a repo needs more than
  that just to list every file. On repos so large that even the floor
  would cost more than ~20,000 tokens, the hook drops the map body
  entirely and injects a disclosure note instead, pointing at `dekko
  lean --budget N` or `dekko summary` for a map at a budget you choose
  — a hard ceiling on what an automatic, zero-choice hook can cost,
  independent of how big the repo is.
- **`prompt-submit`** — for the new prompt, a short pointer to the most
  task-relevant files not already read, so the agent doesn't `grep` blind.
- **`pre-read`** — a non-blocking advisory to `outline` a large file
  first, before a whole-file `Read`.
- **`pre-bash`** — the enforcement tier, off by default even when other
  hooks are installed. Matches a repo-wide `grep`/`rg`/`ag` search, a
  `find -name` hunt, or a `cat`/`head`/`sed` on a large mapped file, and
  surfaces `permissionDecision: "ask"` with the dekko-equivalent
  command — a real interruption, not ignorable text. `--strict`
  escalates matches to `"deny"` instead. Matching is deliberately
  conservative (a targeted, non-recursive `grep pattern one_file.py` or
  a `cat` on an unmapped file like `package.json` never matches) to
  keep false positives low; `grep`/`cat` remain correct for string
  literals, comments, config/data files, and anything outside dekko's
  language coverage.

Installing writes to `.claude/settings.json` (restart Claude Code to
activate). Every handler is fail-silent — a stale map or hook error
never blocks a session or a tool call, it just produces no output (for
`pre-bash`, "no output" means the command runs normally, unmatched).

## Skills

`dekko --claude-install` also ships five Claude Code skills alongside
the `/map` command and MCP server — Claude discovers and invokes them
automatically when their trigger conditions match, no separate install
step:

| Skill | Nudges toward |
| --- | --- |
| `dekko-orient` | reaching for dekko's tools instead of grep/`Read` whenever a repo has a `.dekko/` directory |
| `dekko-verify` | a targeted grep sanity-check before trusting a suspiciously low or zero call-graph result (`get_callers`, `find_usages`, `unused`, ...) — dekko's known resolver blind spots (cross-package/qualified calls, trait/interface dispatch, unparsed-language files); the `/sanity` command (above) automates the check itself rather than only nudging toward running it |
| `dekko-daemon` | starting `dekko daemon start` ahead of a Bash-CLI-heavy stretch of work, and how to handle a `--no-daemon`/exit-7 abandoned-request retry |
| `dekko-notes` | reading a symbol's notes before editing it and writing one after a non-obvious change, via `dekko note add`/`add_note` |
| `dekko-review-context` | composing `workset` + `impacted_tests` + `check_ambiguous` into structural context for a PR description or code-review flow, before reading the diff line by line |

See each skill's `SKILL.md` under `integrations/claude/skills/` for the
full guidance.

## The MCP server

`dekko serve --mcp` speaks the Model Context Protocol over stdio
(newline-delimited JSON-RPC, no SDK dependency), so an agent can ask
"who calls X?" with a tool call instead of reading `MAP.md`. It exposes
18 tools:

| Tool | Backs |
| --- | --- |
| `search_code` | free-text relevance search over every symbol (BM25 by default; `scorer: "embedding"` opt-in with `dekko[search]`) |
| `query_symbol` | signature, doc, fan-in/out, notes |
| `get_callers` / `get_callees` | callers/callees, with call sites |
| `find_usages` | references to an external name |
| `find_type_usages` | functions/methods taking or returning a type |
| `get_supertypes` / `get_subtypes` | a type's extends/implements heritage, one hop or transitive |
| `get_context_pack` | a symbol's neighborhood, budget-capped |
| `outline` | a file's structure without bodies |
| `workset` | one bundle for a change (`rev` or `symbol`; `type_impact` for a type's full blast radius) |
| `summary` | repo digest |
| `impacted_tests` | test files impacted by changes |
| `check_ambiguous` | resolver-trust summary: where call resolution was ambiguous |
| `add_note` / `list_notes` | symbol-anchored notes |
| `map_status` / `refresh_map` | freshness check / regenerate |

`dekko --claude-install` registers this automatically for Claude Code.
For a standalone registration: `dekko --mcp-install` (runs
`claude mcp add dekko -- dekko serve --mcp`).

**Note:** a running `dekko serve --mcp` process holds its code in memory
for its whole lifetime — restart it after any dekko upgrade or source
change, or its output can silently disagree with the CLI. `/doctor`
(above) surfaces a currently-running server as an active finding, not
just this doc note — it just can't tell you whether that process's
loaded code is actually stale (no way to introspect that from outside
without an MCP round trip), so it always reports "restart if you
upgraded since it started" rather than a definitive verdict.

## Cline

```sh
dekko --cline-install      # merge dekko into cline_mcp_settings.json
dekko --cline-uninstall    # remove just the dekko entry
```

Cline has no plugin system, so only the MCP tools are available (no
`/map`-equivalent slash command). See `dekko --cline-install --help`
for scope/config overrides if auto-detection picks the wrong file.
