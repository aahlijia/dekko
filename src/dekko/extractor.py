"""Tree-sitter extraction: source file → symbols, raw calls, imports."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .languages import LanguageSpec
from .model import FileMap, Import, Param, RawCall, Symbol
from tree_sitter import Node, Parser, Query, QueryCursor
from .grammars import get_grammar

_WS = re.compile(r"\s+")


def _text(node: Node) -> str:
    """Decode a node's source text, collapsing internal whitespace."""
    raw = node.text or b""
    return _WS.sub(" ", raw.decode("utf-8", "replace")).strip()


@lru_cache(maxsize=None)
def _compiled_query(grammar: str, query_str: str) -> Query:
    """Compile a query once per (grammar, query) pair."""
    return Query(get_grammar(grammar), query_str)


def _run_query(
    grammar: str, query_str: str, root: Node
) -> list[tuple[int, dict[str, list[Node]]]]:
    """Run a cached compiled query, returning its matches."""
    return QueryCursor(_compiled_query(grammar, query_str)).matches(root)


def _one(caps: dict[str, list[Node]], name: str) -> Node | None:
    """Return the first capture for a name, or ``None``."""
    nodes = caps.get(name)
    return nodes[0] if nodes else None


def extract_file(root: Path, rel: str, spec: LanguageSpec) -> FileMap:
    """Extract all symbols, calls, and imports from one source file.

    Args:
        root: Repository root.
        rel: Repo-relative POSIX path of the file.
        spec: Language spec describing how to parse it.

    Returns:
        A ``FileMap``; on read/parse failure, one with ``error`` set
        and no symbols.
    """
    try:
        source = (root / rel).read_bytes()
        parser = Parser(get_grammar(spec.grammar))
        tree = parser.parse(source)
    except (OSError, ValueError) as exc:
        return FileMap(path=rel, language=spec.name, error=str(exc))

    defs = _collect_definitions(spec, tree.root_node, rel)
    calls = _collect_calls(spec, tree.root_node, rel, defs)
    imports = _collect_imports(spec, tree.root_node, rel)
    return FileMap(
        path=rel,
        language=spec.name,
        symbols=[sym for _, sym in defs],
        calls=calls,
        imports=imports,
        doc=_module_doc(spec.name, tree.root_node),
    )


# ---------------------------------------------------------------------
# Definitions


def _collect_definitions(
    spec: LanguageSpec, root: Node, rel: str
) -> list[tuple[Node, Symbol]]:
    """Find every function/method/class definition in the tree."""
    matches = _run_query(spec.grammar, spec.definition_query, root)
    defs: list[tuple[Node, Symbol]] = []
    seen: dict[str, int] = {}
    for _, caps in matches:
        class_name = _one(caps, "classname")

        if class_name is not None:
            def_node = _one(caps, "classdef")

            if def_node is None:
                continue

            sym = _make_symbol(
                spec,
                rel,
                def_node,
                _text(class_name),
                "class",
                params=[],
                returns=None,
                seen=seen,
            )
            defs.append((def_node, sym))
            continue

        name_node = _one(caps, "name")
        def_node = _one(caps, "def")

        if name_node is None or def_node is None:
            continue

        params_node = _one(caps, "params")
        ret_node = _one(caps, "ret")
        params = (
            _parse_params(spec.param_style, params_node)
            if params_node is not None
            else []
        )

        returns = None

        if ret_node is not None:
            returns = _text(ret_node).lstrip(":").strip() or None

        sym = _make_symbol(
            spec,
            rel,
            def_node,
            _text(name_node),
            "function",
            params=params,
            returns=returns,
            seen=seen,
            receiver=_receiver_container(_one(caps, "recv")),
        )
        defs.append((def_node, sym))

    return defs


def _receiver_container(recv_node: Node | None) -> str | None:
    """Type name from a Go method receiver list, e.g. ``(s *Server)``."""
    if recv_node is None:
        return None

    stack = list(recv_node.named_children)

    while stack:
        node = stack.pop(0)

        if node.type == "type_identifier":
            return _text(node)

        stack = list(node.named_children) + stack

    return None


