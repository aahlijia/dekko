"""Tier-2 extraction: best-effort symbols for any grammar.

Instead of per-language queries, this walks the syntax tree looking
for node types that look like definitions, classes, and calls. It
yields function names, raw parameter text, and call links — enough
for a useful map — without per-language type fidelity.
"""

import re
from pathlib import Path
from typing import Iterator

from .extractor import (
    _callee_parts,
    _doc_comment_above,
    _enclosing,
    _module_doc,
    _params_generic,
    _text,
)
from .model import FileMap, RawCall, Symbol
from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_language

_DEF_RE = re.compile(r"function|method|func\b|procedure|subroutine")
_CLASS_RE = re.compile(
    r"class|module|struct|interface|trait|impl|namespace|object"
)
_NOT_DEF_RE = re.compile(r"call|type|pattern|expression|signature")
_NOT_CLASS_RE = re.compile(r"call|pattern|expression|access")
_CALL_RE = re.compile(r"call$|call_expression|invocation")
_NAME_SPLIT = re.compile(r"[.:]+")

_CALLEE_FIELDS = ("function", "callee", "constructor")
_NAME_FIELDS = ("method", "name", "field")
_RECEIVER_FIELDS = ("receiver", "object", "operand", "value")


def extract_file_generic(root: Path, rel: str, grammar: str) -> FileMap:
    """Extract symbols and calls from a Tier-2 language file.

    Args:
        root: Repository root.
        rel: Repo-relative POSIX path of the file.
        grammar: Grammar name for ``tree-sitter-language-pack``.

    Returns:
        A ``FileMap``; on read/parse/grammar failure, one with
        ``error`` set and no symbols.
    """
    try:
        source = (root / rel).read_bytes()
        parser = Parser(get_language(grammar))
        tree = parser.parse(source)
    except Exception as exc:  # grammar download/parse can fail
        return FileMap(path=rel, language=grammar, error=str(exc))

    defs: list[tuple[Node, Symbol]] = []
    call_nodes: list[Node] = []
    seen: dict[str, int] = {}
    for node in _walk(tree.root_node):
        if _is_definition(node):
            sym = _make_symbol(node, rel, grammar, "function", seen)
            if sym is not None:
                defs.append((node, sym))
        elif _is_class(node):
            sym = _make_symbol(node, rel, grammar, "class", seen)
            if sym is not None:
                defs.append((node, sym))
        elif _CALL_RE.search(node.type):
            call_nodes.append(node)

    calls = _collect_calls(call_nodes, rel, defs)

    return FileMap(
        path=rel,
        language=grammar,
        symbols=[sym for _, sym in defs],
        calls=calls,
        doc=_module_doc(grammar, tree.root_node),
    )


def _walk(root: Node) -> Iterator[Node]:
    """Yield every named node, depth-first."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.named_children))


def _is_definition(node: Node) -> bool:
    """Heuristic: does this node define a callable?"""
    node_type = node.type
    if _NOT_DEF_RE.search(node_type):
        return False

    if not _DEF_RE.search(node_type) and node_type not in (
        "method",
        "singleton_method",
    ):
        return False

    return node.child_by_field_name("name") is not None


def _is_class(node: Node) -> bool:
    """Heuristic: does this node define a class-like container?"""
    node_type = node.type
    if _NOT_CLASS_RE.search(node_type):
        return False

    if not _CLASS_RE.search(node_type):
        return False

    return node.child_by_field_name("name") is not None


def _make_symbol(
    node: Node,
    rel: str,
    grammar: str,
    kind: str,
    seen: dict[str, int],
) -> Symbol | None:
    """Build a symbol from a heuristic definition node."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None

    raw_name = _text(name_node)
    parts = [p for p in _NAME_SPLIT.split(raw_name) if p]
    if not parts:
        return None

    name = parts[-1]
    containers = _containers(node) + parts[:-1]
    qualname = ".".join([*containers, name])
    if kind == "function" and containers:
        kind = "method"

    params = []
    params_node = node.child_by_field_name("parameters")
    if params_node is not None:
        params = _params_generic(params_node)

    ret = node.child_by_field_name(
        "return_type",
    ) or node.child_by_field_name(
        "result",
    )

    sym_id = f"{rel}::{qualname}"
    count = seen.get(sym_id, 0)
    seen[sym_id] = count + 1
    if count:
        sym_id = f"{sym_id}#{count + 1}"

    return Symbol(
        id=sym_id,
        name=name,
        qualname=qualname,
        kind=kind,
        path=rel,
        language=grammar,
        params=params,
        returns=_text(ret) if ret is not None else None,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        doc=_doc_comment_above(node),
    )


def _containers(node: Node) -> list[str]:
    """Names of class-like ancestors, outermost first."""
    names: list[str] = []
    parent = node.parent
    while parent is not None:
        if _is_class(parent):
            name_node = parent.child_by_field_name("name")
            if name_node is not None:
                names.append(_text(name_node))

        parent = parent.parent

    names.reverse()
    return names


def _collect_calls(
    call_nodes: list[Node],
    rel: str,
    defs: list[tuple[Node, Symbol]],
) -> list[RawCall]:
    """Attribute heuristic call nodes to their enclosing symbols."""
    spans = [(node.start_byte, node.end_byte, sym) for node, sym in defs]
    calls: list[RawCall] = []
    for node in call_nodes:
        parts = _call_parts(node)
        if parts is None:
            continue

        text, name, receiver = parts
        caller = _enclosing(spans, node.start_byte)
        calls.append(
            RawCall(
                caller_id=caller.id if caller else None,
                path=rel,
                text=text,
                name=name,
                receiver=receiver,
                line=node.start_point[0] + 1,
            )
        )

    return calls


def _call_parts(node: Node) -> tuple[str, str, str | None] | None:
    """Find the callee name/receiver on a heuristic call node."""
    for field_name in _CALLEE_FIELDS:
        callee = node.child_by_field_name(field_name)
        if callee is not None:
            return _callee_parts(callee)

    name_node = None
    for field_name in _NAME_FIELDS:
        name_node = node.child_by_field_name(field_name)
        if name_node is not None:
            break

    if name_node is not None:
        name = _text(name_node)
        receiver = None
        for field_name in _RECEIVER_FIELDS:
            recv = node.child_by_field_name(field_name)
            if recv is not None:
                receiver = _text(recv)
                break

        text = f"{receiver}.{name}" if receiver else name
        return text, name, receiver

    if node.named_child_count:
        return _callee_parts(node.named_children[0])

    return None
