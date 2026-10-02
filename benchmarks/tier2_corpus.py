"""Measure every Tier-2 row against a real repository.

A Tier-2 language is read through one row in ``dekko.core.tier2``. A
row is only as good as the code it was checked on, so this runs the
Tier-2 extractor over one pinned repository per grammar
(``tier2_corpus.json``) and compares the names it yields with a
per-language line regex, the "truth". Truth is approximate. It tells
0% from 60% from 95%, which is what a row is judged on.

The bar, on name recall per grammar:

* supported: functions at 90% or more, types at 80% or more (when the
  repository has 20 or more), and at least one call extracted
* partial: functions at 75% or more, types at 50% or more
* out: anything less

Comparison is by name set per file, so a function written as several
clauses, or as a declaration plus a body, counts once. "extra%" is the
share of extracted names the regex did not find. It is not part of the
bar, because it counts real definitions of a shape the regex lacks
(a getter, a ``deftest``) along with wrong ones; ``--detail`` lists the
names so they can be read.

It needs the grammar pack (``dekko[all]``) and, to clone, the network,
so it is a benchmark and not a test. Run it when the pack is upgraded
or a row changes.

Usage::

    python benchmarks/tier2_corpus.py --repos-dir ../corpus --clone
    python benchmarks/tier2_corpus.py --repos-dir ../corpus --grammar zig
    python benchmarks/tier2_corpus.py --root ../some/checkout --detail d.txt

With ``--repos-dir`` it exits 1 when a grammar measures below the
status pinned for it. ``--root`` measures any checkout and judges
nothing.
"""

import argparse
import collections
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node, Parser

from dekko.core.extractor_generic import extract_file_generic
from dekko.core.grammars import get_grammar
from dekko.core.languages import tier2_grammar_for_path
from dekko.core.tier2 import TIER2_SPECS
from dekko.core.walker import discover

FIXTURE = Path(__file__).with_name("tier2_corpus.json")

M = re.M
I = re.I  # noqa: E741

