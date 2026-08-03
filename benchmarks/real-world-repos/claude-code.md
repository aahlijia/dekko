# claude-code


Large real TypeScript/TSX repo: **1,902 mapped files, 15,360 symbols**.

## Results

| Task | dekko tokens | Old-way tokens | Ratio |
|---|---:|---:|---|
| Understand `src/main.tsx` (4,683-line entrypoint) | `outline`: 1,017 | `Read` full file: ~200,981 | **~197x** |
| Locate `QueryEngine` class + its real caller | `query_symbol`+`get_context_pack`: ~700 | `grep`+`Read`: ~12,265 | ~17x |
| Repo orientation (structure, hotspots, entry points) | `summary`: ~600 | `ls`+`Read README.md`: ~1,521, still no call-graph data | not apples-to-apples — dekko strictly superior |
| Find real callers of a wide-fan-in function (`push`, fan-in 939) | `get_callers` @ budget 4000: 3,598 (35/939 rows, precise) | `grep -rn "push("`: ~50,066 (2,614 raw matches, mostly false positives) | ~14x cheaper and precise, but still can't surface the full 939 in one call |

## Notes

- This is the single largest win recorded across all 7 repos: outlining
  the 4,683-line `main.tsx` entrypoint cost 1,017 tokens against
  ~200,981 to `Read` it in full — **~197x**. The pattern that produces
  this: outline cost stays roughly flat regardless of file size, while
  a full read scales linearly, so the largest/most central files show
  the largest ratios.
- `get_callers`/`get_callees` return call-graph edges, not text
  matches, so they don't get confused by same-named unrelated symbols
  (e.g. `Array.prototype.push` vs. the permission queue's own `push`)
  the way grep does.
- The one place dekko strains here is the tail of a very-high-fan-in
  symbol: `PermissionContext.push` has 939 real callers, and there is
  no clean "give me all of it, cheaply" path yet — a low budget
  under-delivers, and pushing the budget up risks tripping the host's
  own output-size ceiling rather than paginating gracefully.
- The other gotcha: calling a symbol-lookup tool without an explicit
  `root` doesn't error when the server's cwd has its own unrelated
  map — it silently answers from the wrong repo. Always pass `root`
  explicitly in a multi-repo session.
