"""Render the map as one self-contained interactive HTML file.

``dekko export --format html`` writes a single file an agent-free reader
can open in a browser: a collapsible directory tree, client-side search,
and a symbol pane with clickable callers/callees. The whole map is
inlined as a JSON island plus vanilla JS and CSS — no dependencies, no
network, no build step (the same zero-dependency stance as the MCP
server). A size guard refuses maps too large to inline, mirroring the
``--max-nodes`` contract in :mod:`export`.
"""

import html
import json
import sys
from pathlib import Path

from .classify import is_test_path
from .mapfile import MapIndex
from .textutil import signature

EXIT_OK = 0
EXIT_TOO_BIG = 2

# Refuse to inline a map whose JSON island exceeds this; a browser bogs
# down well before this and the reader is better served by a subtree map.
HTML_MAX_BYTES = 10_000_000


def _rels(
    index: MapIndex, ids: list[str], caller_first: bool, pivot: str
) -> list[dict]:
    """Relation rows with their call-site lines.

    Args:
        index: Loaded map index.
        ids: The related symbol/module ids (callers or callees).
        caller_first: ``True`` when ``pivot`` is the callee (building a
            caller list), ``False`` when ``pivot`` is the caller.
        pivot: The symbol id the relations are anchored to.

    Returns:
        ``[{"id", "lines"}]`` rows.
    """
    rows = []
    for other in ids:
        key = (other, pivot) if caller_first else (pivot, other)
        rows.append({"id": other, "lines": index.edge_lines.get(key, [])})
    return rows


def _symbols(index: MapIndex) -> dict[str, dict]:
    """Per-symbol payload for the browser, keyed by symbol id."""
    out: dict[str, dict] = {}
    for sid, sym in index.symbols_by_id.items():
        out[sid] = {
            "signature": signature(sym),
            "qualname": sym.qualname,
            "name": sym.name,
            "kind": sym.kind,
            "doc": sym.doc or "",
            "path": sym.path,
            "start": sym.start_line,
            "end": sym.end_line,
            "test": sym.test,
            "callers": _rels(index, index.calls_in.get(sid, []), True, sid),
            "callees": _rels(index, index.calls_out.get(sid, []), False, sid),
        }
    return out


def _files(index: MapIndex) -> list[dict]:
    """Per-file payload (symbol ids only; details live in ``symbols``)."""
    out = []
    for path, language in index.languages_by_path.items():
        out.append(
            {
                "path": path,
                "language": language,
                "doc": index.docs_by_path.get(path) or "",
                "error": index.errors_by_path.get(path) or "",
                "test": is_test_path(path),
                "symbols": [s.id for s in index.symbols_by_path.get(path, [])],
            }
        )
    return out


def _stats(index: MapIndex) -> dict:
    """Header counts mirroring ``dekko summary``."""
    kinds = [s.kind for s in index.symbols_by_id.values()]
    return {
        "files": len(index.languages_by_path),
        "symbols": len(index.symbols_by_id),
        "functions": sum(1 for k in kinds if k in ("function", "method")),
        "classes": sum(1 for k in kinds if k == "class"),
        "edges": sum(len(v) for v in index.calls_out.values()),
    }


def build_document(index: MapIndex) -> dict:
    """Build the browser-facing document inlined into the page."""
    return {
        "root": index.root_label,
        "stats": _stats(index),
        "files": _files(index),
        "symbols": _symbols(index),
    }