# grammar -> (function regexes, container regexes). Group 1 is the name.
TRUTH: dict[str, tuple[list[re.Pattern[str]], list[re.Pattern[str]]]] = {
    "ruby": (
        [re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!=]?)", M)],
        [re.compile(r"^\s*(?:class|module)\s+([A-Z][\w:]*)", M)],
    ),
    "php": (
        [re.compile(r"\bfunction\s+&?\s*([A-Za-z_]\w*)\s*\(")],
        [
            re.compile(
                r"^\s*(?:abstract\s+|final\s+|readonly\s+)*"
                r"(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)",
                M,
            )
        ],
    ),
    "csharp": (
        [
            re.compile(
                r"^\s*(?:(?:public|private|protected|internal|static|virtual|"
                r"override|abstract|async|sealed|extern|new|unsafe|partial)\s+)+"
                r"[\w<>\[\],.?]+\s+([A-Za-z_]\w*)\s*(?:<[^>()]*>)?\s*\(",
                M,
            )
        ],
        [re.compile(r"\b(?:class|interface|struct|enum|record)\s+([A-Z]\w*)")],
    ),
    "swift": (
        [re.compile(r"\bfunc\s+([A-Za-z_]\w*)")],
        [
            re.compile(
                r"^\s*(?:(?:public|private|internal|fileprivate|open|final|"
                r"indirect)\s+)*(?:class|struct|enum|protocol|extension|actor)"
                r"\s+([A-Z][\w.]*)",
                M,
            )
        ],
    ),
    "scala": (
        [re.compile(r"\bdef\s+([A-Za-z_]\w*)")],
        [re.compile(r"\b(?:class|object|trait|enum)\s+([A-Z]\w*)")],
    ),
    "lua": (
        [
            re.compile(r"\bfunction\s+([\w.:]+)\s*\("),
            re.compile(r"^\s*(?:local\s+)?([\w.]+)\s*=\s*function\s*\(", M),
        ],
        [],
    ),
    "perl": (
        [re.compile(r"^\s*sub\s+(\w+)", M)],
        [re.compile(r"^\s*package\s+([\w:]+)", M)],
    ),
    "r": (
        [re.compile(r"^\s*([\w.]+)\s*(?:<-|=)\s*function\s*\(", M)],
        [],
    ),
    "julia": (
        [
            re.compile(
                r"^\s*(?:@[\w.]+\s+)*function\s+(?:[\w.]+\.)?([\w!]+)", M
            ),
            re.compile(
                r"^(?:@[\w.]+\s+)*(?:[\w.]+\.)?([A-Za-z_][\w!]*)\([^=\n]*\)"
                r"(?:\s*where\s+[^=\n]+)?\s*=(?!=)",
                M,
            ),
        ],
        [
            re.compile(r"^\s*(?:mutable\s+)?struct\s+(\w+)", M),
            re.compile(r"^\s*(?:abstract type|module)\s+(\w+)", M),
        ],
    ),
    "dart": (
        [
            re.compile(
                r"^\s*(?:static\s+|external\s+|@\w+\s+)*[\w<>?,\[\]]+\s+"
                r"([a-z_]\w*)\s*(?:<[^>()]*>)?\([^;]*\)\s*"
                r"(?:async\*?\s*|sync\*\s*)?(?:\{|=>)",
                M,
            )
        ],
        [
            re.compile(
                r"^\s*(?:abstract\s+|final\s+|sealed\s+|base\s+)*"
                r"(?:class|mixin|enum|extension)\s+([A-Z]\w*)",
                M,
            )
        ],
    ),
    "zig": (
        [re.compile(r"\bfn\s+([A-Za-z_]\w*)\s*\(")],
        [
            re.compile(
                r"^\s*(?:pub\s+)?const\s+(\w+)\s*=\s*(?:extern\s+|packed\s+)?"
                r"(?:struct|enum|union)\b",
                M,
            )
        ],
    ),
    "haskell": (
        [re.compile(r"^([a-z_][\w']*)\s*::", M)],
        [re.compile(r"^(?:data|newtype|class|type)\s+([A-Z][\w']*)", M)],
    ),
    "elixir": (
        [
            re.compile(
                r"^\s*(?:def|defp|defmacro|defmacrop)\s+([a-z_]\w*[?!]?)", M
            )
        ],
        [re.compile(r"^\s*(?:defmodule|defprotocol)\s+([\w.]+)", M)],
    ),
    "erlang": (
        [re.compile(r"^([a-z]\w*)\([^\n]*\)\s*(?:when\b[^\n]*)?->", M)],
        [re.compile(r"^-module\(\s*([a-z]\w*)\s*\)", M)],
    ),
    "ocaml": (
        [
            re.compile(
                r"^\s*(?:let|and)\s+(?:rec\s+)?([a-z_][\w']*)\s+[^=:\s]", M
            )
        ],
        [re.compile(r"^\s*module\s+(?:type\s+)?([A-Z]\w*)", M)],
    ),
    "clojure": (
        [
            re.compile(
                r"^\((?:defn-?|defmacro|defmulti)\s+(?:\^\S+\s+)*([^\s\[\]()]+)",
                M,
            )
        ],
        [
            re.compile(
                r"^\((?:defprotocol|defrecord|deftype)\s+([^\s\[\]()]+)", M
            )
        ],
    ),
    "gleam": (
        [re.compile(r"^\s*(?:pub\s+)?fn\s+(\w+)\s*\(", M)],
        [re.compile(r"^\s*(?:pub\s+)?(?:opaque\s+)?type\s+([A-Z]\w*)", M)],
    ),
    "nim": (
        [
            re.compile(
                r"^\s*(?:proc|func|method|iterator|template|macro|converter)"
                r"\s+`?([A-Za-z_]\w*)",
                M,
            )
        ],
        [
            re.compile(
                r"^\s+([A-Z]\w*)\*?\s*(?:\{\.[^}]*\.\})?\s*=\s*"
                r"(?:ref\s+)?(?:object|enum)\b",
                M,
            )
        ],
    ),
    "solidity": (
        [re.compile(r"\b(?:function|modifier)\s+(\w+)\s*\(")],
        [
            re.compile(
                r"^\s*(?:abstract\s+)?(?:contract|interface|library)\s+(\w+)",
                M,
            )
        ],
    ),
    "bash": (
        [
            re.compile(
                r"^\s*(?:function\s+)?([A-Za-z_][\w:.-]*)\s*\(\s*\)", M
            ),
            re.compile(r"^\s*function\s+([A-Za-z_][\w:.-]*)", M),
        ],
        [],
    ),
    "powershell": (
        [
            re.compile(
                r"^\s*(?:function|filter)\s+(?:global:|script:)?([\w-]+)",
                M | I,
            )
        ],
        [re.compile(r"^\s*class\s+(\w+)", M | I)],
    ),
    "sql": (
        [
            re.compile(
                r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+"
                r"[\[\"`]?([\w.]+)",
                I,
            )
        ],
        [
            re.compile(
                r"\bCREATE\s+(?:(?:GLOBAL|TEMPORARY|TEMP|UNLOGGED)\s+)*TABLE\s+"
                r"(?:IF\s+NOT\s+EXISTS\s+)?(?:[\[\"`]?\w+[\]\"`]?\.)?"
                r"[\[\"`]?(\w+)",
                I,
            )
        ],
    ),
    "fortran": (
        [
            re.compile(
                r"^(?!\s*end\b)\s*(?:[\w()=*,]+\s+)*?(?:function|subroutine)"
                r"\s+(\w+)",
                M | I,
            )
        ],
        [
            re.compile(r"^\s*module\s+(?!procedure\b)(\w+)", M | I),
            re.compile(r"^\s*type\s*(?:,[^:\n]*)?::\s*(\w+)", M | I),
        ],
    ),
    "pascal": (
        [
            re.compile(
                r"^\s*(?:class\s+)?(?:function|procedure|constructor|"
                r"destructor)\s+([\w.]+)",
                M | I,
            )
        ],
        [
            re.compile(
                r"^\s*(\w+)\s*=\s*(?:packed\s+)?"
                r"(?:class|record|interface|object)\b",
                M | I,
            )
        ],
    ),
    "elm": (
        [re.compile(r"^([a-z]\w*)\s*:", M)],
        [re.compile(r"^type\s+(?:alias\s+)?([A-Z]\w*)", M)],
    ),
    "fsharp": (
        [
            re.compile(
                r"^(?: {0,4})(?:let|and)\s+(?:(?:inline|private|rec|internal|"
                r"public)\s+)*([a-zA-Z_][\w']*)\s+[^=:\n]",
                M,
            ),
            re.compile(
                r"^\s*(?:static\s+)?(?:member|override|abstract|default)\s+"
                r"(?:(?:inline|private|internal|public)\s+)*(?:\w+\.)?"
                r"([A-Za-z_][\w']*)",
                M,
            ),
        ],
        [
            re.compile(
                r"^\s*(?:type|module)\s+(?:(?:private|internal|public|rec)\s+)*"
                r"([A-Z][\w']*)",
                M,
            )
        ],
    ),
    "racket": (
        [re.compile(r"^\s*\(define\s+\(([^\s()]+)", M)],
        [re.compile(r"^\s*\(struct\s+([^\s()]+)", M)],
    ),
    "scheme": (
        [
            re.compile(r"^\s*\(define\s+\(([^\s()]+)", M),
            re.compile(r"^\s*\(define-syntax\s+([^\s()]+)", M),
        ],
        [re.compile(r"^\s*\(define-record-type\s+\(?([^\s()]+)", M)],
    ),
    "commonlisp": (
        [
            re.compile(
                r"^\s*\((?:defun|defmacro|defgeneric|defmethod)\s+"
                r"([^\s()]+)",
                M | I,
            )
        ],
        [re.compile(r"^\s*\((?:defclass|defstruct)\s+\(?([^\s()]+)", M | I)],
    ),
    "elisp": (
        [
            re.compile(
                r"^\((?:defun|defmacro|defsubst|cl-defun|cl-defmethod|"
                r"cl-defgeneric)\s+([^\s()]+)",
                M,
            )
        ],
        [re.compile(r"^\((?:defclass|cl-defstruct)\s+\(?([^\s()]+)", M)],
    ),
    "vim": (
        [
            re.compile(
                r"^\s*fu(?:n(?:c(?:t(?:i(?:on?)?)?)?)?)?!?\s+([\w:#.<>]+)\s*\(",
                M,
            )
        ],
        [],
    ),
    "tcl": (
        [re.compile(r"^\s*(?:proc|method)\s+([^\s{}]+)", M)],
        [re.compile(r"^\s*(?:oo::class\s+create|snit::type)\s+([^\s{}]+)", M)],
    ),
    "d": (
        [
            re.compile(
                r"^\s*(?:(?:public|private|protected|package|static|final|"
                r"override|abstract|pure|nothrow|@safe|@trusted|@system|@nogc|"
                r"@property|const|immutable|ref|export)\s+)*"
                r"(?!(?:if|while|for|foreach|switch|return|else|catch|with|"
                r"assert|enforce|version|debug|mixin|import|scope|throw|new|"
                r"static|foreach_reverse|synchronized|delete|alias|enum|auto)\b)"
                r"[\w!.\[\]*]+(?:\([^)\n]*\))?\s+([a-zA-Z_]\w*)\s*\([^;{\n]*\)"
                r"[\w@ \t]*(?:\{\s*)?$",
                M,
            )
        ],
        [
            re.compile(
                r"^\s*(?:(?:public|private|package|final|abstract|static)\s+)*"
                r"(?:class|struct|interface|template|union)\s+([A-Z]\w*)",
                M,
            )
        ],
    ),
    "ada": (
        [
            re.compile(
                r"^\s*(?:overriding\s+|not\s+overriding\s+)?"
                r"(?:procedure|function)\s+([\w.]+)",
                M | I,
            )
        ],
        [
            re.compile(
                r"^\s*(?:private\s+)?package\s+(?:body\s+)?([\w.]+)"
                r"(?!\s+(?:renames|is\s+new)\b)\s+is\b",
                M | I,
            )
        ],
    ),
    "hare": (
        [re.compile(r"^(?:export\s+)?(?:@\w+\s+)*fn\s+(\w+)\s*\(", M)],
        [
            re.compile(
                r"^(?:export\s+)?type\s+(\w+)\s*=\s*(?:struct|union|enum)", M
            )
        ],
    ),
    "odin": (
        [re.compile(r"^\s*(\w+)\s*::\s*(?:#\w+\s+)*proc\b", M)],
        [re.compile(r"^\s*(\w+)\s*::\s*(?:struct|enum|union|bit_set)\b", M)],
    ),
    "crystal": (
        [
            re.compile(
                r"^\s*(?:private\s+|protected\s+)?(?:def|macro)\s+(?:self\.)?"
                r"([A-Za-z_]\w*[?!=]?)",
                M,
            )
        ],
        [
            re.compile(
                r"^\s*(?:abstract\s+|private\s+)?"
                r"(?:class|module|struct|enum|lib|annotation)\s+([A-Z][\w:]*)",
                M,
            )
        ],
    ),
    "haxe": (
        [re.compile(r"\bfunction\s+(\w+)\s*[(<]")],
        [
            re.compile(
                r"^\s*(?:(?:private|extern|final|abstract)\s+)*"
                r"(?:class|interface|enum|typedef|abstract)\s+([A-Z]\w*)",
                M,
            )
        ],
    ),
    "gdscript": (
        [re.compile(r"^\s*(?:static\s+)?func\s+(\w+)\s*\(", M)],
        [
            re.compile(r"^\s*class\s+(\w+)", M),
            re.compile(r"^class_name\s+(\w+)", M),
        ],
    ),
    "nix": (
        [
            re.compile(
                r"^\s*([\w'-]+)\s*=\s*(?:\{[^}\n]*\}|[\w']+)\s*"
                r"(?:@\s*\w+\s*)?:(?!:)",
                M,
            )
        ],
        [],
    ),
    "starlark": (
        [re.compile(r"^\s*def\s+(\w+)\s*\(", M)],
        [],
    ),
    "cmake": (
        [re.compile(r"^\s*(?:function|macro)\s*\(\s*([\w.+-]+)", M | I)],
        [],
    ),
}
TRUTH["zsh"] = TRUTH["bash"]