def _make_symbol(
    spec: LanguageSpec,
    rel: str,
    def_node: Node,
    name: str,
    kind: str,
    params: list[Param],
    returns: str | None,
    seen: dict[str, int],
    receiver: str | None = None,
) -> Symbol:
    """Build a ``Symbol`` with container qualification and unique id."""
    containers, is_method = _qualify(spec, def_node)
    if receiver is not None:
        containers.append(receiver)
        is_method = True

    if "::" in name:
        parts = [p for p in name.split("::") if p]
        containers.extend(_strip_generics(p) for p in parts[:-1])
        name = parts[-1]
        is_method = True

    qualname = ".".join([*containers, name])
    if kind == "function" and is_method:
        kind = "method"

    sym_id = f"{rel}::{qualname}"
    count = seen.get(sym_id, 0)
    seen[sym_id] = count + 1
    if count:
        sym_id = f"{sym_id}#{count + 1}"

    decorated, exported = _symbol_flags(spec.name, def_node)
    return Symbol(
        id=sym_id,
        name=name,
        qualname=qualname,
        kind=kind,
        path=rel,
        language=spec.name,
        params=params,
        returns=returns,
        start_line=def_node.start_point[0] + 1,
        end_line=def_node.end_point[0] + 1,
        decorated=decorated,
        exported=exported,
        doc=_doc_for_symbol(spec.name, def_node),
    )


def _symbol_flags(language: str, def_node: Node) -> tuple[bool, bool]:
    """Detect ``(decorated, exported)`` for a definition node.

    Best-effort and language-specific; anything not recognized is
    reported as ``(False, False)``. Implicit visibility (Go capitals,
    Python dunders) is intentionally *not* handled here — the analyzer
    derives it from the symbol name.

    Args:
        language: Registry language name.
        def_node: The definition's syntax node.

    Returns:
        Whether the symbol is decorated and whether it is exported.
    """
    return _is_decorated(language, def_node), _is_exported(language, def_node)


def _is_decorated(language: str, def_node: Node) -> bool:
    """Whether a definition carries a decorator/attribute/annotation."""
    if language == "python":
        parent = def_node.parent
        return parent is not None and parent.type == "decorated_definition"
    if language == "rust":
        return _has_prev_sibling(def_node, "attribute_item")
    if language == "java":
        return _modifiers_have(def_node, ("annotation", "marker_annotation"))
    if language in ("javascript", "typescript", "tsx"):
        return _has_child(def_node, "decorator") or _has_prev_sibling(
            def_node, "decorator"
        )
    return False


def _is_exported(language: str, def_node: Node) -> bool:
    """Whether a definition is part of the language's public surface."""
    if language == "rust":
        return _has_child(def_node, "visibility_modifier")
    if language == "java":
        return _modifiers_keyword(def_node, "public")
    if language in ("javascript", "typescript", "tsx"):
        return _ancestor_is(def_node, "export_statement", depth=4)
    return False


def _has_child(node: Node, child_type: str) -> bool:
    """Whether any direct child has the given node type."""
    return any(child.type == child_type for child in node.children)


def _has_prev_sibling(node: Node, sibling_type: str) -> bool:
    """Whether any preceding sibling has the given node type."""
    prev = node.prev_sibling
    while prev is not None:
        if prev.type == sibling_type:
            return True
        if prev.type != "comment":
            return False
        prev = prev.prev_sibling
    return False


def _ancestor_is(node: Node, ancestor_type: str, depth: int) -> bool:
    """Whether an ancestor within ``depth`` hops has the given type."""
    current = node.parent
    for _ in range(depth):
        if current is None:
            return False
        if current.type == ancestor_type:
            return True
        current = current.parent
    return False


def _modifiers_node(def_node: Node) -> Node | None:
    """The ``modifiers`` child of a Java declaration, if present."""
    for child in def_node.children:
        if child.type == "modifiers":
            return child
    return None


def _modifiers_have(def_node: Node, kinds: tuple[str, ...]) -> bool:
    """Whether the Java ``modifiers`` node contains any of ``kinds``."""
    modifiers = _modifiers_node(def_node)
    if modifiers is None:
        return False
    return any(child.type in kinds for child in modifiers.children)