def _json_island(doc: dict) -> str:
    """Serialize ``doc`` safe to embed in a ``<script>`` island.

    ``<``, ``>`` and ``&`` only ever appear inside JSON string values
    (the structure uses none), so escaping them to ``\\uXXXX`` keeps the
    text valid JSON while ensuring a symbol named ``</script>`` cannot
    terminate the island or inject markup.
    """
    text = json.dumps(doc, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render(doc: dict) -> str:
    """Assemble the complete HTML page for a built document."""
    island = _json_island(doc)
    root = html.escape(doc["root"])
    s = doc["stats"]
    stats_line = (
        f"{s['files']} files · {s['functions']} functions/methods · "
        f"{s['classes']} classes · {s['edges']} edges"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>dekko — {root}</title>\n"
        '<meta name="viewport" '
        'content="width=device-width, initial-scale=1">\n'
        f"<style>\n{_CSS}\n</style>\n</head>\n<body>\n"
        f"<header><h1>{root}</h1>"
        f'<div class="stats">{stats_line}</div></header>\n'
        "<main>\n<aside>\n"
        '<input id="search" type="search" autocomplete="off" '
        'placeholder="Filter symbols and files…">\n'
        '<div id="tree"></div>\n</aside>\n'
        '<section id="pane"></section>\n</main>\n'
        '<script type="application/json" id="dekko-map">'
        f"{island}</script>\n"
        f"<script>\n{_JS}\n</script>\n</body>\n</html>\n"
    )


def run(index: MapIndex, out_path: Path) -> int:
    """Write the interactive HTML map, guarding against oversized output.

    Args:
        index: Loaded map index.
        out_path: Destination file for the page.

    Returns:
        ``0`` on success, ``2`` when the inlined map exceeds
        :data:`HTML_MAX_BYTES`.
    """
    doc = build_document(index)
    island_bytes = len(_json_island(doc).encode("utf-8"))
    if island_bytes > HTML_MAX_BYTES:
        mb = island_bytes / 1_000_000
        print(
            f"dekko: map too large to inline ({mb:.1f} MB > "
            f"{HTML_MAX_BYTES // 1_000_000} MB); map a subtree "
            "(`dekko map SUBPATH`) or use --format mermaid",
            file=sys.stderr,
        )
        return EXIT_TOO_BIG
    page = render(doc)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page)
    print(f"dekko: wrote {out_path} ({len(page) / 1024:.1f} KB)")
    return EXIT_OK


_CSS = """\
* { box-sizing: border-box; }
body {
  margin: 0;
  color: #1c1e21;
  background: #fff;
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
}
header { padding: 12px 16px; border-bottom: 1px solid #e2e4e8; }
header h1 { margin: 0; font-size: 18px; }
.stats { color: #6b7178; font-size: 13px; margin-top: 2px; }
main { display: flex; height: calc(100vh - 62px); }
aside {
  width: 340px;
  border-right: 1px solid #e2e4e8;
  display: flex;
  flex-direction: column;
}
#search {
  margin: 10px;
  padding: 7px 9px;
  border: 1px solid #cfd2d6;
  border-radius: 6px;
  font: inherit;
}
#tree { overflow: auto; padding: 0 10px 16px; }
#pane { flex: 1; overflow: auto; padding: 16px 22px; }
details.dir > summary { font-weight: 600; cursor: pointer; color: #3b4148; }
details.file { margin: 2px 0 2px 12px; }
details.file > summary { cursor: pointer; }
summary.err { color: #c0392b; }
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px;
}
.sym {
  margin-left: 24px;
  padding: 1px 0;
  cursor: pointer;
  color: #0b5fff;
}
.sym:hover { text-decoration: underline; }
.sym.test, h2.test { color: #9aa0a6; }
.muted { color: #9aa0a6; }
#pane h2 { font-size: 16px; margin: 0 0 4px; }
.meta { color: #6b7178; font-size: 13px; }
.doc { margin: 10px 0; white-space: pre-wrap; }
.rel h3 { font-size: 13px; margin: 16px 0 4px; color: #3b4148; }
.rel ul { margin: 0; padding-left: 18px; }
.rel li { padding: 1px 0; }
a.link { color: #0b5fff; text-decoration: none; cursor: pointer; }
a.link:hover { text-decoration: underline; }
.lines { color: #9aa0a6; }\
"""