# Definition words: a call named one of these is a definition the
# extractor read as a call.
KEYWORDS = frozenset(
    "def defp defmodule defmacro defmacrop defn defn- defun define "
    "defprotocol defrecord deftype defmethod defgeneric defclass "
    "function proc func fn class module struct".split()
)
# A callee name with whitespace or brackets in it is not a name.
IDENT = re.compile(r"^[^\s(){}\[\];,\"'`]{1,60}$")
NAME_SPLIT = re.compile(r"[.:]+")
TYPE_KINDS = frozenset({"class", "struct", "interface", "trait"})

# Types are judged only when the repository has this many.
MIN_TYPES = 20
# (status, function recall, type recall), best first.
BARS = (("supported", 0.90, 0.80), ("partial", 0.75, 0.50))
RANK = {"supported": 2, "partial": 1}

Counter = collections.Counter


@dataclass
class Tally:
    """Everything measured for one grammar."""

    counts: Counter[str] = field(default_factory=Counter)
    missed: Counter[str] = field(default_factory=Counter)
    extra: Counter[str] = field(default_factory=Counter)
    callees: Counter[str] = field(default_factory=Counter)
    examples: list[str] = field(default_factory=list)


def last(name: str, grammar: str) -> str:
    """The bare name, the way the grammar's row would split it."""
    if TIER2_SPECS[grammar].split is None:
        return name

    parts = [p for p in NAME_SPLIT.split(name) if p]
    return parts[-1] if parts else name