def _modifiers_keyword(def_node: Node, keyword: str) -> bool:
    """Whether the Java ``modifiers`` node contains a literal keyword."""
    modifiers = _modifiers_node(def_node)
    if modifiers is None:
        return False
    return any(child.type == keyword for child in modifiers.children)


def _qualify(spec: LanguageSpec, def_node: Node) -> tuple[list[str], bool]:
    """Collect container names above a definition, outermost first.

    Returns:
        ``(container_names, is_method)`` where ``is_method`` is true
        when the immediate class-like container makes this a method.
    """
    containers: list[str] = []
    is_method = False
    node = def_node.parent
    while node is not None:
        name_field = spec.container_types.get(node.type)

        if name_field is not None:
            name_node = node.child_by_field_name(name_field)

            if name_node is not None:
                containers.append(_strip_generics(_text(name_node)))

                if node.type in spec.method_containers:
                    is_method = True

        node = node.parent

    containers.reverse()
    return containers, is_method


def _strip_generics(name: str) -> str:
    """Drop a trailing generic parameter list: ``Foo<T>`` → ``Foo``."""
    cut = name.find("<")
    return name[:cut].strip() if cut != -1 else name


# ---------------------------------------------------------------------
# Doc lines

_DOC_MAX_LEN = 100
_COMMENT_TYPES = frozenset(
    {"comment", "line_comment", "block_comment", "doc_comment"}
)
# Nodes that may sit between a doc comment and the definition itself.
_DOC_SKIP_TYPES = frozenset({"attribute_item", "decorator", "modifiers"})
# Wrappers to climb before looking at preceding siblings: comments
# precede the export/declaration statement, not the inner definition.
_DOC_CLIMB_TYPES = frozenset(
    {
        "decorated_definition",
        "export_statement",
        "lexical_declaration",
        "variable_declaration",
    }
)
_STR_PREFIX = re.compile(r"^[rRbBuUfF]{0,3}")
_COMMENT_MARKERS = ("/**", "/*!", "/*", "///", "//!", "//", "*/")


def _raw(node: Node) -> str:
    """Decode a node's source text, preserving newlines."""
    return (node.text or b"").decode("utf-8", "replace")


def _clean_doc(line: str) -> str | None:
    """Collapse whitespace and truncate a doc line."""
    line = _WS.sub(" ", line).strip()
    if len(line) > _DOC_MAX_LEN:
        line = line[: _DOC_MAX_LEN - 1].rstrip() + "…"
    return line or None


def _string_first_line(raw: str) -> str | None:
    """First non-empty content line of a string literal."""
    text = _STR_PREFIX.sub("", raw.strip(), count=1)
    for quote in ('"""', "'''", '"', "'"):
        if text.startswith(quote):
            text = text[len(quote) :]
            text = text.removesuffix(quote)
            break
    for line in text.splitlines():
        if line.strip():
            return _clean_doc(line)
    return None


def _strip_comment_markers(line: str) -> str:
    """Drop leading/trailing comment syntax from one line."""
    for marker in _COMMENT_MARKERS:
        if line.startswith(marker):
            line = line[len(marker) :]
            break
    else:
        if line.startswith("*"):
            line = line[1:]
        elif line.startswith("#"):
            line = line.lstrip("#")
    return line.removesuffix("*/").strip()


def _comment_first_line(raw: str) -> str | None:
    """First non-empty content line of a comment block."""
    for line in raw.splitlines():
        content = _strip_comment_markers(line.strip())
        if content:
            return _clean_doc(content)
    return None


def _leading_string(children: list[Node]) -> Node | None:
    """The docstring node opening a block, if any.

    Depending on grammar version a docstring appears either as a bare
    ``string`` or wrapped in an ``expression_statement``.
    """
    if not children:
        return None
    first = children[0]
    if first.type == "expression_statement" and first.named_children:
        first = first.named_children[0]
    return first if first.type == "string" else None


