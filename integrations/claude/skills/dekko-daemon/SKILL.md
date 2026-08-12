---
name: dekko-daemon
description: Start dekko's background daemon before a session that will run many bare `dekko` CLI commands via Bash (not the MCP tools) against a large or medium repo. Trigger when you're about to run several dekko CLI invocations in a row on the same repo — a long refactor loop, a batch of query/search/outline calls, or a hook/script context — and want to avoid each one reparsing map.json from scratch.
---

# Warming up repeated `dekko` CLI calls with the daemon

`dekko daemon start` spawns a small per-repo background process that
keeps a warm, in-memory map index across CLI invocations, instead of
each one reloading and re-indexing `map.json` from disk. It only
matters for the **bare CLI path** (commands run via Bash) — if you're
calling the MCP tools (`mcp__dekko__*`) instead, skip this: the MCP
server already keeps its own warm per-process cache
(`Context.index_cache`) independently, by design, so nothing here
applies there.

## When to start it

```sh
dekko daemon start        # spawns it, returns immediately
```

Worth doing at the start of a Bash-CLI-heavy stretch of work — a
refactor loop running `query`/`affected`/`workset` repeatedly, a batch
of `outline`/`search` calls while orienting in a large repo, or
scripted/hook invocations. Skip it for a one-off `dekko map` or a
single query; the daemon adds spin-up overhead that isn't worth it for
one call. On a small repo (fast reload already), it's rarely worth
the trouble either way.

Read-only subcommands route through it transparently once running
(`query`, `search`, `workset`, `diff`, `affected`, `outline`,
`context`, `trace`, `stats`, `summary`, `lean`, `unused`, `status`,
`note list`, `export`) — same output and exit code, just without the
reload. Write-path commands (`map`, `note add`/`note rm`, `hooks ...`)
always run directly regardless. Pass `--no-daemon` on any command to
force direct execution for that one call.

## Checking it and shutting down

```sh
dekko daemon status          # running? pid, uptime, cache hits/misses
dekko daemon stop            # graceful shutdown
```

No need to proactively stop it — it self-shuts-down after 30 minutes
idle by default. Only stop it explicitly if you need a guaranteed-cold
next read (rare) or are done with a long session and want to be tidy.

## What it does not speed up

- **`diff`/`affected`'s old-side (the git rev being diffed against)
  reparse is not covered** by the warm cache at all — that cache only
  ever holds the current working tree's index. A comparison against a
  rev it hasn't seen before pays the same reparse cost a direct
  invocation would; only a *repeat* comparison against the same
  already-cached rev benefits (via the separate on-disk
  `.dekko/rev-cache/`, shared with direct calls regardless of daemon
  use).
- It never trades correctness for speed: every read re-validates the
  cached index the same way a direct invocation would, so a
  working-tree edit or an out-of-band `dekko map` is never served
  stale.

## If a command exits with code 7

That specific exit code means a request was already sent to the
daemon and no response came back in time — the daemon is still
computing it in the background (its accept loop is single-threaded
and has no notion of "the client gave up"). This is deliberately
**not** a silent fallback to a local rerun, to avoid wasting CPU
racing the orphaned daemon-side copy. Either retry with `--no-daemon`
for an immediate fresh local run, or just wait and retry normally once
the daemon has had time to finish the original request.

## Boundaries

- Every other daemon-unavailable case (stale socket, unreachable
  process, any transport error *before* a request is sent) fails open
  silently to normal direct execution — you should never see an error
  from a dead or never-started daemon, only from the exit-7 case
  above.
- Per-repo: if you're working across multiple repos in one session,
  each needs its own `daemon start` (or none, if it doesn't warrant
  one) — `--root DIR` targets a repo other than the cwd.