def truth_names(grammar: str, text: str) -> tuple[set[str], set[str]]:
    """Function and type names the line regexes find in a file."""
    fns, types = TRUTH[grammar]
    return (
        {last(m.group(1), grammar) for rx in fns for m in rx.finditer(text)},
        {last(m.group(1), grammar) for rx in types for m in rx.finditer(text)},
    )


def error_bytes(root: Node) -> int:
    """Bytes under the outermost ``ERROR`` nodes of a tree."""
    total = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR":
            total += node.end_byte - node.start_byte
        elif node.has_error:
            stack.extend(node.children)

    return total


def _ratio(hit: int, total: int, floor: int = 1) -> float | None:
    return hit / total if total >= floor else None


def verdict(c: Counter[str]) -> str:
    """Where a grammar's numbers put it against the bar."""
    fn = _ratio(c["hit_fn"], c["truth_fn"])
    ty = _ratio(c["hit_cls"], c["truth_cls"], MIN_TYPES)
    if fn is None and ty is None:
        return "no truth"

    low = fn if fn is not None else ty
    if not c["calls"]:
        return "out (no calls)" if low < BARS[-1][1] else "no calls"
    for status, fn_bar, type_bar in BARS:
        if low >= fn_bar and (ty is None or ty >= type_bar):
            return status

    return "out"