def _python_docstring(def_node: Node) -> str | None:
    """First docstring line of a Python function/class body."""
    body = def_node.child_by_field_name("body")
    if body is None:
        return None
    string = _leading_string(list(body.named_children))
    if string is None:
        return None
    return _string_first_line(_raw(string))


def _end_row(node: Node) -> int:
    """Last row a node occupies, excluding a trailing newline.

    Comment nodes that swallow their newline end at column 0 of the
    next row; for gap detection that next row does not count.
    """
    row, col = node.end_point
    if col == 0 and row > node.start_point[0]:
        return row - 1
    return row


def _doc_comment_above(def_node: Node) -> str | None:
    """First line of the contiguous comment block above a definition.

    Climbs wrapper nodes (export statements, declarations) first, then
    walks preceding siblings: decorators/attributes are skipped, a
    blank-line gap or an inner doc comment (``//!``, module-level)
    ends the block. The block's topmost comment supplies the line —
    for ``///`` runs that is the summary line.
    """
    node = def_node
    while node.parent is not None and node.parent.type in _DOC_CLIMB_TYPES:
        node = node.parent
    expected = node.start_point[0]
    comments: list[Node] = []
    prev = node.prev_sibling
    while prev is not None:
        if prev.type in _DOC_SKIP_TYPES:
            expected = prev.start_point[0]
            prev = prev.prev_sibling
            continue
        if prev.type not in _COMMENT_TYPES:
            break
        if _end_row(prev) < expected - 1:
            break
        if _raw(prev).lstrip().startswith(("//!", "/*!")):
            break
        comments.append(prev)
        expected = prev.start_point[0]
        prev = prev.prev_sibling
    if not comments:
        return None
    return _comment_first_line(_raw(comments[-1]))


def _doc_for_symbol(language: str, def_node: Node) -> str | None:
    """Best-effort first doc line for a definition, or ``None``."""
    if language == "python":
        return _python_docstring(def_node)
    return _doc_comment_above(def_node)


def _module_doc(language: str, root: Node) -> str | None:
    """Best-effort first doc line for a whole file, or ``None``."""
    if language == "python":
        string = _leading_string(list(root.named_children))
        if string is None:
            return None
        return _string_first_line(_raw(string))
    for child in root.named_children:
        if child.type not in _COMMENT_TYPES:
            return None
        raw = _raw(child)
        if raw.startswith("#!"):
            continue
        return _comment_first_line(raw)
    return None


# ---------------------------------------------------------------------
# Parameters


def _params_python(params_node: Node) -> list[Param]:
    """Parse a Python ``parameters`` node."""
    out: list[Param] = []
    for child in params_node.named_children:
        kind = child.type
        if kind == "identifier":
            out.append(Param(name=_text(child)))
        elif kind in ("typed_parameter", "typed_default_parameter"):
            type_node = child.child_by_field_name("type")
            name_node = child.child_by_field_name("name")
            if name_node is None:
                name_node = child.named_children[0]
            out.append(
                Param(
                    name=_text(name_node),
                    type=_text(type_node) if type_node else None,
                )
            )
        elif kind == "default_parameter":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                out.append(Param(name=_text(name_node)))
        elif kind == "list_splat_pattern":
            out.append(Param(name="*" + _text(child).lstrip("* ")))
        elif kind == "dictionary_splat_pattern":
            out.append(Param(name="**" + _text(child).lstrip("* ")))
        elif kind in ("keyword_separator", "positional_separator"):
            out.append(Param(name=_text(child)))
    return out


def _params_rust(params_node: Node) -> list[Param]:
    """Parse a Rust ``parameters`` node."""
    out: list[Param] = []
    for child in params_node.named_children:
        if child.type == "self_parameter":
            out.append(Param(name=_text(child)))
        elif child.type == "parameter":
            pattern = child.child_by_field_name("pattern")
            type_node = child.child_by_field_name("type")
            out.append(
                Param(
                    name=_text(pattern) if pattern else _text(child),
                    type=_text(type_node) if type_node else None,
                )
            )
        elif child.type == "variadic_parameter":
            out.append(Param(name="..."))
    return out