_JS = """\
"use strict";
(function () {
  var data = JSON.parse(document.getElementById("dekko-map").textContent);
  var syms = data.symbols;
  var tree = document.getElementById("tree");
  var pane = document.getElementById("pane");
  var search = document.getElementById("search");

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function baseOf(p) {
    var i = p.lastIndexOf("/");
    return i < 0 ? p : p.slice(i + 1);
  }

  function dirOf(p) {
    var i = p.lastIndexOf("/");
    return i < 0 ? "." : p.slice(0, i);
  }

  function moduleLabel(id) {
    var suf = "::<module>";
    if (id.slice(-suf.length) === suf) {
      return "top level of " + id.slice(0, -suf.length);
    }
    return id;
  }

  function relList(title, rels) {
    if (!rels.length) return null;
    var wrap = el("div", "rel");
    wrap.appendChild(el("h3", null, title));
    var ul = el("ul", "mono");
    rels.forEach(function (r) {
      var li = el("li");
      var target = syms[r.id];
      if (target) {
        var a = el("a", "link", target.signature);
        a.href = "#" + encodeURIComponent(r.id);
        a.addEventListener("click", function (ev) {
          ev.preventDefault();
          showSymbol(r.id);
        });
        li.appendChild(a);
      } else {
        li.appendChild(el("span", "muted", moduleLabel(r.id)));
      }
      if (r.lines && r.lines.length) {
        li.appendChild(el("span", "lines", "  :" + r.lines.join(", ")));
      }
      ul.appendChild(li);
    });
    wrap.appendChild(ul);
    return wrap;
  }

  function showSymbol(id) {
    var s = syms[id];
    pane.textContent = "";
    if (!s) {
      pane.appendChild(el("p", "muted", "Select a symbol."));
      return;
    }
    pane.appendChild(el("h2", "mono" + (s.test ? " test" : ""), s.signature));
    pane.appendChild(
      el("div", "meta",
        s.kind + " · " + s.path + " · lines " + s.start + "-" + s.end)
    );
    if (s.doc) pane.appendChild(el("p", "doc", s.doc));
    var cb = relList("called by", s.callers);
    if (cb) pane.appendChild(cb);
    var cl = relList("calls", s.callees);
    if (cl) pane.appendChild(cl);
    location.hash = encodeURIComponent(id);
  }

  function symItem(id) {
    var s = syms[id];
    var it = el("div", "sym mono" + (s.test ? " test" : ""), s.signature);
    var hay = (s.qualname + " " + s.name + " " + s.path).toLowerCase();
    it.setAttribute("data-hay", hay);
    it.addEventListener("click", function () { showSymbol(id); });
    return it;
  }

  function fileNode(f) {
    var d = el("details", "file");
    d.appendChild(el("summary", f.error ? "err" : null, baseOf(f.path)));
    if (f.error) {
      d.appendChild(el("div", "muted", "parse error: " + f.error));
    }
    f.symbols.forEach(function (id) { d.appendChild(symItem(id)); });
    d.setAttribute("data-hay", f.path.toLowerCase());
    return d;
  }

  function build() {
    var dirs = {};
    data.files.forEach(function (f) {
      var d = dirOf(f.path);
      (dirs[d] = dirs[d] || []).push(f);
    });
    Object.keys(dirs).sort().forEach(function (d) {
      var det = el("details", "dir");
      det.open = true;
      det.appendChild(el("summary", "dirname", d + "/"));
      dirs[d].forEach(function (f) { det.appendChild(fileNode(f)); });
      tree.appendChild(det);
    });
  }

  function filterFile(file, q) {
    var fileHit = !q || (file.getAttribute("data-hay") || "").indexOf(q) >= 0;
    var symHit = false;
    file.querySelectorAll(".sym").forEach(function (sym) {
      var h = sym.getAttribute("data-hay") || "";
      var show = !q || fileHit || h.indexOf(q) >= 0;
      sym.style.display = show ? "" : "none";
      if (show && q && !fileHit) symHit = true;
    });
    var show = !q || fileHit || symHit;
    file.style.display = show ? "" : "none";
    if (q) file.open = symHit && !fileHit;
    return show;
  }

  function filter() {
    var q = search.value.trim().toLowerCase();
    tree.querySelectorAll(".dir").forEach(function (dir) {
      var any = false;
      dir.querySelectorAll(".file").forEach(function (file) {
        if (filterFile(file, q)) any = true;
      });
      dir.style.display = any ? "" : "none";
    });
  }

  build();
  search.addEventListener("input", filter);
  var hash = location.hash.slice(1);
  var initial = hash ? decodeURIComponent(hash) : "";
  if (initial && syms[initial]) showSymbol(initial);
  else pane.appendChild(el("p", "muted", "Select a symbol."));
})();\
"""