def _call_counts(fm_calls: list, tally: Tally) -> Counter[str]:
    c: Counter[str] = Counter()
    c["calls"] = len(fm_calls)
    c["calls_attr"] = sum(1 for call in fm_calls if call.caller_id)
    for call in fm_calls:
        tally.callees[call.name] += 1
        if not IDENT.match(call.name):
            c["calls_garbage"] += 1
        elif call.name in KEYWORDS:
            c["calls_keyword"] += 1

    return c


def measure_file(
    root: Path, rel: str, grammar: str, parser: Parser, tally: Tally
) -> None:
    """Extract one file and add its numbers to the grammar's tally."""
    try:
        src = (root / rel).read_bytes()
    except OSError:
        return

    text = src.decode("utf-8", "replace")
    c: Counter[str] = Counter(
        files=1, lines=text.count("\n") + 1, bytes=len(src)
    )
    tree_root = parser.parse(src).root_node
    if tree_root.has_error:
        c["error_files"] = 1
        c["error_bytes"] = error_bytes(tree_root)

    fm = extract_file_generic(root, rel, grammar)
    if fm.error:
        c["extract_failed"] = 1
    truth_fn, truth_cls = truth_names(grammar, text)
    found_cls = {s.name for s in fm.symbols if s.kind in TYPE_KINDS}
    found = {s.name for s in fm.symbols}
    truth = truth_fn | truth_cls
    c["truth_fn"] = len(truth_fn)
    c["truth_cls"] = len(truth_cls)
    c["hit_fn"] = len(truth_fn & found)
    c["hit_cls"] = len(truth_cls & found)
    c["hit_cls_kind"] = len(truth_cls & found_cls)
    c["symbols"] = len(fm.symbols)
    c["names"] = len(found)
    c["extra"] = len(found - truth)
    c.update(_call_counts(fm.calls, tally))
    tally.missed.update(truth - found)
    tally.extra.update(found - truth)
    if len(tally.examples) < 3 and truth - found:
        tally.examples.append(f"{root.name}/{rel}")
    tally.counts.update(c)