def _params_generic(params_node: Node) -> list[Param]:
    """Best-effort parse: try name/type fields, else raw text."""
    out: list[Param] = []
    for child in params_node.named_children:
        if child.type == "comment":
            continue
        name_node = child.child_by_field_name(
            "name"
        ) or child.child_by_field_name("pattern")
        type_node = child.child_by_field_name("type")
        if name_node is not None:
            out.append(
                Param(
                    name=_text(name_node),
                    type=_text(type_node) if type_node else None,
                )
            )
        else:
            out.append(Param(name=_text(child)))
    return out


def _params_c(params_node: Node) -> list[Param]:
    """Parse a C/C++ ``parameter_list`` node."""
    out: list[Param] = []
    for child in params_node.named_children:
        if child.type not in (
            "parameter_declaration",
            "optional_parameter_declaration",
        ):
            if child.type == "variadic_parameter":
                out.append(Param(name="..."))
            continue
        type_node = child.child_by_field_name("type")
        declarator = child.child_by_field_name("declarator")
        base_type = _text(type_node) if type_node else None
        if declarator is None:
            out.append(Param(name="_", type=base_type))
            continue
        decl_text = _text(declarator)
        stars = "*" * decl_text.count("*") + "&" * decl_text.count("&")
        name = _innermost_identifier(declarator) or decl_text
        full_type = f"{base_type} {stars}".strip() if base_type else None
        out.append(Param(name=name, type=full_type))
    return out


def _innermost_identifier(node: Node) -> str | None:
    """Find the identifier nested inside a C declarator."""
    if node.type in ("identifier", "field_identifier"):
        return _text(node)
    for child in node.named_children:
        found = _innermost_identifier(child)
        if found is not None:
            return found
    return None


def _params_js(params_node: Node) -> list[Param]:
    """Parse a JavaScript ``formal_parameters`` node."""
    out: list[Param] = []
    for child in params_node.named_children:
        if child.type == "identifier":
            out.append(Param(name=_text(child)))
        elif child.type == "assignment_pattern":
            left = child.child_by_field_name("left")
            out.append(Param(name=_text(left) if left else _text(child)))
        elif child.type == "rest_pattern":
            out.append(Param(name="..." + _text(child).lstrip(". ")))
        else:
            out.append(Param(name=_text(child)))
    return out


def _params_ts(params_node: Node) -> list[Param]:
    """Parse a TypeScript ``formal_parameters`` node."""
    out: list[Param] = []
    for child in params_node.named_children:
        if child.type not in ("required_parameter", "optional_parameter"):
            out.append(Param(name=_text(child)))
            continue
        pattern = child.child_by_field_name("pattern")
        type_node = child.child_by_field_name("type")
        name = _text(pattern) if pattern else _text(child)
        if child.type == "optional_parameter":
            name += "?"
        param_type = None
        if type_node is not None:
            param_type = _text(type_node).lstrip(":").strip()
        out.append(Param(name=name, type=param_type))
    return out


def _params_go(params_node: Node) -> list[Param]:
    """Parse a Go ``parameter_list`` node."""
    out: list[Param] = []
    for child in params_node.named_children:
        if child.type not in (
            "parameter_declaration",
            "variadic_parameter_declaration",
        ):
            continue
        type_node = child.child_by_field_name("type")
        param_type = _text(type_node) if type_node else None
        if child.type == "variadic_parameter_declaration":
            param_type = f"...{param_type}" if param_type else "..."
        names = child.children_by_field_name("name")
        if not names:
            out.append(Param(name="_", type=param_type))
            continue
        out.extend(
            Param(name=_text(name_node), type=param_type)
            for name_node in names
        )
    return out


_PARAM_PARSERS: dict[str, Callable[[Node], list[Param]]] = {
    "python": _params_python,
    "rust": _params_rust,
    "c": _params_c,
    "js": _params_js,
    "ts": _params_ts,
    "go": _params_go,
    "generic": _params_generic,
}


def _parse_params(style: str, params_node: Node) -> list[Param]:
    """Dispatch parameter parsing by language style."""
    parser = _PARAM_PARSERS.get(style, _params_generic)
    return parser(params_node)


