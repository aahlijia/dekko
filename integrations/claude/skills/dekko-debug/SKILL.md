---
name: dekko-debug
description: Work a stack trace, exception, or error report from dekko's map instead of reading each frame's file. Trigger when given a traceback, an exception type or message, a failing test's error, or a question like "why does X raise", "who handles this error", "how does control get from A to B", or "where is this env var read", in a repo with a `.dekko/` directory. dekko has the raise sites, catch clauses, call paths and getenv reads indexed; the trace names the frames, the map explains them.
---

# Debugging with dekko (frames first, files last)

A traceback already names the symbols. Opening each frame's file to
see what it does, what it raises, and who called it re-derives what
the map holds. Ask the map, then read only the lines you'll change.
Tool names below are the bare MCP names (`dekko-orient` has the
prefixes and target syntax); the `throws`/`catches`/`trace`/`env`
queries are CLI only.

## Question → tool

| Question | Use |
|---|---|
| What is this frame, and does it carry a note? | `query_symbol file.py:func` (line-qualify with `:LINE` when the reply says it's an overload set) |
| What does the raising function actually do here? | `get_context_pack` with `with_source=true`, budget ~800; a whole-file Read is the last resort |
| Where can this exception come from? | `dekko query throws <sym>`: the function's own raise sites, each `repo-defined` or `external`; `--transitive --depth N` walks callees |
| Who catches it, or should have? | `dekko query catches <ExcType>`: exact type-name matches plus catch-alls |
| How did control reach this frame? | `get_callers` one hop up; `dekko trace <entry> <frame>` for the shortest call path(s) when the trace is truncated, async, or via a callback |
| Is the failure environment-dependent? | `dekko query env <NAME>` for every getenv-shaped read; `--list` to see the repo's keys |
| Which tests cover the fix? | `impacted_tests` (CLI `dekko affected`) after editing; run the command it prints |

## Procedure

1. **Start from the innermost in-repo frame.** `query_symbol` it;
   read any `note:` lines first, they often name the invariant that
   just broke. External frames (stdlib, vendored packages) are
   context, not targets.
2. **Find the raise.** `dekko query throws <frame>`. If the frame
   only propagates, `--transitive --depth 3` walks its callees; a
   `reached the transitive depth cap` note means raise `--depth`
   rather than assume the list is complete. A `re-raise site(s)
   omitted` note means a bare `raise`/`throw` whose type depends on
   the enclosing handler: read that handler with `get_context_pack`.
3. **Check the handling.** `dekko query catches <ExcType>` tells you
   whether the error was supposed to be caught upstream. Matching is
   exact-name only: a catch of a supertype won't show, so also query
   the type's parents from `get_supertypes <ExcType>`.
4. **Reconstruct the path when the trace doesn't show it.** Async
   boundaries, thread pools, callbacks and signal handlers cut a
   trace short. `get_callers <frame>` one hop at a time, or `dekko
   trace <entry> <frame>` (use `file.py:name` for both ends; a bare
   `main` is usually ambiguous).
5. **Rule out configuration.** If the raise site reads or depends on
   an env var, `dekko query env <NAME>` lists every read site so a
   fix covers all of them. It does not trace config files (YAML,
   `.env`) or dynamic keys.
6. **Fix, then `impacted_tests`.** If the fix changes a signature or
   moves a symbol, `dekko-refactor` takes over the site list.

## Boundaries

- **Grep is right for the message text.** dekko doesn't model
  string literals; one `grep -rn "<exact message>"` to find which
  raise built the message is the correct tool, and it is the one grep
  this skill expects.
- `catches` is a weak signal on JS/TS (catch clauses are rarely
  typed) and only covers the languages it names in its own note.
- `throws` and `trace` follow resolved call edges. A raise reached
  through trait/interface dispatch or a runtime-built name won't
  appear; when the frame is such a method, see `dekko-verify`.
