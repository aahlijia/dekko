"""Tree-sitter extraction: source file → symbols, raw calls, imports."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Callable

from dekko.core.languages import LanguageSpec
from dekko.core.model import (
    TYPE_KINDS,
    FileMap,
    Import,
    Param,
    RawCall,
    RawHeritage,
    RawRef,
    Symbol,
)
from tree_sitter import Node, Parser, Query, QueryCursor
from dekko.core.grammars import get_grammar

_WS = re.compile(r"\s+")

# Tree-sitter node type -> ``Symbol.kind`` for ``@classdef`` matches
# (looked up against ``@classkind`` when present, else the ``@classdef``
# node itself — Go's struct/interface both surface as the same
# ``type_declaration`` wrapper, so those two definitions capture an
# inner ``@classkind`` node to disambiguate). Anything not listed here
# keeps the default ``"class"`` (plain classes across every language
# need no entry).
_CLASSDEF_KIND: dict[str, str] = {
    "interface_declaration": "interface",
    "interface_type": "interface",
    "enum_declaration": "enum",
    "enum_item": "enum",
    "record_declaration": "record",
    "struct_specifier": "struct",
    "struct_item": "struct",
    "struct_type": "struct",
    "trait_item": "trait",
}


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
    if spec.name == "rust":
        calls.extend(_collect_rust_macro_calls(tree.root_node, rel, defs))
    refs = _collect_refs(spec, tree.root_node, rel, defs)
    heritage = _collect_heritage(spec, tree.root_node, rel, defs)
    imports = _collect_imports(spec, tree.root_node, rel)
    return FileMap(
        path=rel,
        language=spec.name,
        symbols=[sym for _, sym in defs],
        calls=calls,
        refs=refs,
        heritage=heritage,
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

            kind_node = _one(caps, "classkind")
            kind_type = (
                kind_node.type if kind_node is not None else def_node.type
            )
            sym = _make_symbol(
                spec,
                rel,
                def_node,
                _text(class_name),
                _CLASSDEF_KIND.get(kind_type, "class"),
                params=[],
                returns=None,
                seen=seen,
            )
            defs.append((def_node, sym))
            continue

        var_def = _one(caps, "vardef")

        if var_def is not None:
            value_node = _one(caps, "value")
            name_node = _one(caps, "name")

            # Arrow/function-expression values are already captured by
            # the dedicated function-definition patterns above; skip
            # them here to avoid a duplicate symbol for the same node.
            if (
                name_node is None
                or value_node is None
                or value_node.type in ("arrow_function", "function_expression")
            ):
                continue

            sym = _make_symbol(
                spec,
                rel,
                var_def,
                _text(name_node),
                "variable",
                params=[],
                returns=None,
                seen=seen,
            )
            defs.append((var_def, sym))
            continue

        name_node = _one(caps, "name")
        def_node = _one(caps, "def")

        if name_node is None or def_node is None:
            continue

        params_node = _one(caps, "params")

        if _looks_like_c_macro_invocation(
            spec.name, _text(name_node), params_node
        ):
            continue

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


# ALL-CAPS-with-underscores is the near-universal C/C++ convention for
# function-like macros (``TF_DEVICELIST_METHOD``, ``EXPECT_DEATH``,
# ``TF_ASSERT_OK_AND_ASSIGN``, ...). Combined with a malformed
# parameter list (see ``_looks_like_c_macro_invocation``), this
# reliably identifies an unexpanded macro invocation misparsed as a
# function definition without also flagging legitimate ALL-CAPS
# macro-shaped test helpers like gtest's ``TEST(Suite, Case) { ... }``
# — those parse with a syntactically clean (if semantically nonsense)
# parameter list, so requiring an actual parse error keeps this from
# over-triggering (verified empirically against round 15's
# tensorflow/zed/spring-boot/cline/awesome-go/claude-code test-repos
# corpus: 228 flagged out of 137,705 C/C++ definitions, one false
# negative risk accepted — see the macro-extraction-gaps plan).
_ALL_CAPS_NAME = re.compile(r"^[A-Z_]*[A-Z][A-Z0-9_]*$")


def _looks_like_c_macro_invocation(
    language: str, name: str, params_node: Node | None
) -> bool:
    """Detect a C/C++ macro invocation misparsed as a function def.

    Tree-sitter's C/C++ grammars have no dedicated node type for an
    unexpanded, function-like macro invocation at file/namespace
    scope (``FOO(a, b, c);``) — dekko never runs a preprocessor, by
    design. Best-effort grammar error recovery sometimes lands on a
    ``function_definition``/``function_declarator`` shape anyway,
    whose "name" is the macro's own name and whose "parameters" node
    itself contains a syntax error (round 15's ``TF_DEVICELIST_
    METHOD`` finding in tensorflow's C API layer — see
    ``round15-macro-extraction-gaps-plan.md`` Track B). Symbols
    matching this shape are dropped rather than emitted garbled.

    Note: this only suppresses the one garbled symbol. Definitions
    that tree-sitter's error recovery swallows entirely into the same
    malformed subtree (real functions textually following the macro
    invocation) are not recovered by this check — they were already
    invisible to ``_collect_definitions`` before this function runs,
    since no separate query match exists for them. Recovering those
    would need source-level preprocessing before parsing, out of
    scope here.

    Args:
        language: Registry language name (only ``c``/``cpp`` apply).
        name: The captured definition name.
        params_node: The captured parameter-list node, or ``None``.

    Returns:
        Whether this definition should be treated as a probable
        macro invocation rather than a real function.
    """
    if language not in ("c", "cpp"):
        return False
    if params_node is None or not params_node.has_error:
        return False

    return bool(_ALL_CAPS_NAME.match(name))


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
    containers, is_method, in_test_module = _qualify(spec, def_node)
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
        test=in_test_module,
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


# Rust ``mod`` names conventionally used for inline ``#[cfg(test)]``
# unit-test submodules co-located with the production code they test
# (``mod tests { ... }`` at the bottom of the same file). Shares the
# same two literal values as ``classify.TEST_DIR_PARTS``' bare-name
# test-directory check, kept as a separate constant here since this is
# an AST-context signal, not a path-segment one — see ``_qualify``'s
# ``in_test_module`` return value and round 11 master report #7.
_RUST_TEST_MOD_NAMES = frozenset({"tests", "test"})


def _qualify(
    spec: LanguageSpec, def_node: Node
) -> tuple[list[str], bool, bool]:
    """Collect container names above a definition, outermost first.

    The climb stops dead at the first enclosing function/method/
    closure (``spec.function_boundary_types``): a definition nested
    inside another function body is a closure-local helper, never a
    member of whatever contains that outer function, so anything
    collected before reaching a boundary is discarded rather than
    attributed to a class several levels further up.

    Returns:
        ``(container_names, is_method, in_test_module)`` — ``is_method``
        is true when the immediate class-like container makes this a
        method; ``in_test_module`` is true when the climb passed
        through a Rust ``mod_item`` container conventionally used for
        inline unit tests (a bare module name of ``tests``/``test``),
        the dominant Rust pattern for co-locating
        ``#[cfg(test)]``-gated test code with the production code it
        tests. Always ``False`` for every other language.
    """
    containers: list[str] = []
    is_method = False
    in_test_module = False
    node = def_node.parent
    while node is not None:
        if node.type in spec.function_boundary_types:
            return [], False, False

        name_field = spec.container_types.get(node.type)

        if name_field is not None:
            name_node = node.child_by_field_name(name_field)

            if name_node is not None:
                name_text = _strip_generics(_text(name_node))
                containers.append(name_text)

                if node.type in spec.method_containers:
                    is_method = True

                if (
                    spec.name == "rust"
                    and node.type == "mod_item"
                    and name_text in _RUST_TEST_MOD_NAMES
                ):
                    in_test_module = True

        node = node.parent

    containers.reverse()
    return containers, is_method, in_test_module


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


# Rust macros whose arguments are ordinary expressions in practice
# (even though tree-sitter-rust never parses them as such — see
# ``_collect_rust_macro_calls``). Deliberately narrow: the highest-
# value, lowest-risk subset (round 15's assert-family recommendation)
# rather than an attempt at general macro-argument parsing.
_RUST_ASSERT_MACROS = frozenset(
    {
        "assert",
        "assert_eq",
        "assert_ne",
        "debug_assert",
        "debug_assert_eq",
        "debug_assert_ne",
    }
)


def _collect_rust_macro_calls(
    root: Node, rel: str, defs: list[tuple[Node, Symbol]]
) -> list[RawCall]:
    """Recover calls made as arguments to assert-family macros.

    tree-sitter-rust does not parse a macro invocation's arguments as
    Rust expression syntax — ``assert_eq!(helper(1), 2)``'s argument
    list is an opaque ``token_tree`` of raw tokens, not a structured
    ``call_expression``, so ``_collect_calls``'s tree-sitter-query
    mechanism has nothing to match inside it. This is a real, common
    coverage gap: ``assert!``/``assert_eq!`` wrapping a direct call is
    an everyday Rust test idiom, and a call made only this way was
    previously invisible to the call graph entirely (round 15's zed
    finding — see ``round15-macro-extraction-gaps-plan.md`` Track A).

    Scans ``_RUST_ASSERT_MACROS`` invocations' token trees for
    ``identifier(...)``/``identifier.identifier(...)``-shaped
    subsequences and treats each as a call site. General user-defined
    macro bodies are out of scope.

    Args:
        root: Parsed file's root node.
        rel: Repo-relative POSIX path of the file.
        defs: Definitions found in this file, used to attribute each
            recovered call to its enclosing function.

    Returns:
        Recovered ``RawCall``s, one per call-shaped site found inside
        a target macro's arguments.
    """
    spans = [(node.start_byte, node.end_byte, sym) for node, sym in defs]
    calls: list[RawCall] = []
    _find_target_macro_invocations(root, rel, spans, calls)
    return calls


def _find_target_macro_invocations(
    node: Node,
    rel: str,
    spans: list[tuple[int, int, Symbol]],
    calls: list[RawCall],
) -> None:
    """Recurse the whole tree for target-macro invocations."""
    if node.type == "macro_invocation":
        macro_name = node.child_by_field_name("macro")
        token_tree = _first_child_of_type(node, "token_tree")
        if (
            macro_name is not None
            and token_tree is not None
            and _text(macro_name) in _RUST_ASSERT_MACROS
        ):
            for site, name, receiver in _rust_macro_call_sites(token_tree):
                caller = _enclosing(spans, site.start_byte)
                text = f"{receiver}.{name}" if receiver else name
                calls.append(
                    RawCall(
                        caller_id=caller.id if caller else None,
                        path=rel,
                        text=text,
                        name=name,
                        receiver=receiver,
                        line=site.start_point[0] + 1,
                    )
                )

    for child in node.children:
        _find_target_macro_invocations(child, rel, spans, calls)


def _first_child_of_type(node: Node, node_type: str) -> Node | None:
    """First direct child matching a node type, or ``None``."""
    for child in node.children:
        if child.type == node_type:
            return child
    return None


def _rust_macro_call_sites(
    token_tree: Node,
) -> list[tuple[Node, str, str | None]]:
    """Find identifier-shaped call sites inside a macro's token tree.

    Structural, not textual: a call written inside a macro argument
    still nests correctly as an ``identifier`` node immediately
    followed by a sibling ``token_tree`` node whose own text starts
    with ``(`` — tree-sitter still balances the raw token stream's
    brackets, it just doesn't type them as ``call_expression``/
    ``arguments``. Recurses into nested token trees, so
    ``outer(inner())`` finds both calls for free (the nesting is
    already structural, no extra bracket-matching needed).

    Args:
        token_tree: A macro invocation's ``token_tree`` argument node.

    Returns:
        ``(identifier_node, name, receiver)`` for each call-shaped
        site found, in document order.
    """
    found: list[tuple[Node, str, str | None]] = []
    _scan_rust_token_tree(token_tree, found)
    return found


def _scan_rust_token_tree(
    node: Node, found: list[tuple[Node, str, str | None]]
) -> None:
    """Depth-first scan for ``identifier(...)``-shaped subsequences."""
    children = node.children
    for i, child in enumerate(children):
        if child.type == "identifier":
            nxt = children[i + 1] if i + 1 < len(children) else None
            if (
                nxt is not None
                and nxt.type == "token_tree"
                and _text(nxt).startswith("(")
            ):
                receiver = None
                if (
                    i >= 2
                    and children[i - 1].type in (".", "::")
                    and children[i - 2].type == "identifier"
                ):
                    receiver = _text(children[i - 2])
                found.append((child, _text(child), receiver))
        elif child.type == "token_tree":
            _scan_rust_token_tree(child, found)


def _collect_refs(
    spec: LanguageSpec, root: Node, rel: str, defs: list[tuple[Node, Symbol]]
) -> list[RawRef]:
    """Find bare-identifier value references, attributed to enclosing defs.

    Captures identifiers used as *values* rather than invoked —
    object-literal property values, array elements, call arguments,
    and assignment/declarator right-hand sides (see ``languages.py``'s
    per-language ``reference_query``) — the pass-by-reference usage a
    plain call-expression query structurally cannot see (bug #2b).
    Returns an empty list for languages with no ``reference_query``
    yet.
    """
    if spec.reference_query is None:
        return []
    spans = [(node.start_byte, node.end_byte, sym) for node, sym in defs]
    refs: list[RawRef] = []
    for _, caps in _run_query(spec.grammar, spec.reference_query, root):
        ref_node = _one(caps, "ref")
        if ref_node is None:
            continue
        name = _text(ref_node)
        if not name:
            continue
        caller = _enclosing(spans, ref_node.start_byte)
        refs.append(
            RawRef(
                caller_id=caller.id if caller else None,
                path=rel,
                name=name,
                line=ref_node.start_point[0] + 1,
            )
        )
    return refs


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
# Heritage (extends/implements clauses)

# Python ``superclasses`` argument_list entries that are real base
# names: a bare identifier, an ``attribute`` access (``mod.Base``), or
# a ``subscript`` (``Generic[T]``/``Protocol[T]``-shaped — the design
# doc's own documented edge case: no attempt is made to distinguish a
# structural-typing marker from a real base, both resolve the same
# way). ``keyword_argument`` (``metaclass=Meta``) is deliberately
# absent — filtered out by simply never matching one of these types.
_PY_BASE_TYPES = ("identifier", "attribute")


def _heritage_python(bases_node: Node) -> list[tuple[Node, str]]:
    """Walk a Python ``superclasses`` argument_list into base nodes.

    Every kept entry is an ``extends`` clause — Python has no syntactic
    distinction between "extends a class" and "implements an
    interface," everything is ``class X(Y):``.
    """
    out: list[tuple[Node, str]] = []
    for child in bases_node.named_children:
        if child.type in _PY_BASE_TYPES:
            out.append((child, "extends"))
        elif child.type == "subscript":
            value = child.child_by_field_name("value")
            if value is not None and value.type in _PY_BASE_TYPES:
                out.append((value, "extends"))
        # keyword_argument (metaclass=...) and anything else (a
        # *args/**kwargs splat) are not base names; skip silently.
    return out


def _heritage_js(heritage_node: Node) -> list[tuple[Node, str]]:
    """Walk a JS ``class_heritage`` node: always exactly one ``extends``.

    Unlike TypeScript, plain JS's ``class_heritage`` has no
    ``extends_clause`` wrapper — its sole named child *is* the base
    type expression directly (verified against the pinned
    tree-sitter-javascript grammar).
    """
    children = heritage_node.named_children
    return [(children[0], "extends")] if children else []


def _heritage_ts(heritage_node: Node) -> list[tuple[Node, str]]:
    """Walk a TS ``class_heritage``/``extends_type_clause`` container.

    ``class_heritage`` (class/abstract-class declarations) wraps an
    optional ``extends_clause`` (single ``value`` field) and/or
    ``implements_clause`` (a flat list of unfielded type children).
    ``extends_type_clause`` (interface declarations) is itself already
    the flat list — an interface extending other interfaces.
    """
    if heritage_node.type == "extends_type_clause":
        return [(c, "extends") for c in heritage_node.named_children]
    out: list[tuple[Node, str]] = []
    for child in heritage_node.named_children:
        if child.type == "extends_clause":
            value = child.child_by_field_name("value")
            if value is not None:
                out.append((value, "extends"))
        elif child.type == "implements_clause":
            out.extend((c, "implements") for c in child.named_children)
    return out


def _type_list_children(node: Node) -> list[Node]:
    """A Java heritage container's actual type nodes.

    Java's ``superclass`` field wraps its single base type directly as
    a named child, but ``super_interfaces``/``extends_interfaces`` wrap
    a ``type_list`` node one level down instead (verified against the
    pinned tree-sitter-java grammar) — this drills into that wrapper
    when present so every Java heritage container can be walked the
    same way regardless of which of the two shapes it is.
    """
    type_list = _first_child_of_type(node, "type_list")
    return (
        type_list.named_children
        if type_list is not None
        else node.named_children
    )


def _heritage_java(caps: dict[str, list[Node]]) -> list[tuple[Node, str]]:
    """Combine a Java ``@classdef`` match's heritage captures.

    A single ``class_declaration`` match can carry both ``@superclass``
    (``extends``, at most one) and ``@interfaces`` (``implements``, a
    list) at once; an ``interface_declaration`` match instead carries
    ``@heritage`` (its own ``extends_interfaces``, also a list, still
    an ``extends`` relation since one interface extending another is
    not "implementing").
    """
    out: list[tuple[Node, str]] = []
    superclass = _one(caps, "superclass")
    if superclass is not None:
        out.extend((c, "extends") for c in _type_list_children(superclass))
    interfaces = _one(caps, "interfaces")
    if interfaces is not None:
        out.extend((c, "implements") for c in _type_list_children(interfaces))
    heritage = _one(caps, "heritage")
    if heritage is not None:
        out.extend((c, "extends") for c in _type_list_children(heritage))
    return out


def _heritage_cpp(clause_node: Node) -> list[tuple[Node, str]]:
    """Walk a C++ ``base_class_clause`` into ``(type_node, "extends")``.

    Each base is either a bare type node (``struct S : Base1 {}`` —
    no explicit access specifier; a struct base defaults to public and
    carries no ``access_specifier`` sibling at all) or preceded by its
    own ``access_specifier`` wrapper node (``public``/``private``/
    ``protected``) that must be stripped, not treated as part of the
    type name — the design doc's own documented pitfall
    (``"public Base"`` would never equal any real symbol name).
    Verified against the pinned tree-sitter-cpp grammar:
    ``base_class_clause``'s named children are a flat sequence, each
    base's own ``access_specifier`` (when present) a preceding
    sibling, not nested inside the type node. C++ has no syntactic
    extends/implements distinction — every base, public or private, is
    an ``extends`` relation.
    """
    return [
        (child, "extends")
        for child in clause_node.named_children
        if child.type != "access_specifier"
    ]


def _heritage_rust_bounds(bounds_node: Node) -> list[tuple[Node, str]]:
    """Walk a Rust ``trait_bounds`` node into supertrait entries.

    ``trait Sub: Super + Clone`` bounds every named supertrait the
    same way — Rust has no syntactic distinction for a supertrait
    bound, it's always ``extends`` (mirrors how Python's
    ``class X(Y):`` collapses everything to ``extends`` too). A
    lifetime bound (``trait Sub<'a>: 'a + Super``) surfaces as its own
    ``lifetime`` named child here, not a type — filtered out, since a
    lifetime is not a supertrait and has no symbol to resolve against
    (verified against the pinned tree-sitter-rust grammar).
    """
    return [
        (child, "extends")
        for child in bounds_node.named_children
        if child.type != "lifetime"
    ]


def _heritage_rust_impl(
    caps: dict[str, list[Node]],
    rel: str,
    defs: list[tuple[Node, Symbol]],
) -> list[RawHeritage]:
    """Resolve one ``impl Trait for Type`` block into a ``RawHeritage``.

    Structurally different from every other language's heritage shape
    (see ``LanguageSpec.heritage_query``'s docstring): the clause
    isn't attached to the type's own definition node, so there's no
    ``@classdef`` span to correlate against the way ``_collect_
    heritage``'s main loop does for every other language. Instead,
    ``@impl_type``'s name is looked up against this file's own
    already-extracted ``TYPE_KINDS`` symbols by exact name match —
    Rust ``impl`` blocks are almost always in the same file as the
    type they're for, though not required by the language. When the
    type isn't defined in this file, or its name is ambiguous within
    it (same-named types in two different ``mod`` blocks — legal but
    rare), no symbol id exists to attach a ``RawHeritage`` to
    (``subtype_id`` is never ``None``), so the impl block is silently
    skipped rather than guessed at.

    An inherent ``impl Type { ... }`` block (no ``trait:`` field)
    never reaches this function — ``heritage_query``'s ``trait: (_)``
    field requirement means the query itself never matches one (the
    design's own flagged false-signal risk: inherent impls vastly
    outnumber trait impls in typical Rust code).
    """
    impl_trait = _one(caps, "impl_trait")
    impl_type = _one(caps, "impl_type")
    if impl_trait is None or impl_type is None:
        return []
    _, type_name, _ = _heritage_name_parts(impl_type)
    if not type_name:
        return []
    candidates = [
        sym
        for _, sym in defs
        if sym.kind in TYPE_KINDS and sym.name == type_name
    ]
    if len(candidates) != 1:
        return []
    text, name, receiver = _heritage_name_parts(impl_trait)
    if not name:
        return []
    return [
        RawHeritage(
            subtype_id=candidates[0].id,
            path=rel,
            text=text,
            name=name,
            receiver=receiver,
            relation="impl",
            line=impl_trait.start_point[0] + 1,
        )
    ]


def _heritage_entries(
    language: str, caps: dict[str, list[Node]]
) -> list[tuple[Node, str]]:
    """Dispatch one ``@classdef`` match's captures to its language parser.

    Returns ``(type_node, relation)`` pairs — mirrors how ``_params_*``
    returns one entry per parameter, just for heritage clauses instead.
    Rust's ``impl Trait for Type`` heritage (``@implblock`` matches,
    which carry no ``@classdef``) is handled separately by
    ``_heritage_rust_impl``, called directly from ``_collect_heritage``
    before this dispatch ever runs — only Rust's supertrait-bound
    shape (``@classdef``-attached, like every other language here)
    reaches this function for Rust.
    """
    if language == "python":
        bases = _one(caps, "bases")
        return _heritage_python(bases) if bases is not None else []
    if language == "javascript":
        heritage = _one(caps, "heritage")
        return _heritage_js(heritage) if heritage is not None else []
    if language in ("typescript", "tsx"):
        heritage = _one(caps, "heritage")
        return _heritage_ts(heritage) if heritage is not None else []
    if language == "java":
        return _heritage_java(caps)
    if language == "cpp":
        heritage = _one(caps, "heritage")
        return _heritage_cpp(heritage) if heritage is not None else []
    if language == "rust":
        bounds = _one(caps, "bounds")
        return _heritage_rust_bounds(bounds) if bounds is not None else []
    return []


def _heritage_name_parts(node: Node) -> tuple[str, str, str | None]:
    """Split a heritage clause's type node into (text, name, receiver).

    Reuses ``_split_callee_text`` — the same dotted/scoped-path
    splitting a callee expression already needs (``mod.Base`` ->
    name ``Base``, receiver ``mod``; ``Comparable<Foo>`` -> name
    ``Comparable``, its generic argument stripped the same way a call's
    argument list already is).
    """
    text = _text(node)
    name, receiver = _split_callee_text(text)
    return text, name, receiver


def _collect_heritage(
    spec: LanguageSpec, root: Node, rel: str, defs: list[tuple[Node, Symbol]]
) -> list[RawHeritage]:
    """Find extends/implements/impl-for clauses on type definitions.

    Mirrors ``_collect_refs``'s "return empty for languages without a
    query yet" shape. Each ``heritage_query`` match's ``@classdef``
    node is correlated back to the ``Symbol`` ``_collect_definitions``
    already built for it by exact byte span (the same node, re-matched
    by a second query pass — see ``_collect_calls``/``_enclosing`` for
    the analogous span-based correlation used for call attribution),
    restricted to ``TYPE_KINDS`` symbols so a match can never
    accidentally attach to something else.

    Rust's ``impl Trait for Type`` matches (``@implblock``) carry no
    ``@classdef`` at all — a structurally different shape handled
    before span correlation ever runs, via same-file name lookup in
    ``_heritage_rust_impl`` instead.
    """
    if spec.heritage_query is None:
        return []
    by_span = {
        (node.start_byte, node.end_byte): sym
        for node, sym in defs
        if sym.kind in TYPE_KINDS
    }
    out: list[RawHeritage] = []
    for _, caps in _run_query(spec.grammar, spec.heritage_query, root):
        if "implblock" in caps:
            out.extend(_heritage_rust_impl(caps, rel, defs))
            continue
        classdef = _one(caps, "classdef")
        if classdef is None:
            continue
        sym = by_span.get((classdef.start_byte, classdef.end_byte))
        if sym is None:
            continue
        for node, relation in _heritage_entries(spec.name, caps):
            text, name, receiver = _heritage_name_parts(node)
            if not name:
                continue
            out.append(
                RawHeritage(
                    subtype_id=sym.id,
                    path=rel,
                    text=text,
                    name=name,
                    receiver=receiver,
                    relation=relation,
                    line=node.start_point[0] + 1,
                )
            )
    return out


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
    if spec.name in ("c", "cpp"):
        return _imports_cpp(matches, rel)
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


def _imports_cpp(
    matches: list[tuple[int, dict[str, list[Node]]]], rel: str
) -> list[Import]:
    """Normalize C/C++ ``#include``s.

    Unlike Python/JS/Rust imports, a ``#include`` binds no single
    symbol name — it textually includes an entire header, so there is
    no per-symbol local binding the generic fallback's name-derivation
    can recover. That fallback (``_imports_generic``) splits the
    include path on ``[./:]`` and keeps the *last* segment, which for
    ``#include "tensorflow/core/data/rewrite_utils.h"`` is the literal
    string ``"h"`` (the extension) — never usable as a lookup key, and
    colliding across nearly every C/C++ ``#include`` in a file (all
    typically ending in ``.h``/``.hpp``), which silently dropped all
    but the first such include from ``resolver.py``'s
    ``_imports_by_file`` dedupe-by-name dict. This derives the
    header's own stem instead (``rewrite_utils``) — still not a real
    per-symbol binding, but a stable, mostly-unique-per-file key, and
    a far more useful label wherever ``Import.name`` is displayed
    (``contextpack.py``). ``resolver.py``'s ``_import_match`` actually
    disambiguates C/C++ calls via each import's full ``source`` path
    (see its whole-file-include fallback), not this ``name`` — see
    ``test-repos/reports/investigation-1.5-cpp-gtest-affected.md``.
    """
    out: list[Import] = []
    for _, caps in matches:
        node = _one(caps, "module")
        if node is None:
            continue
        source = _strip_quotes(_text(node))
        base = source.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0] if "." in base else base
        out.append(Import(path=rel, name=stem or source, source=source))
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