def measure(roots: list[Path], only: set[str]) -> dict[str, Tally]:
    """Run the extractor over every Tier-2 file under the roots."""
    tallies: dict[str, Tally] = collections.defaultdict(Tally)
    parsers: dict[str, Parser] = {}
    for root in roots:
        files, _ = discover(root)
        for rel in files:
            grammar = tier2_grammar_for_path(rel)
            if grammar not in TRUTH or (only and grammar not in only):
                continue
            if grammar not in parsers:
                parsers[grammar] = Parser(get_grammar(grammar))
            measure_file(
                root, rel, grammar, parsers[grammar], tallies[grammar]
            )

    return tallies


def pct(a: int, b: int) -> str:
    """``a`` of ``b`` as a whole percent, or ``-`` for nothing of nothing."""
    return f"{100 * a / b:.0f}%" if b else "-"


def _recall_key(item: tuple[str, Tally]) -> tuple[float, str]:
    grammar, tally = item
    c = tally.counts
    hits = c["hit_fn"] + c["hit_cls"]
    return -hits / max(1, c["truth_fn"] + c["truth_cls"]), grammar


def summary(tallies: dict[str, Tally]) -> list[str]:
    """One row per grammar, best recall first."""
    rows = [
        f"{'grammar':16}{'files':>6}{'err%':>6}{'errB%':>6}"
        f"{'fn truth':>9}{'fn rec':>7}{'cls truth':>10}{'cls rec':>8}"
        f"{'symbols':>8}{'extra%':>8}{'calls':>8}{'attr%':>7}"
        f"{'garb%':>7}{'kw%':>6}  verdict"
    ]
    for grammar, tally in sorted(tallies.items(), key=_recall_key):
        c = tally.counts
        rows.append(
            f"{grammar:16}{c['files']:>6}"
            f"{pct(c['error_files'], c['files']):>6}"
            f"{pct(c['error_bytes'], c['bytes']):>6}"
            f"{c['truth_fn']:>9}{pct(c['hit_fn'], c['truth_fn']):>7}"
            f"{c['truth_cls']:>10}{pct(c['hit_cls'], c['truth_cls']):>8}"
            f"{c['symbols']:>8}{pct(c['extra'], c['names']):>8}"
            f"{c['calls']:>8}{pct(c['calls_attr'], c['calls']):>7}"
            f"{pct(c['calls_garbage'], c['calls']):>7}"
            f"{pct(c['calls_keyword'], c['calls']):>6}  {verdict(c)}"
        )

    return rows


def _top(counter: Counter[str], n: int) -> list[tuple[str, int]]:
    """The ``n`` most common, ties in name order so runs compare."""
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:n]