# ---------------------------------------------------------------------
# Calls


def _collect_calls(
    spec: LanguageSpec, root: Node, rel: str, defs: list[tuple[Node, Symbol]]
) -> list[RawCall]:
    """Find call expressions and attribute them to enclosing defs."""
    spans = [(node.start_byte, node.end_byte, sym) for node, sym in defs]
    calls: list[RawCall] = []
    for _, caps in _run_query(spec.grammar, spec.call_query, root):
        callee = _one(caps, "callee")
        if callee is None:
            continue
        text, name, receiver = _callee_parts(callee)
        if not name:
            continue
        caller = _enclosing(spans, callee.start_byte)
        calls.append(
            RawCall(
                caller_id=caller.id if caller else None,
                path=rel,
                text=text,
                name=name,
                receiver=receiver,
                line=callee.start_point[0] + 1,
            )
        )
    return calls


_NAME_FIELDS = ("attribute", "property", "field")
_RECEIVER_FIELDS = ("object", "value", "operand", "argument", "scope", "path")
_SCOPED_TYPES = ("scoped_identifier", "qualified_identifier")


def _callee_parts(node: Node) -> tuple[str, str, str | None]:
    """Split a callee node into (full text, base name, receiver).

    Handles attribute/member/field access (``a.b``, ``a->b``), scoped
    paths (``a::b``), Java invocations, and falls back to splitting
    the raw text.
    """
    special = _callee_java(node)
    if special is not None:
        return special
    name_node = None
    for field_name in _NAME_FIELDS:
        name_node = node.child_by_field_name(field_name)
        if name_node is not None:
            break
    if name_node is None and node.type in _SCOPED_TYPES:
        name_node = node.child_by_field_name("name")
    if name_node is not None:
        receiver = None
        for field_name in _RECEIVER_FIELDS:
            recv_node = node.child_by_field_name(field_name)
            if recv_node is not None:
                receiver = _text(recv_node)
                break
        return _text(node), _text(name_node), receiver
    text = _text(node)
    if node.named_child_count == 0:
        return text, text, None
    return text, *_split_callee_text(text)


def _callee_java(node: Node) -> tuple[str, str, str | None] | None:
    """Handle Java's call shapes, which carry their own arguments."""
    if node.type == "method_invocation":
        name_node = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        name = _text(name_node) if name_node else ""
        receiver = _text(obj) if obj else None
        text = f"{receiver}.{name}" if receiver else name
        return text, name, receiver
    if node.type == "object_creation_expression":
        type_node = node.child_by_field_name("type")
        if type_node is None:
            return None
        name = _strip_generics(_text(type_node)).split(".")[-1]
        return f"new {name}", name, None
    return None


def _split_callee_text(text: str) -> tuple[str, str | None]:
    """Heuristically split callee text into (name, receiver)."""
    cleaned = re.split(r"[(<]", text, maxsplit=1)[0]
    parts = re.split(r"::|\.|->", cleaned)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return "", None
    if len(parts) == 1:
        return parts[0], None
    return parts[-1], parts[0]


def _enclosing(
    spans: list[tuple[int, int, Symbol]], byte: int
) -> Symbol | None:
    """Innermost definition whose span contains the byte offset."""
    best: Symbol | None = None
    best_size = 0
    for start, end, sym in spans:
        if start <= byte < end:
            size = end - start
            if best is None or size < best_size:
                best, best_size = sym, size
    return best


# ---------------------------------------------------------------------
# Imports


def _collect_imports(spec: LanguageSpec, root: Node, rel: str) -> list[Import]:
    """Extract imported names for the resolver, per language."""
    if spec.import_query is None:
        return []
    matches = _run_query(spec.grammar, spec.import_query, root)
    if spec.name == "python":
        return _imports_python(matches, rel)
    if spec.name == "rust":
        return _imports_rust(matches, rel)
    if spec.name in ("javascript", "typescript", "tsx"):
        return _imports_js(matches, rel)
    return _imports_generic(matches, rel)