def detail(tallies: dict[str, Tally]) -> list[str]:
    """Per grammar: raw counts, missed and extra names, top callees."""
    out: list[str] = []
    for grammar in sorted(tallies):
        tally = tallies[grammar]
        counts = ", ".join(f"{k}={v}" for k, v in sorted(tally.counts.items()))
        out += [
            f"== {grammar} ==",
            f"  {counts}",
            f"  missed (top 12): {_top(tally.missed, 12)}",
            f"  extra (top 12): {_top(tally.extra, 12)}",
            f"  call names (top 15): {_top(tally.callees, 15)}",
            f"  files with misses: {tally.examples}",
            "",
        ]

    return out


def _git(dest: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(dest), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def checkout(dest: Path, url: str, commit: str, clone: bool) -> bool:
    """Make sure a pinned repository is on disk; say so if it is not.

    Returns:
        Whether ``dest`` holds a checkout to measure.
    """
    if (dest / ".git").is_dir():
        head = _git(dest, "rev-parse", "HEAD")
        if head != commit:
            print(
                f"{dest.name}: checkout is at {head[:10]}, "
                f"pinned {commit[:10]}",
                file=sys.stderr,
            )
        return True
    if not clone:
        print(f"{dest.name}: no checkout (pass --clone)", file=sys.stderr)
        return False

    dest.mkdir(parents=True, exist_ok=True)
    _git(dest, "init", "--quiet")
    _git(dest, "fetch", "--quiet", "--depth", "1", url, commit)
    _git(dest, "checkout", "--quiet", "FETCH_HEAD")
    return True


def below_bar(tallies: dict[str, Tally], pinned: dict[str, str]) -> list[str]:
    """Grammars that measure worse than the status pinned for them."""
    failures = []
    for grammar, status in sorted(pinned.items()):
        tally = tallies.get(grammar)
        if tally is None:
            continue

        got = verdict(tally.counts)
        if RANK.get(got, 0) < RANK[status]:
            failures.append(f"{grammar}: pinned {status}, measured {got}")

    return failures


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--repos-dir",
        type=Path,
        help="directory holding (or to hold) one checkout per grammar",
    )
    where.add_argument(
        "--root",
        type=Path,
        action="append",
        help="measure this checkout instead of the pinned ones; repeatable",
    )
    parser.add_argument(
        "--clone",
        action="store_true",
        help="fetch any pinned repository missing under --repos-dir",
    )
    parser.add_argument(
        "--grammar",
        action="append",
        default=[],
        help="measure only this grammar; repeatable",
    )
    parser.add_argument(
        "--detail", type=Path, help="write missed and extra names here"
    )
    parser.add_argument("--json", action="store_true", help="print JSON")
    return parser.parse_args(argv)


def _pinned_roots(
    args: argparse.Namespace, only: set[str]
) -> tuple[list[Path], dict[str, str]]:
    roots: list[Path] = []
    pinned: dict[str, str] = {}
    for entry in json.loads(FIXTURE.read_text()):
        grammar = entry["grammar"]
        if only and grammar not in only:
            continue

        dest = args.repos_dir / grammar
        if checkout(dest, entry["url"], entry["commit"], args.clone):
            roots.append(dest.resolve())
            pinned[grammar] = entry["status"]

    return roots, pinned


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark; return the process exit code."""
    if importlib.util.find_spec("tree_sitter_language_pack") is None:
        sys.exit("needs the grammar pack: pip install 'dekko[all]'")

    args = _parse_args(argv)
    only = set(args.grammar)
    pinned: dict[str, str] = {}
    if args.root:
        roots = [root.resolve() for root in args.root]
    else:
        roots, pinned = _pinned_roots(args, only)

    # A pinned repository holds files of other grammars too (a shell
    # script in a Zig project). They count toward that grammar's row
    # unless the run was narrowed to named grammars.
    tallies = measure(roots, only)
    if args.detail:
        args.detail.write_text("\n".join(detail(tallies)) + "\n")
    if args.json:
        doc = {
            grammar: {**tally.counts, "verdict": verdict(tally.counts)}
            for grammar, tally in sorted(tallies.items())
        }
        print(json.dumps(doc, indent=2))
    else:
        print("\n".join(summary(tallies)))

    failures = below_bar(tallies, pinned)
    for line in failures:
        print(f"below the bar: {line}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