def _imports_python(
    matches: list[tuple[int, dict[str, list[Node]]]], rel: str
) -> list[Import]:
    """Normalize Python import/from-import matches."""
    out: list[Import] = []
    for _, caps in matches:
        alias = _one(caps, "alias")
        module = _one(caps, "module")
        from_module = _one(caps, "from_module")
        name = _one(caps, "name")
        if module is not None:
            source = _text(module)
            local = _text(alias) if alias else source.split(".")[0]
            out.append(Import(path=rel, name=local, source=source))
        elif from_module is not None and name is not None:
            base = _text(from_module)
            imported = _text(name)
            local = _text(alias) if alias else imported.split(".")[-1]
            # Relative bases ("." / "..") already end in a dot; joining
            # with another "." would double it (e.g. ``..contextpack``).
            sep = "" if base.endswith(".") else "."
            out.append(
                Import(path=rel, name=local, source=f"{base}{sep}{imported}")
            )
    return out


def _imports_rust(
    matches: list[tuple[int, dict[str, list[Node]]]], rel: str
) -> list[Import]:
    """Flatten Rust ``use`` declarations into imported names."""
    out: list[Import] = []
    for _, caps in matches:
        use = _one(caps, "use")
        if use is None:
            continue
        for name, source in _parse_rust_use(_text(use)):
            out.append(Import(path=rel, name=name, source=source))
    return out


def _imports_js(
    matches: list[tuple[int, dict[str, list[Node]]]], rel: str
) -> list[Import]:
    """Normalize JS/TS import statements (named and default)."""
    out: list[Import] = []
    for _, caps in matches:
        module = _one(caps, "from_module")
        name = _one(caps, "name")
        if module is None or name is None:
            continue
        source = _strip_quotes(_text(module))
        alias = _one(caps, "alias")
        local = _text(alias) if alias else _text(name)
        out.append(
            Import(path=rel, name=local, source=f"{source}/{_text(name)}")
        )
    return out


def _imports_generic(
    matches: list[tuple[int, dict[str, list[Node]]]], rel: str
) -> list[Import]:
    """Fallback: any ``@name``/``@module`` capture becomes an import."""
    out: list[Import] = []
    for _, caps in matches:
        node = _one(caps, "module") or _one(caps, "name")
        if node is None:
            continue
        source = _strip_quotes(_text(node))
        alias = _one(caps, "alias")
        name = _text(alias) if alias else re.split(r"[./:]", source)[-1]
        out.append(Import(path=rel, name=name, source=source))
    return out


def _strip_quotes(text: str) -> str:
    """Drop string quotes and include angle brackets."""
    return text.strip("\"'<>")


def _parse_rust_use(text: str) -> list[tuple[str, str]]:
    """Expand a ``use`` argument into ``(local_name, source)`` pairs.

    Handles plain paths, ``as`` renames, nested ``{...}`` groups, and
    skips glob imports.

    Args:
        text: The argument of a ``use`` declaration, e.g.
            ``a::b::{c, d as e}``.

    Returns:
        One pair per imported name.
    """
    text = text.strip().rstrip(";")
    brace = text.find("{")
    if brace == -1:
        return _rust_use_leaf(text)
    prefix = text[:brace].rstrip(": ")
    inner = text[brace + 1 : text.rfind("}")]
    out: list[tuple[str, str]] = []
    for part in _split_top_level(inner):
        full = f"{prefix}::{part}" if prefix else part
        out.extend(_parse_rust_use(full))
    return out


def _rust_use_leaf(path: str) -> list[tuple[str, str]]:
    """Resolve a brace-free use path to its local binding."""
    path = path.strip()
    if not path or path.endswith("*"):
        return []
    if " as " in path:
        source, local = path.rsplit(" as ", 1)
        return [(local.strip(), source.strip())]
    name = path.split("::")[-1].strip()
    if name == "self":
        parts = path.split("::")
        name = parts[-2].strip() if len(parts) > 1 else ""
        path = "::".join(parts[:-1])
    return [(name, path)] if name else []


def _split_top_level(text: str) -> list[str]:
    """Split on commas not nested inside braces."""
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts
