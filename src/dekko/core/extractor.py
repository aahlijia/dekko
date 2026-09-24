"""Tree-sitter extraction: source file → symbols, raw calls, imports."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Callable, NamedTuple

from dekko.core.languages import LanguageSpec
from dekko.core.model import (
    TYPE_KINDS,
    EnvRead,
    FileMap,
    Import,
    Param,
    RawCall,
    RawCatch,
    RawHeritage,
    RawRef,
    RawThrow,
    Symbol,
    TypeUse,
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
    "type_alias_declaration": "type_alias",
    "type_item": "type_alias",
}


def _text(node: Node) -> str:
    """Decode a node's source text, collapsing internal whitespace."""
    raw = node.text or b""
    return _WS.sub(" ", raw.decode("utf-8", "replace")).strip()


@lru_cache(maxsize=None)
def _compiled_query(grammar: str, query_str: str) -> Query:
    """Compile a query once per (grammar, query) pair."""
    return Query(get_grammar(grammar), query_str)


_QueryMatches = list[tuple[int, dict[str, list[Node]]]]


def _run_query(grammar: str, query_str: str, root: Node) -> _QueryMatches:
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
    if spec.name in ("c", "cpp"):
        calls.extend(_collect_cpp_ctor_arg_calls(tree.root_node, rel, defs))
    import_matches = _import_matches(spec, tree.root_node)
    refs = _collect_refs(
        spec,
        tree.root_node,
        rel,
        defs,
        _import_binding_bytes(import_matches),
    )
    heritage = _collect_heritage(spec, tree.root_node, rel, defs)
    throws = _collect_throws(spec, tree.root_node, rel, defs)
    catches = _collect_catches(spec, tree.root_node, rel, defs)
    env_reads = _collect_env_reads(spec, tree.root_node, rel, defs)
    imports = _collect_imports(spec, rel, import_matches)
    type_aliases = _collect_type_aliases(spec, tree.root_node)
    enum_variants = _collect_enum_variants(spec, tree.root_node)
    type_uses = _collect_type_uses(spec, tree.root_node, rel, defs)
    return FileMap(
        path=rel,
        language=spec.name,
        symbols=[sym for _, sym in defs],
        calls=calls,
        refs=refs,
        heritage=heritage,
        throws=throws,
        catches=catches,
        env_reads=env_reads,
        imports=imports,
        type_aliases=type_aliases,
        enum_variants=enum_variants,
        type_uses=type_uses,
        doc=_module_doc(spec.name, tree.root_node),
    )


# Node types produced only by a genuine C++ construct -- the C grammar
# has no ``class``/``namespace``/``template`` productions at all, so
# these never appear in a parse tree built with that grammar. Used to
# disambiguate a ``.h`` file's actual language from its own content
# (both C and C++ claim the extension; see
# ``repo_ops._resolve_header_spec``).
_CPP_HEADER_MARKER_QUERY = """
(class_specifier) @marker
(namespace_definition) @marker
(template_declaration) @marker
"""


def looks_like_cpp_header(source: bytes) -> bool:
    """Whether ``source`` contains a genuine C++-only construct.

    Parses ``source`` with the C++ tree-sitter grammar (a Tier-1,
    offline dependency -- no optional-grammar gap) and checks whether
    the resulting parse tree contains a ``class_specifier``,
    ``namespace_definition``, or ``template_declaration`` node
    anywhere. Tree-sitter's error recovery means this still classifies
    correctly around unrelated parse trouble elsewhere in the file
    (unknown macros, etc.) -- verified live against a plain C header,
    an ``extern "C"``-wrapped C header, and a C file using ``class``/
    ``template`` as ordinary identifiers (legal in C, not in C++):
    none of these three marker node types appear for any of them.

    Args:
        source: Raw file bytes.

    Returns:
        True if a C++-only construct was found anywhere in the parse
        tree.
    """
    parser = Parser(get_grammar("cpp"))
    tree = parser.parse(source)
    matches = _run_query("cpp", _CPP_HEADER_MARKER_QUERY, tree.root_node)
    return bool(matches)


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
                params=_tuple_struct_fields(def_node),
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
# over-triggering (verified empirically against a
# tensorflow/zed/spring-boot/cline/awesome-go/claude-code corpus: 228
# flagged out of 137,705 C/C++ definitions, one false negative risk
# accepted).
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
    itself contains a syntax error (e.g. ``TF_DEVICELIST_METHOD`` in
    tensorflow's C API layer). Symbols
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
# ``in_test_module`` return value.
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


# A leading comment/docstring line that reads as copyright/license
# boilerplate rather than a real file description. On an
# Apache/BSD/MIT-licensed codebase, nearly every file's
# leading comment opens with exactly this shape ("Copyright 20XX The
# Foo Authors. All Rights Reserved.", "Licensed under the Apache
# License, Version 2.0...", "SPDX-License-Identifier: ..."), which
# ``workset``/``summary``/``orient`` all surfaced verbatim as the
# file's one-line description since they share this same module-doc
# extraction path.
#
# The original set only covered the first three
# lines of a standard Apache-2.0 header, so the fourth line onward
# ("You may obtain a copy of the License at", the "AS IS" disclaimer,
# ...) leaked through as the file's "purpose" on every file carrying
# one -- confirmed on spring-boot (100% of 8,659 files) and tensorflow
# (10,733 occurrences). Extended here with the rest of the standard
# Apache-2.0 header, plus the standard MIT and BSD-2/3-Clause header
# lines preemptively (same failure shape, just not yet reproduced
# live), since this is the second round a gap in this exact regex has
# been found.
_BOILERPLATE_HEADER_RE = re.compile(
    r"^(copyright\b|\(c\)\s*\d{4}\b|all rights reserved\b|"
    r"licensed under\b|spdx-license-identifier\b|"
    r"permission is hereby granted\b|redistribution and use\b|"
    r"this (?:source|file) (?:code )?is (?:part of|subject to)\b|"
    r"you may not use this file except in compliance\b|"
    r"you may obtain a copy of the license\b|"
    r"unless required by applicable law\b|"
    r"distributed under the license is distributed on an\b|"
    r"without warrant(?:ies or conditions of any kind|y of any kind)\b|"
    r"see the license for the specific language governing permissions\b|"
    r"limitations under the license\b|"
    r"https?://www\.apache\.org/licenses\b|"
    r"the above copyright notice and this permission notice shall be "
    r"included\b|"
    r'the software is provided "as is"|'
    r"list of conditions and the following disclaimer\b|"
    r"neither the name of\b)",
    re.IGNORECASE,
)


def _looks_like_boilerplate_header(line: str) -> bool:
    """Whether ``line`` reads as copyright/license boilerplate rather
    than a real description -- see ``_BOILERPLATE_HEADER_RE``."""
    return _BOILERPLATE_HEADER_RE.match(line.strip()) is not None


# A bare visual-rule line (a row of repeated punctuation used to visually
# close a header block, independent of license family -- TensorFlow's
# header convention appends one of these right before the closing `*/`
# on effectively every .h/.cc file in the repo) is never real "purpose"
# content either. The license-header fix (``_BOILERPLATE_HEADER_RE``
# above) closed the license-*text* gap but not this sibling shape: none
# of that regex's alternatives match a bare row of `=` signs, so the
# skip loop stopped here and surfaced the divider itself as the file's
# "purpose" -- same symptom, new root cause. Kept as a separate check
# (not folded into ``_BOILERPLATE_HEADER_RE``) since it's a structural
# line-shape test, not a text match, and applies uniformly regardless of
# which license header (or no header at all) precedes it.
_DIVIDER_LINE_RE = re.compile(r"^[=\-*#~]{10,}$")


def _looks_like_divider_line(line: str) -> bool:
    """Whether ``line`` is a bare row of one repeated divider character
    rather than real content -- see ``_DIVIDER_LINE_RE``."""
    return _DIVIDER_LINE_RE.match(line.strip()) is not None


def _string_first_line(
    raw: str, *, skip_boilerplate: bool = False
) -> str | None:
    """First non-empty content line of a string literal.

    ``skip_boilerplate`` (module-doc extraction only) additionally
    skips any leading line matching ``_looks_like_boilerplate_header``,
    continuing to the next non-empty line rather than surfacing legal
    text as the description.
    """
    text = _STR_PREFIX.sub("", raw.strip(), count=1)
    for quote in ('"""', "'''", '"', "'"):
        if text.startswith(quote):
            text = text[len(quote) :]
            text = text.removesuffix(quote)
            break
    for line in text.splitlines():
        if not line.strip():
            continue
        if skip_boilerplate and (
            _looks_like_boilerplate_header(line)
            or _looks_like_divider_line(line)
        ):
            continue
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


def _comment_first_line(
    raw: str, *, skip_boilerplate: bool = False
) -> str | None:
    """First non-empty content line of a comment block.

    ``skip_boilerplate`` (module-doc extraction only) additionally
    skips any leading line matching ``_looks_like_boilerplate_header``,
    continuing to the next non-empty line rather than surfacing legal
    text as the description.
    """
    for line in raw.splitlines():
        content = _strip_comment_markers(line.strip())
        if not content:
            continue
        if skip_boilerplate and (
            _looks_like_boilerplate_header(content)
            or _looks_like_divider_line(content)
        ):
            continue
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
    """Best-effort first doc line for a whole file, or ``None``.

    Skips copyright/license boilerplate lines (see
    ``_looks_like_boilerplate_header``) rather than surfacing them as
    the file's description -- scans forward through the file's whole
    leading run of comment nodes (not just the first) for the first
    non-boilerplate line; a file whose entire leading comment run is
    boilerplate falls back to ``None`` rather than picking legal text.
    """
    if language == "python":
        string = _leading_string(list(root.named_children))
        if string is None:
            return None
        return _string_first_line(_raw(string), skip_boilerplate=True)
    for child in root.named_children:
        if child.type not in _COMMENT_TYPES:
            return None
        raw = _raw(child)
        if raw.startswith("#!"):
            continue
        line = _comment_first_line(raw, skip_boilerplate=True)
        if line is not None:
            return line
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
                    has_default=kind == "typed_default_parameter",
                )
            )
        elif kind == "default_parameter":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                out.append(Param(name=_text(name_node), has_default=True))
        elif kind == "list_splat_pattern":
            out.append(
                Param(name="*" + _text(child).lstrip("* "), variadic=True)
            )
        elif kind == "dictionary_splat_pattern":
            out.append(
                Param(name="**" + _text(child).lstrip("* "), variadic=True)
            )
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
            out.append(Param(name="...", variadic=True))
    return out


def _params_generic(params_node: Node) -> list[Param]:
    """Best-effort parse: try name/type fields, else raw text.

    ``spread_parameter`` (Java's ``String... args`` varargs shape —
    Java is the one Tier-1 language dispatched through this otherwise
    Tier-2-only parser, via ``param_style="generic"``) has no
    ``name``/``pattern`` field to key off, unlike every other shape
    handled below, so it's matched by node type directly and marked
    ``variadic=True``: without this, a Java varargs candidate's arity
    range would be wrongly capped by ``resolver._param_arity`` instead
    of left unbounded. Harmless for every genuinely Tier-2 grammar
    that also reaches this parser — ``variadic`` is only ever
    consulted when ``RawCall.arg_count`` is populated, which Tier-2
    extraction never does (see ``extractor_generic.py``).
    """
    out: list[Param] = []
    for child in params_node.named_children:
        if child.type == "comment":
            continue
        if child.type == "spread_parameter":
            out.append(Param(name=_text(child), variadic=True))
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
    """Parse a C/C++ ``parameter_list`` node.

    C++'s ``optional_parameter_declaration`` (a parameter with a
    ``= default`` value) is the language's only defaulted-parameter
    shape; C has none. Confirmed against the pinned tree-sitter-cpp
    grammar: it carries its own ``default_value`` field alongside the
    ordinary ``type``/``declarator`` fields ``parameter_declaration``
    already has, so no new query/field access is needed beyond the
    node-type check already made here.
    """
    out: list[Param] = []
    for child in params_node.named_children:
        if child.type not in (
            "parameter_declaration",
            "optional_parameter_declaration",
        ):
            if child.type == "variadic_parameter":
                out.append(Param(name="...", variadic=True))
            continue
        type_node = child.child_by_field_name("type")
        declarator = child.child_by_field_name("declarator")
        base_type = _text(type_node) if type_node else None
        has_default = child.type == "optional_parameter_declaration"
        if declarator is None:
            out.append(
                Param(name="_", type=base_type, has_default=has_default)
            )
            continue
        decl_text = _text(declarator)
        stars = "*" * decl_text.count("*") + "&" * decl_text.count("&")
        name = _innermost_identifier(declarator) or decl_text
        full_type = f"{base_type} {stars}".strip() if base_type else None
        out.append(Param(name=name, type=full_type, has_default=has_default))
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
            out.append(
                Param(
                    name=_text(left) if left else _text(child),
                    has_default=True,
                )
            )
        elif child.type == "rest_pattern":
            out.append(
                Param(name="..." + _text(child).lstrip(". "), variadic=True)
            )
        else:
            out.append(Param(name=_text(child)))
    return out


def _params_ts(params_node: Node) -> list[Param]:
    """Parse a TypeScript ``formal_parameters`` node.

    Two arity-relevant shapes both surface as ``required_parameter``
    (not a distinct node type) in the pinned tree-sitter-typescript
    grammar, so both need their own field check rather than a
    node-type check alone: a defaulted-but-unmarked parameter
    (``b: number = 5``) carries a ``value`` field alongside its
    ``pattern``/``type`` fields, and a rest parameter (``...rest:
    T[]``) carries a ``rest_pattern`` node in its ``pattern`` field.
    Confirmed live against the pinned grammar.
    """
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
        has_default = (
            child.type == "optional_parameter"
            or child.child_by_field_name("value") is not None
        )
        variadic = pattern is not None and pattern.type == "rest_pattern"
        out.append(
            Param(
                name=name,
                type=param_type,
                has_default=has_default,
                variadic=variadic,
            )
        )
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
        variadic = child.type == "variadic_parameter_declaration"
        if variadic:
            param_type = f"...{param_type}" if param_type else "..."
        names = child.children_by_field_name("name")
        if not names:
            out.append(Param(name="_", type=param_type, variadic=variadic))
            continue
        out.extend(
            Param(name=_text(name_node), type=param_type, variadic=variadic)
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
        text, receiver = _cap_callee(text, name, receiver)
        caller = _enclosing(spans, callee.start_byte)
        args_node = _one(caps, "args")
        arg_count = (
            len(args_node.named_children) if args_node is not None else None
        )
        calls.append(
            RawCall(
                caller_id=caller.id if caller else None,
                path=rel,
                text=text,
                name=name,
                receiver=receiver,
                line=callee.start_point[0] + 1,
                arg_count=arg_count,
            )
        )
    return calls


# Rust macros whose arguments are ordinary expressions in practice
# (even though tree-sitter-rust never parses them as such — see
# ``_collect_rust_macro_calls``). Deliberately narrow: the highest-
# value, lowest-risk subset (the assert family) rather than an
# attempt at general macro-argument parsing.
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
    previously invisible to the call graph entirely (seen in zed).

    Scans ``_RUST_ASSERT_MACROS`` invocations' token trees for
    ``name(...)``, ``recv.name(...)`` and ``a::b::name(...)``-shaped
    subsequences and treats each as a call site, rendered the way the
    source wrote it (see ``_rust_token_qualifier`` for why the joiner
    matters). General user-defined macro bodies are out of scope.

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
            for found in _rust_macro_call_sites(token_tree):
                caller = _enclosing(spans, found.node.start_byte)
                calls.append(
                    RawCall(
                        caller_id=caller.id if caller else None,
                        path=rel,
                        text=found.text,
                        name=found.name,
                        receiver=found.receiver,
                        line=found.node.start_point[0] + 1,
                        arg_count=found.arg_count,
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


class _MacroCallSite(NamedTuple):
    """One call recovered from a Rust macro's raw token stream.

    Attributes:
        node: The callee's own ``identifier`` node.
        name: Its text (``new``).
        receiver: What the call is qualified by, or ``None`` for a bare
            call: the whole ``::`` path (``gpui::Point``), or for a
            dot-call the one token left of the dot (``buf``,
            ``iter()``).
        joiner: ``"::"``, ``"."``, or ``""`` for a bare call.
        arg_count: Written argument count, or ``None`` when the token
            stream can't be counted reliably.
    """

    node: Node
    name: str
    receiver: str | None
    joiner: str
    arg_count: int | None

    @property
    def text(self) -> str:
        """Callee text as the source wrote it, joiner included."""
        if not self.receiver:
            return self.name

        return f"{self.receiver}{self.joiner}{self.name}"


def _rust_macro_call_sites(token_tree: Node) -> list[_MacroCallSite]:
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
        Each call-shaped site found, in document order.
    """
    found: list[_MacroCallSite] = []
    _scan_rust_token_tree(token_tree, found)
    return found


def _scan_rust_token_tree(node: Node, found: list[_MacroCallSite]) -> None:
    """Depth-first scan for ``identifier(...)``-shaped subsequences."""
    children = node.children
    for i, child in enumerate(children):
        if child.type == "token_tree":
            _scan_rust_token_tree(child, found)
            continue
        if child.type not in _RUST_CALLEE_TOKEN_TYPES or i + 1 >= len(
            children
        ):
            continue
        args = children[i + 1]
        if args.type != "token_tree" or not _text(args).startswith("("):
            continue
        qualifier = _rust_token_qualifier(children, i)
        if qualifier is None:
            continue
        receiver, joiner = qualifier
        name = _text(child)
        found.append(
            _MacroCallSite(
                node=child,
                name=name,
                receiver=receiver,
                joiner=joiner,
                arg_count=_rust_token_arg_count(args),
            )
        )


# Token node types that can name a callee. ``default`` and ``union``
# are contextual keywords with node types of their own, so
# ``assert_eq!(x, Foo::default())``, as common as Rust test idioms
# get, used to go unrecovered entirely.
_RUST_CALLEE_TOKEN_TYPES = frozenset({"identifier", "default", "union"})


# Token node types that can be a segment of a ``::`` path. ``Self`` is
# a plain ``identifier`` to tree-sitter-rust; these three are not.
_RUST_PATH_SEGMENT_TYPES = frozenset({"identifier", "crate", "super", "self"})


def _rust_token_qualifier(
    children: list[Node], i: int
) -> tuple[str | None, str] | None:
    """What qualifies the callee at ``children[i]``, read leftwards.

    The joiner used to be matched and then thrown
    away, every recovered call being rendered ``receiver.name``. So
    ``assert_eq!(p, Point::new(1, 1))`` reached the resolver as
    ``Point.new``, and every rule that reads a Rust call's *shape* off
    its text misread it: the ``Type::name`` owner rule and the
    unknown-type veto stood down (no ``::``), the dot-call veto fired
    (a ``.``), and with nothing left to object the ladder took the
    file's only ``new``. 4,287 such sites on zed, concentrated in
    tests, which is what ``dekko affected`` reads.

    Returns:
        ``(None, "")`` for a bare call; ``(receiver, joiner)`` for a
        qualified one; ``None`` when a joiner is present but what it
        joins can't be recovered. The caller must then emit nothing: a
        call known to be qualified, reported as bare, is known-wrong
        (a bare ``new`` is exactly what takes the lone same-file
        ``new``), and a missing edge beats a wrong one.
    """
    if i < 1 or children[i - 1].type not in (".", "::"):
        return None, ""
    joiner = children[i - 1].type
    if joiner == ".":
        return _rust_token_dot_receiver(children, i - 2), "."

    segments: list[str] = []
    j = i - 2
    while True:
        j = _rust_skip_generic_args(children, j)
        if j < 0 or children[j].type not in _RUST_PATH_SEGMENT_TYPES:
            # ``<Foo as Bar>::make(..)``, or a path head this scan
            # doesn't understand.
            return None
        segments.append(_text(children[j]))
        if j < 1 or children[j - 1].type != "::":
            break
        j -= 2

    return "::".join(reversed(segments)), "::"


def _rust_token_dot_receiver(children: list[Node], j: int) -> str:
    """Receiver text for a recovered dot-call, rebuilt leftwards.

    Walks the method chain back to its head, so ``x.iter().count()``
    gives ``x.iter()`` for ``count``, the same receiver a parsed call
    gets (argument text is dropped: ``x.get(k).len()`` reads
    ``x.get()``; only the chain's first segment is ever looked at
    downstream, for ``self``, an import binding, or a typed
    parameter). What matters most is that there IS a receiver, so the
    call stays a method call. It used to be lost whenever the token
    left of the dot wasn't an ``identifier`` (here, a ``token_tree``),
    and the call came out as a bare ``count()``, which the ladder
    resolves by preferring a free function: the one thing a method
    call can't be. ``"_"`` stands in for a head this scan can't read
    (a literal, a parenthesised expression).
    """
    pieces: list[str] = []
    while j >= 0:
        node = children[j]
        if node.type in _RUST_PATH_SEGMENT_TYPES:
            pieces.append(_text(node))
            j -= 1
        elif (
            node.type == "token_tree"
            and j >= 1
            and children[j - 1].type == "identifier"
        ):
            pieces.append(f"{_text(children[j - 1])}()")
            j -= 2
        else:
            break
        if j < 0 or children[j].type != ".":
            break
        j -= 1

    return ".".join(reversed(pieces)) or "_"


def _rust_skip_generic_args(children: list[Node], j: int) -> int:
    """Step left over one turbofish group, if ``children[j]`` ends one.

    ``Vec :: < u8 > :: new``: from the ``>`` back to the ``Vec``.
    Generic arguments aren't nested token trees, just a flat run of
    tokens, so this balances angle brackets by hand (``>>`` closes
    two). Returns ``j`` unchanged when there is no group, and ``-1``
    when one is opened but never balances, or isn't introduced by
    ``::`` (so it was a comparison, not a turbofish).
    """
    if j < 0 or not _text(children[j]).startswith(">"):
        return j
    depth = 0
    while j >= 0:
        token = _text(children[j])
        if token and set(token) == {">"}:
            depth += len(token)
        elif token and set(token) == {"<"}:
            depth -= len(token)
        if depth <= 0:
            break
        j -= 1
    if j < 2 or depth != 0 or children[j - 1].type != "::":
        return -1

    return j - 2


# Direct-child tokens that make comma-counting unsafe: closure
# parameters (``|a, b| ..``) and generic arguments (``Map::<K, V>``)
# both put their commas at the argument list's own depth.
_RUST_UNCOUNTABLE_ARG_TOKENS = frozenset({"|", "||", "<", ">", "<<", ">>"})


def _rust_token_arg_count(args: Node) -> int | None:
    """Written argument count of a recovered call, when countable.

    Parenthesised and bracketed sub-expressions are already nested
    ``token_tree`` nodes, so a top-level comma really is a separator,
    with the two exceptions in ``_RUST_UNCOUNTABLE_ARG_TOKENS``. Those
    return ``None`` (this path's original behavior) rather than
    a guess: arity is used to *reject* a sole candidate
    (``resolver._sole_candidate_match``), so a wrong count costs a
    correct edge, where no count costs nothing.
    """
    inner = [c for c in args.children if c.type not in ("(", ")")]
    if any(c.type in _RUST_UNCOUNTABLE_ARG_TOKENS for c in inner):
        return None
    if not inner:
        return 0
    commas = sum(1 for c in inner if c.type == ",")
    trailing = 1 if inner[-1].type == "," else 0

    return commas + 1 - trailing


def _collect_cpp_ctor_arg_calls(
    root: Node, rel: str, defs: list[tuple[Node, Symbol]]
) -> list[RawCall]:
    """Recover constructor calls dropped by C/C++'s "most vexing parse".

    ``Type name(Ctor(), deleter);`` at statement position is
    grammatically indistinguishable from a local function declaration
    (``Ctor``'s trailing ``()`` reads as "a parameter named ``Ctor`` of
    function-returning-T type with no arguments," legal if archaic C++
    parameter syntax). Real compilers resolve this with full semantic
    analysis; tree-sitter-c/tree-sitter-cpp have no type information
    and structurally cannot resolve it — they commit to the
    declaration parse every time. ``Ctor()`` becomes a
    ``parameter_declaration`` wrapping an ``abstract_function_declarator``
    rather than a ``call_expression``, so ``_collect_calls``'s
    ``call_query`` has no node to match: the call is silently dropped,
    not misattributed. This is a common idiom in RAII-heavy C++
    (``std::unique_ptr<T, D> p(Ctor(), deleter);``), not a rare edge
    case (tensorflow alone had 100+ dropped sites).

    Scans block-scoped ``declaration`` nodes shaped like the ambiguity
    and synthesizes the call each ``abstract_function_declarator``
    parameter represents. Deliberately narrow, matching
    ``_collect_rust_macro_calls``'s precedent for a structurally
    unreachable call shape:

    - Only ``declaration`` nodes whose direct parent is a
      ``compound_statement`` (block/local scope) are considered. A
      genuine forward-declared local function prototype is legal C++
      but vanishingly rare next to this construction idiom, and real
      top-level/header prototypes (whose parameters are always types,
      never call-shaped) are excluded outright by this check.
    - Only single-level recovery: a call-shaped argument nested inside
      another call-shaped argument (``Foo bar(Baz(Qux()), other);``)
      recovers ``Baz`` but not ``Qux``. Documented, narrower residual
      gap rather than a silent one.

    Args:
        root: Parsed file's root node.
        rel: Repo-relative POSIX path of the file.
        defs: Definitions found in this file, used to attribute each
            recovered call to its enclosing function.

    Returns:
        Recovered ``RawCall``s, one per call-shaped constructor
        argument found.
    """
    spans = [(node.start_byte, node.end_byte, sym) for node, sym in defs]
    calls: list[RawCall] = []
    _find_cpp_ctor_arg_declarations(root, rel, spans, calls)
    return calls


def _find_cpp_ctor_arg_declarations(
    node: Node,
    rel: str,
    spans: list[tuple[int, int, Symbol]],
    calls: list[RawCall],
) -> None:
    """Recurse the tree for block-scoped vexing-parse declarations."""
    if node.type == "declaration" and _is_block_scoped(node):
        declarator = node.child_by_field_name("declarator")
        if declarator is not None and declarator.type == "function_declarator":
            _collect_cpp_ctor_arg_params(declarator, rel, spans, calls)

    for child in node.children:
        _find_cpp_ctor_arg_declarations(child, rel, spans, calls)


def _is_block_scoped(node: Node) -> bool:
    """Whether a node's direct parent is a ``compound_statement``."""
    return node.parent is not None and node.parent.type == "compound_statement"


def _collect_cpp_ctor_arg_params(
    declarator: Node,
    rel: str,
    spans: list[tuple[int, int, Symbol]],
    calls: list[RawCall],
) -> None:
    """Synthesize a ``RawCall`` for each call-shaped constructor arg."""
    params = declarator.child_by_field_name("parameters")
    if params is None:
        return
    for param in params.named_children:
        callee = _cpp_ctor_arg_callee(param)
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


def _cpp_ctor_arg_callee(param: Node) -> Node | None:
    """Callee node for a call-shaped ``parameter_declaration``, if any.

    A vexing-parse constructor argument surfaces as a
    ``parameter_declaration`` whose ``declarator`` field is an
    ``abstract_function_declarator`` (the "type name with an empty/
    call-shaped parameter list" shape) — its ``type`` field is the
    misparsed callee name. A bare-value argument (no nested call, e.g.
    the plain ``deleter`` in ``p(Ctor(), deleter)``) has no declarator
    at all and is left alone; it isn't call-shaped and recovering it is
    out of scope for this pass.
    """
    if param.type != "parameter_declaration":
        return None
    param_declarator = param.child_by_field_name("declarator")
    if (
        param_declarator is None
        or param_declarator.type != "abstract_function_declarator"
    ):
        return None
    return param.child_by_field_name("type")


def _collect_refs(
    spec: LanguageSpec,
    root: Node,
    rel: str,
    defs: list[tuple[Node, Symbol]],
    import_bytes: frozenset[int] = frozenset(),
) -> list[RawRef]:
    """Find bare-identifier value references, attributed to enclosing defs.

    Captures identifiers used as *values* rather than invoked —
    object-literal property values, array elements, call arguments,
    and assignment/declarator right-hand sides (see ``languages.py``'s
    per-language ``reference_query``) — the pass-by-reference usage a
    plain call-expression query structurally cannot see.
    Returns an empty list for languages with no ``reference_query``
    yet.

    Each reference is tagged with what it is lexically bound to
    (``RawRef.bound``, see ``_local_bindings``); ``import_bytes`` is
    ``_import_binding_bytes``'s set.
    """
    if spec.reference_query is None:
        return []
    spans = [(node.start_byte, node.end_byte, sym) for node, sym in defs]
    bindings = _local_bindings(spec, root, defs, import_bytes)
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
                bound=_bound_kind(spec, ref_node, bindings.get(name)),
            )
        )
    return refs


# ---------------------------------------------------------------------
# Local bindings
#
# ``reference_query`` captures every bare identifier in value position,
# and nothing in it knows ``count`` on line 1624 is the ``const count``
# from line 1623. ``resolver._ref_target_visible`` vetoes
# the cross-file cases it can prove from the import table. It cannot
# see a local that shadows a *same-file* symbol, one that shadows an
# *imported* name, or any local at all in a file with zero imports
# (which it has to exempt: an empty table proves nothing). Measured on
# 0.43.69, that residue was 6.2% of claude-code's reference sites and
# 4.1% of cline's, two thirds of it in zero-import files.
#
# Everything here fails open. A binding shape this table doesn't know
# yields ``bound=None``, which is exactly the behavior without this
# table: it can leave a false edge standing, never remove a true one.

# One scope's claim on a name: (start byte, end byte, kind, scope node
# type). ``kind`` is "param" / "local", or ``_BOUND_EXEMPT`` for a
# binding the map already indexes (a nested def, an import), recorded
# only so the innermost-scope search stops on it.
_Binding = tuple[int, int, str, str]
_BOUND_EXEMPT = ""

# A class body binds names for its own statements only: code inside a
# method does not see them (Python's one scoping oddity). Such a scope
# is skipped when a function sits between it and the reference.
_CLASS_SCOPES = frozenset({"class_definition"})

# Pattern nodes whose named children are all (sub)patterns.
_PATTERN_CONTAINERS = frozenset(
    {
        "formal_parameters",
        "parameters",
        "lambda_parameters",
        "object_pattern",
        "array_pattern",
        "rest_pattern",
        "pattern_list",
        "tuple_pattern",
        "list_pattern",
        "list_splat_pattern",
        "dictionary_splat_pattern",
        "as_pattern_target",
        "tuple",
        "list",
    }
)
# Pattern nodes that keep the bound name under one field; every other
# child (a default value, a type annotation) is not a binding.
_PATTERN_FIELDS = {
    "pair_pattern": "value",
    "assignment_pattern": "left",
    "object_assignment_pattern": "left",
    "required_parameter": "pattern",
    "optional_parameter": "pattern",
    "default_parameter": "name",
    "typed_default_parameter": "name",
}
_PATTERN_LEAVES = frozenset(
    {"identifier", "shorthand_property_identifier_pattern"}
)


def _pattern_identifiers(node: Node) -> list[Node]:
    """Every identifier a binding pattern introduces, at any depth.

    Anything that isn't a pattern (``self.x``, ``table[k]``, a TS
    ``this`` parameter) contributes nothing.
    """
    if node.type in _PATTERN_LEAVES:
        return [node]
    field_name = _PATTERN_FIELDS.get(node.type)
    if field_name is not None:
        inner = node.child_by_field_name(field_name)
        return _pattern_identifiers(inner) if inner is not None else []
    if node.type == "typed_parameter":
        # ``(typed_parameter (identifier) type: (type ..))``: the name
        # is the first child, and the annotation is full of
        # identifiers that bind nothing.
        first = node.named_children[0] if node.named_children else None
        return _pattern_identifiers(first) if first is not None else []
    if node.type in _PATTERN_CONTAINERS:
        return [
            ident
            for child in node.named_children
            for ident in _pattern_identifiers(child)
        ]
    return []


def _python_import_locals(stmt: Node) -> list[Node]:
    """Local name nodes a Python import statement binds."""
    out: list[Node] = []
    plain = stmt.type == "import_statement"
    for child in stmt.children_by_field_name("name"):
        node: Node | None = child
        if child.type == "aliased_import":
            node = child.child_by_field_name("alias")
        elif child.type == "dotted_name" and child.named_children:
            # ``import a.b`` binds ``a``; ``from m import a`` binds
            # ``a`` (a one-part dotted_name either way).
            node = child.named_children[0 if plain else -1]
        if node is not None and node.type == "identifier":
            out.append(node)
    return out


def _scope_above(node: Node | None, types: tuple[str, ...]) -> Node | None:
    """Nearest ancestor-or-self scope of ``types``; ``None`` means the
    module scope.

    The module scope is "ran out of parents", never a node type: on a
    file with a syntax error tree-sitter's root is ``ERROR``, not
    ``program``/``module``.
    """
    while node is not None and node.parent is not None:
        if node.type in types:
            return node
        node = node.parent
    return None


def _binding_sites(
    spec: LanguageSpec, capture: str, node: Node, scope_node: Node | None
) -> tuple[Node | None, str, list[Node]]:
    """(scope, kind, identifiers) for one ``binding_query`` capture."""
    every = spec.binding_function_scopes + spec.binding_block_scopes
    if capture in ("params", "param"):
        return node.parent, "param", _pattern_identifiers(node)
    if capture == "scoped":
        return scope_node, "local", _pattern_identifiers(node)
    if capture == "funclocal":
        scope = _scope_above(node.parent, spec.binding_function_scopes)
        return scope, "local", _pattern_identifiers(node)
    if capture == "local":
        return (
            _scope_above(node.parent, every),
            "local",
            _pattern_identifiers(node),
        )
    if capture == "defname":
        # ``node.parent`` is the definition itself, which is a scope of
        # its own; the name binds one level out.
        owner = node.parent.parent if node.parent is not None else None
        return _scope_above(owner, every), _BOUND_EXEMPT, [node]
    if capture == "global":
        scope = _scope_above(node.parent, spec.binding_function_scopes)
        return scope, _BOUND_EXEMPT, [node]
    if capture == "import":
        scope = _scope_above(node.parent, spec.binding_function_scopes)
        return scope, _BOUND_EXEMPT, _python_import_locals(node)
    return None, _BOUND_EXEMPT, []


_BINDING_CAPTURES = (
    "params",
    "param",
    "local",
    "funclocal",
    "scoped",
    "defname",
    "global",
    "import",
)


def _local_bindings(
    spec: LanguageSpec,
    root: Node,
    defs: list[tuple[Node, Symbol]],
    import_bytes: frozenset[int],
) -> dict[str, list[_Binding]]:
    """Every non-module binding in the file, keyed by name.

    Module-level bindings are left out entirely: they are symbols or
    imports, and the resolver's ladder and visibility veto already own
    those. A binding whose declarator is one of ``defs`` (a nested
    ``const helper = () => ..``) or whose name node is in
    ``import_bytes`` (a function-local lazy import) is recorded as
    exempt, so it stops the search without tagging the reference.
    """
    if spec.binding_query is None:
        return {}
    matches = _run_query(spec.grammar, spec.binding_query, root)
    nonlocal_names = _nonlocal_names(spec, matches)
    def_bytes = frozenset(node.start_byte for node, _ in defs)
    out: dict[str, list[_Binding]] = {}
    for _, caps in matches:
        scope_node = _one(caps, "scope")
        for capture in _BINDING_CAPTURES:
            for node in caps.get(capture, []):
                scope, kind, idents = _binding_sites(
                    spec, capture, node, scope_node
                )
                if scope is None:
                    continue
                for ident in idents:
                    name = _text(ident)
                    if (scope.start_byte, name) in nonlocal_names:
                        continue
                    if _binds_indexed_name(ident, def_bytes, import_bytes):
                        kind = _BOUND_EXEMPT
                    out.setdefault(name, []).append(
                        (scope.start_byte, scope.end_byte, kind, scope.type)
                    )
    return out


def _nonlocal_names(
    spec: LanguageSpec, matches: _QueryMatches
) -> set[tuple[int, str]]:
    """(scope start byte, name) for every Python ``nonlocal`` name: an
    assignment to it in that scope binds nothing there."""
    out: set[tuple[int, str]] = set()
    for _, caps in matches:
        for node in caps.get("nonlocal", []):
            scope = _scope_above(node.parent, spec.binding_function_scopes)
            if scope is not None:
                out.add((scope.start_byte, _text(node)))
    return out


def _binds_indexed_name(
    ident: Node, def_bytes: frozenset[int], import_bytes: frozenset[int]
) -> bool:
    """Whether a local binding is one the map already indexes: a lazy
    import, or a declarator that is itself a definition (a nested
    ``const helper = () => ..``)."""
    if ident.start_byte in import_bytes:
        return True
    declarator = ident.parent
    return (
        declarator is not None
        and declarator.type == "variable_declarator"
        and declarator.start_byte in def_bytes
    )


def _bound_kind(
    spec: LanguageSpec, ref_node: Node, claims: list[_Binding] | None
) -> str | None:
    """``RawRef.bound`` for one reference: the innermost scope that
    binds its name decides.

    Position inside the scope doesn't matter: ``let``/``const``/``var``
    and Python locals all bind their whole scope, so a name declared
    further down is still the local.
    """
    if not claims:
        return None
    at = ref_node.start_byte
    inside = sorted(
        (c for c in claims if c[0] <= at < c[1]),
        key=lambda c: c[1] - c[0],
    )
    for start, _end, kind, scope_type in inside:
        if scope_type in _CLASS_SCOPES and _function_between(
            spec, ref_node, start
        ):
            continue
        return kind or None
    return None


def _function_between(
    spec: LanguageSpec, ref_node: Node, class_start: int
) -> bool:
    """Whether a function scope sits between the reference and the
    class scope starting at ``class_start``."""
    node = ref_node.parent
    while node is not None and node.start_byte >= class_start:
        if node.type in _CLASS_SCOPES and node.start_byte == class_start:
            return False
        if (
            node.type in spec.binding_function_scopes
            and node.type not in _CLASS_SCOPES
        ):
            return True
        node = node.parent
    return False


_NAME_FIELDS = ("attribute", "property", "field")
_RECEIVER_FIELDS = ("object", "value", "operand", "argument", "scope", "path")
_SCOPED_TYPES = ("scoped_identifier", "qualified_identifier")

# Canonical receiver text. A call's receiver used to be copied from
# the source verbatim, so a chain like ``expect(result.foo).toBe`` or
# ``[...30 lines...].join`` became the external callee id with its
# arguments and literal contents inside: claude-buddy's longest was
# 1,749 characters, claude-code's 122,129, and half of a TS or Rust
# repo's distinct external ids were argument-bearing chains. The
# resolver never reads past the first ``(`` of that text: every rule
# wants the head token (``chalk``, ``this``, ``std``, a parameter
# name), the second segment of a parameter-rooted chain, or the last
# joiner (see ``_PATH_SPLIT`` and ``_rust_dot_call`` in resolver.py).
# So the receiver is rendered structurally instead: identifiers and
# member chains verbatim, a call as ``name()``, a subscript ``name[]``,
# a literal as ``""``/``[]``/``{}``, anything else ``(…)``. Every head
# that is an identifier today stays byte-identical; every head that
# was already garbage (``expect(result``) becomes different garbage.
_CANON_LEAF_TYPES = frozenset(
    {
        "identifier",
        "property_identifier",
        "field_identifier",
        "type_identifier",
        "namespace_identifier",
        "package_identifier",
        "shorthand_property_identifier",
        "this",
        "self",
        "super",
        "this_expression",
        "super_expression",
        "crate",
        "metavariable",
        "meta_property",
        "generic_type",
        "bracketed_type",
        "primitive_type",
        "predefined_type",
        "class_literal",
    }
)
_CANON_CALL_TYPES = frozenset({"call_expression", "call"})
_CANON_NEW_TYPES = frozenset({"new_expression", "object_creation_expression"})
_CANON_SUBSCRIPT_TYPES = frozenset(
    {
        "subscript_expression",
        "subscript",
        "index_expression",
        "array_access",
        "element_access_expression",
    }
)
_CANON_STRING_TYPES = frozenset(
    {
        "string",
        "string_literal",
        "template_string",
        "raw_string_literal",
        "interpreted_string_literal",
        "concatenated_string",
        "char_literal",
    }
)
_CANON_ARRAY_TYPES = frozenset(
    {
        "array",
        "array_expression",
        "array_literal",
        "list",
        "list_comprehension",
        "tuple",
        "tuple_expression",
        "slice_literal",
    }
)
_CANON_OBJECT_TYPES = frozenset(
    {"object", "dictionary", "dictionary_comprehension", "set"}
)
_CANON_OPAQUE = "(…)"
# Node type → fixed marker, for every literal shape.
_CANON_MARKERS = {
    **dict.fromkeys(_CANON_STRING_TYPES, '""'),
    **dict.fromkeys(_CANON_ARRAY_TYPES, "[]"),
    **dict.fromkeys(_CANON_OBJECT_TYPES, "{}"),
    "regex": "/…/",
}
# Longest callee text kept whole. Canonical text is bounded by hop
# count, not argument size, but zed has builder chains a hundred hops
# long; over this, ``_cap_callee`` keeps the head, the last joiner
# and the name, which is everything the resolver reads.
_CALLEE_TEXT_LIMIT = 160
_HEAD_SPLIT = re.compile(r"::|\?\.|\.|->")


def _joiner_text(node: Node, recv: Node, name: Node) -> str:
    """The operator tokens between a receiver and its member name.

    Anonymous children between the two (``.``, ``?.``, ``::``,
    ``->``), comments dropped, so a chain split across lines with a
    comment in it renders the same as one written on one line.
    """
    return "".join(
        _text(child)
        for child in node.children
        if child.start_byte >= recv.end_byte
        and child.end_byte <= name.start_byte
        and not child.type.endswith("comment")
    )


def _canonical_expr(node: Node) -> str:
    """Render an expression node as bounded, argument-free text.

    See the comment above ``_CANON_LEAF_TYPES`` for why and for the
    invariant this keeps. Recursive over the receiver chain; each hop
    contributes its name or a fixed marker, never its arguments.
    """
    # Iterative, not recursive: a builder chain of a hundred hops
    # (zed's ``server.add_request_handler::<_>(..)`` a hundred times) is a
    # hundred nested nodes, and the head the resolver needs is the
    # innermost one. Walk down the chain's spine collecting each
    # hop's suffix, then render from the head outward.
    suffixes: list[str] = []
    current = node
    while True:
        step = _canonical_step(current)
        if isinstance(step, str):
            return step + "".join(reversed(suffixes))
        current, suffix = step
        suffixes.append(suffix)


def _canonical_step(node: Node) -> str | tuple[Node, str]:
    """Render one node, or hand back the node it wraps and its suffix."""
    kind = node.type
    if kind in _CANON_LEAF_TYPES:
        return _text(node)
    marker = _CANON_MARKERS.get(kind)
    if marker is not None:
        return marker
    special = _canonical_call_like(node)
    if special is None:
        special = _canonical_wrapped(node)
    if special is not None:
        return special
    return _canonical_access(node)


def _canonical_access(node: Node) -> str | tuple[Node, str]:
    """A member/scoped access, or a named type-shaped node, or opaque."""
    name_node = None
    for field_name in (*_NAME_FIELDS, "name"):
        name_node = node.child_by_field_name(field_name)
        if name_node is not None:
            break
    if name_node is None:
        return _canonical_fallback(node)
    recv = _receiver_child(node)
    if recv is not None:
        return recv, _joiner_text(node, recv, name_node) + _text(name_node)
    # A named node with no receiver (``Foo<T>``, ``template_type``):
    # type-shaped and short, kept as written.
    return _text(node)


_IDENT_HEAD = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _canonical_fallback(node: Node) -> str:
    """Opaque rendering that still keeps an identifier head.

    A node shape this module does not know (Java's ``Foo.class``, a
    Go type assertion ``x.(T)``, a Rust range ``a..b``) may still start
    with the identifier the resolver keys on. Dropping it to ``(…)``
    would turn an import-vetoed call into a bare-name lookup, which
    fabricates edges; so the head and its separator survive and only
    the rest is elided. A shape with no identifier head was garbage
    to the resolver already and stays garbage.
    """
    text = _text(node)
    match = _HEAD_SPLIT.search(text)
    head = text[: match.start()] if match else text
    if not _IDENT_HEAD.fullmatch(head):
        return _CANON_OPAQUE
    if match is None:
        return head
    return head + match.group(0) + _CANON_OPAQUE


def _receiver_child(node: Node) -> Node | None:
    """The node under the first ``_RECEIVER_FIELDS`` field present."""
    for field_name in _RECEIVER_FIELDS:
        child = node.child_by_field_name(field_name)
        if child is not None:
            return child
    return None


def _descend(node: Node | None, suffix: str) -> str | tuple[Node, str]:
    """Keep descending into ``node`` with ``suffix``, or end opaque."""
    if node is None:
        return _CANON_OPAQUE + suffix
    return node, suffix


def _canonical_call_like(node: Node) -> str | tuple[Node, str] | None:
    """Calls, constructors, macros and generic instantiations."""
    kind = node.type
    first = node.named_children[0] if node.named_children else None
    if kind in _CANON_CALL_TYPES:
        return _descend(node.child_by_field_name("function"), "()")
    if kind == "method_invocation":
        obj = node.child_by_field_name("object")
        name = node.child_by_field_name("name")
        tail = (_text(name) if name else "") + "()"
        if obj is None:
            return tail
        return obj, "." + tail
    if kind in _CANON_NEW_TYPES:
        ctor = node.child_by_field_name(
            "constructor"
        ) or node.child_by_field_name("type")
        if ctor is None:
            return "new " + _CANON_OPAQUE + "()"
        # ``new`` is a prefix; the constructor's own text is a leaf or
        # a short access chain, so rendering it here is bounded.
        return "new " + _canonical_expr(ctor) + "()"
    if kind == "macro_invocation":
        macro = node.child_by_field_name("macro")
        return (_text(macro) if macro else _CANON_OPAQUE) + "!()"
    if kind == "generic_function":
        return _descend(node.child_by_field_name("function") or first, "::<>")
    if kind == "instantiation_expression":
        return _descend(first, "<>")
    return None


def _canonical_wrapped(node: Node) -> str | tuple[Node, str] | None:
    """Subscripts, struct literals and postfix wrappers."""
    kind = node.type
    first = node.named_children[0] if node.named_children else None
    if kind in _CANON_SUBSCRIPT_TYPES:
        return _descend(_receiver_child(node) or first, "[]")
    if kind == "struct_expression":
        name = node.child_by_field_name("name")
        return (_text(name) if name else _CANON_OPAQUE) + " {}"
    if kind == "composite_literal":
        type_node = node.child_by_field_name("type")
        return (_text(type_node) if type_node else _CANON_OPAQUE) + "{}"
    if first is None:
        return None
    if kind == "try_expression":
        return first, "?"
    if kind == "non_null_expression":
        return first, "!"
    if kind == "await_expression":
        if _text(node).endswith(".await"):
            return first, ".await"
        return _CANON_OPAQUE
    return None


def _cap_callee(
    text: str, name: str, receiver: str | None
) -> tuple[str, str | None]:
    """Shorten an over-long callee to head, ellipsis, joiner and name.

    Only reached by a chain of many hops (``server
    .add_request_handler()`` a hundred times). Keeps exactly the parts
    the resolver reads: the head token, the final joiner and the name.
    """
    if len(text) <= _CALLEE_TEXT_LIMIT:
        return text, receiver
    canonical = (
        receiver is not None
        and text.startswith(receiver)
        and text.endswith(name)
        and len(text) - len(receiver) - len(name) <= 3
    )
    if not canonical:
        # Raw text from ``_callee_parts``'s fallback branch (a callee
        # with no name field: an immediately-invoked function, a
        # string-concatenation callee). Nothing in it is structured;
        # cut it and mark the cut. ``name`` is what resolution uses
        # and is untouched.
        cut = text[:_CALLEE_TEXT_LIMIT] + "…"
        if receiver is not None and len(receiver) > _CALLEE_TEXT_LIMIT:
            receiver = receiver[:_CALLEE_TEXT_LIMIT] + "…"
        return cut, receiver
    assert receiver is not None
    joiner = text[len(receiver) : len(text) - len(name)]
    match = _HEAD_SPLIT.search(receiver)
    # The head keeps its own separator so it stays a whole token to
    # the resolver's split (``ListItem::…``, never ``ListItem…``).
    if match is None:
        head = receiver[:_CALLEE_TEXT_LIMIT] + "…"
    else:
        head = receiver[: match.start()] + match.group(0) + "…"
    return head + joiner + name, head


def _callee_parts(node: Node) -> tuple[str, str, str | None]:
    """Split a callee node into (full text, base name, receiver).

    Handles attribute/member/field access (``a.b``, ``a->b``), scoped
    paths (``a::b``), Java invocations, and falls back to splitting
    the raw text.
    """
    special = _callee_java(node)
    if special is not None:
        return special
    # A Rust turbofish call (``xs.iter().collect::<Vec<_>>()``) is a
    # ``generic_function`` wrapping the real callee. It has no name
    # field, so it used to fall through to the raw-text splitter,
    # which cut at the first ``(`` and named the call after the
    # chain's first member (``iter``): 2,859 of zed's 7,372 turbofish
    # calls carried the wrong name, and resolved to it. The type
    # arguments are not part of the callee path; unwrap them.
    if node.type == "generic_function":
        inner = node.child_by_field_name("function")
        if inner is not None:
            return _callee_parts(inner)
    name_node = _callee_name_node(node)
    if name_node is not None:
        name = _canonical_member_name(name_node)
        for field_name in _RECEIVER_FIELDS:
            recv_node = node.child_by_field_name(field_name)
            if recv_node is not None:
                receiver = _canonical_expr(recv_node)
                joiner = _joiner_text(node, recv_node, name_node)
                return receiver + joiner + name, name, receiver
        return _text(node), name, None
    text = _text(node)
    if node.named_child_count == 0:
        return text, text, None
    return text, *_split_callee_text(text)


_TEMPLATE_NAME_TYPES = frozenset({"template_method", "template_function"})
_TEMPLATE_NAME_LIMIT = 40


def _canonical_member_name(name_node: Node) -> str:
    """A member name with C++ template arguments elided.

    ``patterns->add<ConvertAllOp, ..., ConvertXOp>(ctx)`` names its
    member with a ``template_method`` node whose text is the whole
    argument list (2,350 characters on tensorflow). A short one stays
    as written: a template specialization is a symbol named exactly
    ``Get<int>``, and ``v.Get<int>()`` resolves to it by that name.
    Past the gate no symbol could carry the name, so the list is
    elided to ``add<>`` and the call stays external, as it was.
    """
    text = _text(name_node)
    if (
        name_node.type in _TEMPLATE_NAME_TYPES
        and len(text) > _TEMPLATE_NAME_LIMIT
    ):
        inner = name_node.child_by_field_name("name")
        if inner is not None:
            return _text(inner) + "<>"
    return text


def _callee_name_node(node: Node) -> Node | None:
    """The member-name child of an access-shaped callee, if any."""
    for field_name in _NAME_FIELDS:
        name_node = node.child_by_field_name(field_name)
        if name_node is not None:
            return name_node
    if node.type in _SCOPED_TYPES:
        return node.child_by_field_name("name")
    return None


def _callee_java(node: Node) -> tuple[str, str, str | None] | None:
    """Handle Java's call shapes, which carry their own arguments."""
    if node.type == "method_invocation":
        name_node = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        name = _text(name_node) if name_node else ""
        receiver = _canonical_expr(obj) if obj else None
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
    type name — a known pitfall
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
    type they're for, though not required by the language.

    An ordinary Rust layout puts the
    impl block in a sibling file (``text_finder/render.rs: impl
    Render for TextFinder``) from the struct it's for
    (``text_finder.rs: struct TextFinder``) — a normal way to split a
    type's rendering logic out of its own module, not a rare shape.
    When same-file lookup finds no unique match, the clause is
    emitted anyway with ``subtype_id=""`` and ``subtype_name`` set to
    the written type name, so ``resolver.resolve_heritage`` can
    resolve the subject itself repo-wide (by name and crate) before
    resolving the supertype the normal way — see ``RawHeritage``'s
    own docstring. Only the *zero-match* case gets this recovery; a
    same-named-in-two-``mod``-blocks collision (2+ matches, legal but
    rare) still drops the clause outright, unchanged from before —
    same-file kind/name ambiguity isn't this rule's problem to solve,
    and guessing between two same-file candidates would be exactly
    the kind of guess this codebase's resolver avoids elsewhere.

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
    text, name, receiver = _heritage_name_parts(impl_trait)
    if not name:
        return []
    candidates = [
        sym
        for _, sym in defs
        if sym.kind in TYPE_KINDS and sym.name == type_name
    ]
    if len(candidates) > 1:
        return []
    subtype_id = candidates[0].id if len(candidates) == 1 else ""
    return [
        RawHeritage(
            subtype_id=subtype_id,
            path=rel,
            text=text,
            name=name,
            receiver=receiver,
            relation="impl",
            line=impl_trait.start_point[0] + 1,
            subtype_name="" if subtype_id else type_name,
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
# Throws/catches (raise/throw sites, except/catch clauses)

# Node types a raised/caught type node might reasonably be — anything
# outside this set (a string/object/array literal, e.g. JS ``throw "a
# string"``) yields ``name=None`` deliberately rather than a garbage
# name pulled from an unrelated node's text (see ``_throw_type_parts``,
# ``_catch_type_name``). Reused across Python/C++'s bare-identifier and
# attribute/member-access re-raise shapes and Java/C++'s (possibly
# scoped/qualified) type names.
_NAMEABLE_TYPE_NODES = (
    "identifier",
    "attribute",
    "field_expression",
    "qualified_identifier",
    "scoped_identifier",
    "member_expression",
    "type_identifier",
)


def _raise_expr(stmt_node: Node) -> Node | None:
    """First unfielded named child of a raise/throw statement.

    Shared by Python's ``raise_statement`` and C++/JS/TS's
    ``throw_statement`` — all three park the raised expression as a
    bare (unfielded) first named child, absent entirely for a bare
    re-raise (Python bare ``raise``, C++ bare ``throw;``). Python's
    optional ``raise X from Y`` re-raise-cause uses its own fielded
    ``cause:`` child, which must be skipped rather than mistaken for
    the raised expression itself — confirmed against the pinned
    tree-sitter-python grammar (``cause`` is the only fielded child a
    raise/throw statement in any of these languages ever carries).
    """
    for i, child in enumerate(stmt_node.children):
        if not child.is_named:
            continue
        if stmt_node.field_name_for_child(i) is not None:
            continue
        return child
    return None


def _throw_type_parts(expr: Node) -> tuple[str, str | None]:
    """Best-effort ``(text, name)`` for a raised/thrown expression.

    ``name`` is the raised type's base identifier for a type
    construction (``SomeError(...)``, ``new SomeError(...)``, Java's
    ``new SomeError(...)``) or a bare type/identifier reference
    (``raise SomeError`` / ``raise err`` re-raising a caught
    variable) — ``None`` for anything else (a string/object-literal
    throw, valid in JS/TS but not a name-able type; a known JS/TS
    caveat).
    """
    text = _text(expr)
    if expr.type in ("call", "call_expression"):
        func = expr.child_by_field_name("function")
        if func is None:
            return text, None
        _, name, _ = _heritage_name_parts(func)
        return text, name or None
    if expr.type == "object_creation_expression":
        special = _callee_java(expr)
        return text, (special[1] or None) if special else None
    if expr.type == "new_expression":
        ctor = expr.child_by_field_name("constructor")
        if ctor is None:
            return text, None
        _, name, _ = _heritage_name_parts(ctor)
        return text, name or None
    if expr.type in _NAMEABLE_TYPE_NODES:
        _, name, _ = _heritage_name_parts(expr)
        return text, name or None
    return text, None


def _innermost_identifier(node: Node) -> str | None:
    """Unwrap a declarator (C++ ``reference_declarator``/
    ``pointer_declarator``) down to its base ``identifier``'s text.
    """
    while node is not None and node.type != "identifier":
        named = [c for c in node.children if c.is_named]
        node = named[0] if named else None
    return _text(node) if node is not None else None


def _java_instanceof_pattern_name(if_node: Node) -> str | None:
    """Bound pattern variable of a Java ``if (x instanceof T t)`` guard.

    ``None`` if the ``if``'s condition isn't (only) a pattern-matching
    ``instanceof`` test. Handles the direct case only — the condition's
    ``parenthesized_expression`` wraps exactly one
    ``instanceof_expression`` carrying a ``name`` field (Java 16+
    pattern matching for ``instanceof``) — not compound/negated
    conditions (``!(x instanceof T t)`` guards paired with an early
    return, De Morgan-style reordering, etc.), which would need real
    flow analysis to scope correctly and are out of scope for this
    fix. Verified against the pinned tree-sitter-java grammar.
    """
    cond = if_node.child_by_field_name("condition")
    if cond is not None and cond.type == "parenthesized_expression":
        inner = cond.named_children
        cond = inner[0] if inner else None
    if cond is None or cond.type != "instanceof_expression":
        return None
    name = cond.child_by_field_name("name")
    return _text(name) if name is not None else None


def _java_if_pattern_binding(node: Node, prev: Node) -> str | None:
    """``_java_instanceof_pattern_name(node)`` if ``prev`` (the child
    ``_nearest_catch_binding`` arrived from) is inside ``node``'s own
    ``consequence`` block, ``None`` otherwise (including when ``node``
    isn't a pattern-matching ``instanceof`` guard at all) — split out
    of ``_nearest_catch_binding`` purely to keep that function's
    branch count under the project's complexity cap.
    """
    consequence = node.child_by_field_name("consequence")
    if (
        consequence is None
        or consequence.start_byte > prev.start_byte
        or prev.end_byte > consequence.end_byte
    ):
        return None
    return _java_instanceof_pattern_name(node)


# Node type that terminates ``_nearest_catch_binding``'s upward walk
# for each language — reaching it (whether or not a bound name can be
# extracted from it) always stops the search, since it's the nearest
# enclosing except/catch construct.
_CATCH_TERMINAL_TYPE = {
    "python": "except_clause",
    "javascript": "catch_clause",
    "typescript": "catch_clause",
    "tsx": "catch_clause",
    "cpp": "catch_clause",
    "java": "catch_clause",
}


def _catch_binding_name(language: str, node: Node) -> str | None:
    """Bound exception-variable name from one matched terminal
    except/catch node (see ``_CATCH_TERMINAL_TYPE``), dispatched by
    language. Split out of ``_nearest_catch_binding`` purely to keep
    that function's branch count under the project's complexity cap.

    Verified against the pinned grammars:
    - Python: ``except_clause``'s ``value`` field is an ``as_pattern``
      whose ``alias`` field is the bound name (``except X as e:``).
    - JS/TS: ``catch_clause``'s ``parameter`` field is the bound name
      directly (``catch (e)``), when it's a plain identifier (not a
      destructuring pattern, which this deliberately doesn't try to
      match — a destructured catch can't plausibly rethrow "the whole
      exception" by any single name).
    - C++: ``catch_clause``'s ``parameters`` field is a
      ``parameter_list``; its sole ``parameter_declaration``'s
      ``declarator`` field wraps the bound name (possibly through a
      ``reference_declarator``, e.g. ``catch (std::exception& e)``),
      unwrapped via ``_innermost_identifier``.
    - Java: ``catch_clause``'s unfielded ``catch_formal_parameter``
      child has a ``name`` field directly (``catch (Exception ex)``)
      — see ``_java_catch_param_binding``.
    """
    if language == "python":
        value = node.child_by_field_name("value")
        if value is not None and value.type == "as_pattern":
            alias = value.child_by_field_name("alias")
            return _text(alias) if alias is not None else None
        return None
    if language in ("javascript", "typescript", "tsx"):
        param = node.child_by_field_name("parameter")
        if param is not None and param.type == "identifier":
            return _text(param)
        return None
    if language == "cpp":
        params = node.child_by_field_name("parameters")
        if params is not None and params.named_child_count == 1:
            decl = params.named_children[0]
            declarator = decl.child_by_field_name("declarator")
            if declarator is not None:
                return _innermost_identifier(declarator)
        return None
    if language == "java":
        return _java_catch_param_binding(node)
    return None


def _java_catch_param_binding(node: Node) -> str | None:
    """Bound exception-variable name of a Java ``catch_clause``.

    Split out of ``_catch_binding_name`` purely to keep that
    function's branch count under the project's complexity cap.
    """
    param = next(
        (c for c in node.named_children if c.type == "catch_formal_parameter"),
        None,
    )
    if param is None:
        return None
    name = param.child_by_field_name("name")
    return _text(name) if name is not None else None


def _nearest_catch_binding(
    language: str, stmt: Node, boundary_types: tuple[str, ...]
) -> str | None:
    """Bound exception-variable name of the nearest enclosing
    except/catch clause containing ``stmt`` (a raise/throw statement),
    or ``None`` if it isn't confidently inside one before crossing a
    function boundary.

    Java additionally recognizes one narrower binding *nested inside*
    a catch clause, checked before the terminal ``catch_clause`` node
    is reached: a pattern-matching ``instanceof`` guard's bound
    variable (``if (ex instanceof BindException bindException) {
    throw bindException; }``, Java 16+), scoped to that ``if``'s own
    consequence block only — see ``_java_if_pattern_binding``. Without
    this (seen in spring-boot), a rethrown pattern-bound
    variable was labeled ``(external)`` with the raw variable
    identifier standing in for a fabricated type name. See
    ``_catch_binding_name`` for the per-language terminal-node
    extraction this defers to once a match is found.
    """
    terminal_type = _CATCH_TERMINAL_TYPE.get(language)
    prev = stmt
    node = stmt.parent
    while node is not None:
        if node.type in boundary_types:
            return None
        if language == "java" and node.type == "if_statement":
            name = _java_if_pattern_binding(node, prev)
            if name is not None:
                return name
        if node.type == terminal_type:
            return _catch_binding_name(language, node)
        prev = node
        node = node.parent
    return None


def _catch_type_name(node: Node) -> str | None:
    """Base identifier of one caught-type node, or ``None``.

    Mirrors ``_heritage_name_parts``'s text-splitting — a caught type
    is written the same shapes a heritage clause's base type is
    (bare identifier, dotted/scoped path).
    """
    _, name, _ = _heritage_name_parts(node)
    return name or None


def _catches_python(except_node: Node) -> tuple[list[str], bool]:
    """Walk a Python ``except_clause``'s optional ``value`` field.

    ``value`` is absent for a bare ``except:`` (catch-all). When
    present it is an identifier/attribute (single type), a ``tuple``
    (multi-catch, ``except (A, B):``), or an ``as_pattern`` wrapping
    either (``except X as e:`` / ``except (A, B) as e:``) — the
    ``as_pattern``'s own first named child (not its fielded ``alias``)
    is the actual caught-type value, unwrapped here before the
    tuple-vs-single check runs.
    """
    value = except_node.child_by_field_name("value")
    if value is None:
        return [], True
    if value.type == "as_pattern":
        children = value.named_children
        value = children[0] if children else None
        if value is None:
            return [], True
    if value.type == "tuple":
        names = []
        for child in value.named_children:
            name = _catch_type_name(child)
            if name:
                names.append(name)
        return names, False
    name = _catch_type_name(value)
    return ([name] if name else []), False


def _catches_java(catch_type_node: Node) -> tuple[list[str], bool]:
    """Walk a Java ``catch_type``'s named children into type names.

    A single type for an ordinary catch, 2+ (``catch_type``'s named
    children, separated by unnamed ``|`` tokens the query never
    captures) for Java's multi-catch. Java requires a typed parameter
    on every catch clause — no catch-all syntax exists — so ``bare``
    is always ``False``.
    """
    names = []
    for child in catch_type_node.named_children:
        name = _catch_type_name(child)
        if name:
            names.append(name)
    return names, False


def _catches_cpp(params_node: Node) -> tuple[list[str], bool]:
    """Walk a C++ ``catch_clause``'s ``parameters`` (a ``parameter_list``).

    Zero named children means a catch-all ``catch (...)`` (the ``...``
    token parses as anonymous, not a named node — see
    ``LanguageSpec.catch_query``'s docstring); otherwise the sole
    ``parameter_declaration``'s ``type`` field is the caught type. C++
    catch clauses never carry more than one type.
    """
    if params_node.named_child_count == 0:
        return [], True
    decl = params_node.named_children[0]
    type_node = decl.child_by_field_name("type")
    if type_node is None:
        return [], False
    name = _catch_type_name(type_node)
    return ([name] if name else []), False


def _catches_js(_catch_node: Node) -> tuple[list[str], bool]:
    """Plain JS never type-discriminates a caught value.

    Whether or not the clause binds a name (``catch (e) {}`` vs. bare
    ``catch {}``), there is no syntactic type to extract — always a
    catch-all. See ``LanguageSpec.catch_query``'s docstring.
    """
    return [], True


_TS_CATCH_ALL_TYPES = frozenset({"any", "unknown"})


def _catches_ts(catch_node: Node) -> tuple[list[str], bool]:
    """TS/TSX's optional ``type`` field on a ``catch_clause``.

    Absent, or annotated with the only two types TS's compiler permits
    on a catch variable (``any``/``unknown``, semantically a
    catch-all — see ``useUnknownInCatchVariables``), means catch-all;
    any other annotation is not valid TypeScript and can't occur in
    real code, but is still walked defensively rather than assumed
    unreachable.
    """
    type_node = catch_node.child_by_field_name("type")
    if type_node is None:
        return [], True
    inner = type_node.named_children
    if not inner:
        return [], True
    name = _catch_type_name(inner[0])
    if name is None or name in _TS_CATCH_ALL_TYPES:
        return [], True
    return [name], False


def _catch_entries(
    language: str, caps: dict[str, list[Node]], catch_node: Node
) -> tuple[list[str], bool]:
    """Dispatch one ``@catch`` match to its language-specific walker."""
    if language == "python":
        return _catches_python(catch_node)
    if language == "java":
        catch_type = _one(caps, "catch_type")
        if catch_type is None:
            return [], False
        return _catches_java(catch_type)
    if language == "cpp":
        params = _one(caps, "catch_params")
        if params is None:
            return [], True
        return _catches_cpp(params)
    if language == "javascript":
        return _catches_js(catch_node)
    if language in ("typescript", "tsx"):
        return _catches_ts(catch_node)
    return [], False


def _collect_throws(
    spec: LanguageSpec, root: Node, rel: str, defs: list[tuple[Node, Symbol]]
) -> list[RawThrow]:
    """Find raise/throw sites and (Java) declared ``throws``-clause
    entries, attributed to their enclosing definition.

    Mirrors ``_collect_refs``'s "return empty for languages without a
    query yet" shape (Rust/Go/C — see ``LanguageSpec.throw_query``'s
    docstring for why this is permanent, not "not yet implemented").
    Java's query produces two independently-matched shapes in one
    pass — an actual ``@throw`` site and a method's own ``@throws_
    clause`` — each attributed via ``_enclosing`` the same way, since
    a declared checked exception is just as much part of "what this
    method's error surface includes" as an explicit throw statement.
    """
    if spec.throw_query is None:
        return []
    spans = [(node.start_byte, node.end_byte, sym) for node, sym in defs]
    out: list[RawThrow] = []
    for _, caps in _run_query(spec.grammar, spec.throw_query, root):
        throw_node = _one(caps, "throw")
        if throw_node is not None:
            caller = _enclosing(spans, throw_node.start_byte)
            caller_id = caller.id if caller else None
            expr = _raise_expr(throw_node)
            line = throw_node.start_point[0] + 1
            is_bound_reraise = (
                expr is not None
                and expr.type == "identifier"
                and _nearest_catch_binding(
                    spec.name, throw_node, spec.function_boundary_types
                )
                == _text(expr)
            )
            if expr is None or is_bound_reraise:
                out.append(
                    RawThrow(
                        caller_id=caller_id,
                        path=rel,
                        text=None,
                        name=None,
                        line=line,
                    )
                )
                continue
            text, name = _throw_type_parts(expr)
            out.append(
                RawThrow(
                    caller_id=caller_id,
                    path=rel,
                    text=text,
                    name=name,
                    line=line,
                )
            )
            continue
        throws_clause = _one(caps, "throws_clause")
        if throws_clause is None:
            continue
        caller = _enclosing(spans, throws_clause.start_byte)
        caller_id = caller.id if caller else None
        line = throws_clause.start_point[0] + 1
        for child in throws_clause.named_children:
            name = _catch_type_name(child)
            if not name:
                continue
            out.append(
                RawThrow(
                    caller_id=caller_id,
                    path=rel,
                    text=_text(child),
                    name=name,
                    line=line,
                )
            )
    return out


def _collect_catches(
    spec: LanguageSpec, root: Node, rel: str, defs: list[tuple[Node, Symbol]]
) -> list[RawCatch]:
    """Find except/catch clauses, attributed to their enclosing definition.

    Mirrors ``_collect_throws``'s "return empty for languages without
    a query yet" shape.
    """
    if spec.catch_query is None:
        return []
    spans = [(node.start_byte, node.end_byte, sym) for node, sym in defs]
    out: list[RawCatch] = []
    for _, caps in _run_query(spec.grammar, spec.catch_query, root):
        catch_node = _one(caps, "catch")
        if catch_node is None:
            continue
        caller = _enclosing(spans, catch_node.start_byte)
        types, bare = _catch_entries(spec.name, caps, catch_node)
        out.append(
            RawCatch(
                caller_id=caller.id if caller else None,
                path=rel,
                types=types,
                bare=bare,
                line=catch_node.start_point[0] + 1,
            )
        )
    return out


# ---------------------------------------------------------------------
# Env-var reads (config/env value tracing — scoped pilot)


def _string_literal_value(node: Node) -> str | None:
    """Interior text of a plain string-literal node, or ``None`` when
    the literal is an f-string — a dynamic-key form dekko can't
    statically resolve, even one with no ``{...}`` interpolation at
    all (e.g. ``f"PORT"``), rejected purely by its ``f``/``F`` prefix
    rather than by inspecting for an ``interpolation`` child.

    Handles every language's key-literal node shape captured by
    ``LanguageSpec.env_read_query`` (Python's ``string``, Java/Rust/
    Go/C/C++'s ``string_literal``/``interpreted_string_literal``) —
    all quote a plain string body the same way once any prefix
    (``r``/``b``/``u``/``f``, Python's only) is stripped. Every
    non-Python language's env-read query captures only a node type
    that a computed/formatted key structurally cannot produce (a JS
    template literal is node type ``template_string``, not
    ``string``; Rust's ``format!`` is a macro call, not a
    ``string_literal`` argument) — so this f-prefix check is a
    Python-only concern in practice, harmless as a no-op elsewhere.
    """
    raw = _raw(node)
    prefix_match = _STR_PREFIX.match(raw)
    prefix = prefix_match.group(0) if prefix_match else ""
    if "f" in prefix.lower():
        return None
    text = raw[len(prefix) :]
    for quote in ('"""', "'''", '"', "'"):
        if (
            text.startswith(quote)
            and text.endswith(quote)
            and len(text) >= 2 * len(quote)
        ):
            return text[len(quote) : -len(quote)]
    return None


def _env_read_python(caps: dict[str, list[Node]]) -> tuple[str, str] | None:
    """``(call_shape, key)`` for one Python env-read match, or
    ``None`` when the identifier names don't match a known shape or
    the key literal is an f-string.

    Dispatches on which captures are present (mirrors ``_catch_
    entries``' presence-based dispatch): ``sub`` absent means the
    two-level ``os.getenv(...)`` shape; ``sub`` + ``fn`` present means
    the three-level ``os.environ.get(...)`` shape; ``sub`` present
    without ``fn`` means the ``os.environ[...]`` subscript form.
    """
    mod = _one(caps, "mod")
    key_node = _one(caps, "key")
    if mod is None or key_node is None or _text(mod) != "os":
        return None
    sub = _one(caps, "sub")
    fn = _one(caps, "fn")
    if sub is not None:
        if _text(sub) != "environ":
            return None
        call = "os.environ[]"
        if fn is not None:
            if _text(fn) != "get":
                return None
            call = "os.environ.get"
    elif fn is not None and _text(fn) == "getenv":
        call = "os.getenv"
    else:
        return None
    value = _string_literal_value(key_node)
    return (call, value) if value is not None else None


def _env_read_js(caps: dict[str, list[Node]]) -> tuple[str, str] | None:
    """``(call_shape, key)`` for one JS/TS/TSX env-read match.

    The dot-access shape's ``@key`` is a plain ``property_identifier``
    — its text *is* the env-var name already, never dynamic (dot
    syntax has no computed-name form). The bracket-access shape's
    ``@key`` is a ``(string)`` node, unwrapped by
    ``_string_literal_value``; a computed bracket key
    (``process.env[SOME_VAR]``) or template literal
    (`` `APP_${x}` ``) never matches the query's ``(string)``/
    ``property_identifier`` node types at all (see
    ``languages._JS_ENV_READ_QUERY``'s docstring), so no extra
    dynamic-key filtering is needed here.
    """
    proc = _one(caps, "proc")
    env = _one(caps, "env")
    key_node = _one(caps, "key")
    if proc is None or env is None or key_node is None:
        return None
    if _text(proc) != "process" or _text(env) != "env":
        return None
    if key_node.type == "property_identifier":
        return "process.env", _text(key_node)
    value = _string_literal_value(key_node)
    return ("process.env[]", value) if value is not None else None


def _env_read_java(caps: dict[str, list[Node]]) -> tuple[str, str] | None:
    """``(call_shape, key)`` for one Java ``System.getenv(...)`` match."""
    sys_node = _one(caps, "sys")
    fn = _one(caps, "fn")
    key_node = _one(caps, "key")
    if sys_node is None or fn is None or key_node is None:
        return None
    if _text(sys_node) != "System" or _text(fn) != "getenv":
        return None
    value = _string_literal_value(key_node)
    return ("System.getenv", value) if value is not None else None


def _env_read_rust(caps: dict[str, list[Node]]) -> tuple[str, str] | None:
    """``(call_shape, key)`` for one Rust ``env::var``-family match.

    ``call`` is the captured scoped-identifier text exactly as
    written (``std::env::var``, bare ``env::var``, either's ``_os``
    variant) rather than a canonicalized label — so the same key read
    via ``std::env::var`` in one function and bare ``env::var`` (after
    a local ``use std::env;``) in another still surfaces as two
    distinct ``call`` values, same as every other language's
    shape-disclosure intent.
    """
    fn = _one(caps, "fn")
    key_node = _one(caps, "key")
    if fn is None or key_node is None:
        return None
    fn_text = _text(fn)
    if not (fn_text.endswith("::var") or fn_text.endswith("::var_os")):
        return None
    value = _string_literal_value(key_node)
    return (fn_text, value) if value is not None else None


def _env_read_go(caps: dict[str, list[Node]]) -> tuple[str, str] | None:
    """``(call_shape, key)`` for one Go ``os.Getenv``/``os.LookupEnv``
    match."""
    mod = _one(caps, "mod")
    fn = _one(caps, "fn")
    key_node = _one(caps, "key")
    if mod is None or fn is None or key_node is None:
        return None
    if _text(mod) != "os":
        return None
    fn_name = _text(fn)
    if fn_name not in ("Getenv", "LookupEnv"):
        return None
    value = _string_literal_value(key_node)
    return (f"os.{fn_name}", value) if value is not None else None


def _env_read_c_cpp(caps: dict[str, list[Node]]) -> tuple[str, str] | None:
    """``(call_shape, key)`` for one C/C++ bare ``getenv(...)`` match."""
    fn = _one(caps, "fn")
    key_node = _one(caps, "key")
    if fn is None or key_node is None or _text(fn) != "getenv":
        return None
    value = _string_literal_value(key_node)
    return ("getenv", value) if value is not None else None


# Per-language env-read match walker, keyed by ``LanguageSpec.name`` —
# every walker returns ``(call_shape, key)`` for a name-matched hit or
# ``None`` for a structurally-matched but name-mismatched call (e.g.
# Python's ``json.dumps("x")``, which shares ``os.getenv``'s two-level
# attribute-call shape; the matching is curated, not general
# string-matching, for precision).
_ENV_READ_DISPATCH: dict[
    str, Callable[[dict[str, list[Node]]], tuple[str, str] | None]
] = {
    "python": _env_read_python,
    "javascript": _env_read_js,
    "typescript": _env_read_js,
    "tsx": _env_read_js,
    "java": _env_read_java,
    "rust": _env_read_rust,
    "go": _env_read_go,
    "c": _env_read_c_cpp,
    "cpp": _env_read_c_cpp,
}


def _is_env_write_or_delete_target(language: str, call_node: Node) -> bool:
    """Whether ``call_node`` (the matched env-read subscript/member
    node) sits in a write or delete position rather than a read
    position.

    A write is an assignment's ``left`` field (Python ``assignment``,
    JS/TS ``assignment_expression`` — **not** Python's
    ``augmented_assignment``, since ``x += v`` genuinely reads ``x``
    before writing it). A delete is Python's ``delete_statement`` (an
    unfielded child) or JS/TS's ``unary_expression`` with a ``delete``
    operator (the ``argument`` field). Every other Tier-1 language's
    env-read idiom is a call expression, which can never be an
    assignment target or delete operand — this check is a no-op for
    them by construction (see ``LanguageSpec.env_read_query``'s
    docstring).
    """
    # Tree-sitter ``Node`` objects are re-wrapped on each access (e.g.
    # each call to ``child_by_field_name``), so identity comparison
    # with ``is`` unreliably returns ``False`` even for the same
    # underlying node -- confirmed live against the pinned grammar
    # (two separately-fetched handles to the identical node compare
    # ``==`` True but ``is`` False). Compare by ``==`` instead, as
    # tree-sitter's own ``Node.__eq__`` is defined for exactly this.
    parent = call_node.parent
    if parent is None:
        return False
    if language == "python":
        if parent.type == "assignment":
            return parent.child_by_field_name("left") == call_node
        return parent.type == "delete_statement"
    if language in ("javascript", "typescript", "tsx"):
        if parent.type == "assignment_expression":
            return parent.child_by_field_name("left") == call_node
        if parent.type == "unary_expression":
            operator = parent.child_by_field_name("operator")
            return (
                operator is not None
                and _text(operator) == "delete"
                and parent.child_by_field_name("argument") == call_node
            )
    return False


def _collect_env_reads(
    spec: LanguageSpec, root: Node, rel: str, defs: list[tuple[Node, Symbol]]
) -> list[EnvRead]:
    """Find statically-known environment-variable read call sites,
    attributed to their enclosing definition.

    A detector, not a resolver — the literal key text extracted here
    *is* the fully-resolved fact (see ``model.EnvRead``'s docstring).
    Mirrors ``_collect_throws``/``_collect_catches``'s "no query for
    this language" empty-list shape, though every Tier-1 language sets
    ``env_read_query`` (no permanent per-language exclusion here, per
    ``LanguageSpec.env_read_query``'s docstring).
    """
    if spec.env_read_query is None:
        return []
    dispatch = _ENV_READ_DISPATCH.get(spec.name)
    if dispatch is None:
        return []
    spans = [(node.start_byte, node.end_byte, sym) for node, sym in defs]
    out: list[EnvRead] = []
    for _, caps in _run_query(spec.grammar, spec.env_read_query, root):
        call_node = _one(caps, "call")
        if call_node is None:
            continue
        if _is_env_write_or_delete_target(spec.name, call_node):
            continue
        hit = dispatch(caps)
        if hit is None:
            continue
        call, key = hit
        caller = _enclosing(spans, call_node.start_byte)
        out.append(
            EnvRead(
                caller_id=caller.id if caller else None,
                path=rel,
                key=key,
                call=call,
                line=call_node.start_point[0] + 1,
            )
        )
    return out


# ---------------------------------------------------------------------
# Type aliases


def _tuple_struct_fields(def_node: Node) -> list[Param]:
    """A Rust tuple struct's positional fields, as its parameters.

    ``struct GroupName(String);`` is constructed by the call-shaped
    ``GroupName(s)``, so its fields are its signature. They used to be
    dropped (every type got ``params=[]``), which ``resolver.
    _arity_plausible`` reads as "takes zero arguments": a counted
    ``GroupName(s)`` reaching the sole-candidate rung (through a glob
    import, the norm in Rust test modules) was rejected and filed
    external. A brace struct and a unit struct keep
    ``[]``, correctly: neither can be written ``Name(x)``.
    """
    if def_node.type != "struct_item":
        return []
    body = def_node.child_by_field_name("body")
    if body is None or body.type != "ordered_field_declaration_list":
        return []
    return [
        Param(name=str(i), type=_text(field_type))
        for i, field_type in enumerate(body.children_by_field_name("type"))
    ]


def _collect_enum_variants(spec: LanguageSpec, root: Node) -> list[str]:
    """``"Owner::Variant"`` for every tuple enum variant in this file
    (see ``LanguageSpec.enum_variant_query``)."""
    if spec.enum_variant_query is None:
        return []
    out: list[str] = []
    for _, caps in _run_query(spec.grammar, spec.enum_variant_query, root):
        owner, variant = _one(caps, "enum"), _one(caps, "variant")
        if owner is not None and variant is not None:
            out.append(f"{_text(owner)}::{_text(variant)}")
    return out


def _collect_type_aliases(spec: LanguageSpec, root: Node) -> list[str]:
    """Bare names of type-alias declarations in this file.

    TS/TSX only (see ``LanguageSpec.type_alias_query``'s docstring) --
    a lightweight file-scoped name registry, not full symbols, feeding
    ``query._heritage_external_label``'s same-file lookup (on
    claude-code, ``ShellCommandImpl implements ShellCommand``
    where ``ShellCommand`` is a same-file ``type X = {...}`` alias was
    mislabeled ``(external)`` for lack of this signal).
    """
    if spec.type_alias_query is None:
        return []
    names: list[str] = []
    for _, caps in _run_query(spec.grammar, spec.type_alias_query, root):
        name_node = _one(caps, "name")
        if name_node is not None:
            names.append(_text(name_node))
    return names


# ---------------------------------------------------------------------
# Type uses on function-shaped nodes that are not symbols

# Parameter lists the definition pass reaches through one wrapper: a
# ``const f = (a: A) => ...`` is captured with ``@def`` on the
# ``variable_declarator``, so its ``formal_parameters``' parent is the
# arrow, not the definition node.
_DECLARATOR_BOUND_FUNCTIONS = frozenset(
    {"arrow_function", "function_expression"}
)
# ``construct_signature`` (``new (a: A): B``) is the one function-shaped
# node whose return annotation sits in ``type`` rather than
# ``return_type`` in the pinned tree-sitter-typescript grammar.
_RETURN_TYPE_FIELDS = ("return_type", "type")


def _is_claimed_params(node: Node, def_ids: frozenset[int]) -> bool:
    """Whether the definition pass already parsed this parameter list."""
    parent = node.parent
    if parent is None:
        return True
    if parent.id in def_ids:
        return True
    grand = parent.parent
    return (
        parent.type in _DECLARATOR_BOUND_FUNCTIONS
        and grand is not None
        and grand.id in def_ids
    )


def _return_type_text(owner: Node) -> str | None:
    """The owner node's return annotation, normalized like ``returns``."""
    fields = _RETURN_TYPE_FIELDS
    if owner.type != "construct_signature":
        fields = fields[:1]
    for name in fields:
        ret = owner.child_by_field_name(name)
        if ret is not None:
            return _text(ret).lstrip(":").strip() or None
    return None


def _collect_type_uses(
    spec: LanguageSpec,
    root: Node,
    rel: str,
    defs: list[tuple[Node, Symbol]],
) -> list[TypeUse]:
    """Parameter/return annotations on parameter lists no symbol owns.

    Walks every ``formal_parameters`` node ``spec.type_use_query``
    finds and skips the ones ``_collect_definitions`` already parsed
    into a ``Symbol`` (``_is_claimed_params``). What remains is every
    function-shaped node the map has no name for: returned and
    callback arrow functions, function-typed interface members and
    type aliases, method/call/construct signatures, overload
    signatures, class-field arrows. Their annotations are the same
    ``required_parameter``/``optional_parameter`` shape the owning
    language's parameter parser already handles, so each typed
    parameter and each return type becomes one ``TypeUse`` attributed
    to the innermost enclosing definition (``_enclosing``, as
    ``_collect_refs`` does), or to the module when there is none. A
    list under an ``ERROR`` node is skipped: tree-sitter could not
    place it, so neither can this.

    Args:
        spec: Language spec; returns ``[]`` when it has no
            ``type_use_query``.
        root: Parsed tree root.
        rel: Repo-relative path, stored on every record.
        defs: The definition pass's ``(node, symbol)`` pairs.

    Returns:
        One record per typed parameter and per return type, in
        source order.
    """
    if spec.type_use_query is None:
        return []
    def_ids = frozenset(node.id for node, _ in defs)
    spans = [(node.start_byte, node.end_byte, sym) for node, sym in defs]
    out: list[TypeUse] = []
    for _, caps in _run_query(spec.grammar, spec.type_use_query, root):
        params_node = _one(caps, "params")
        if params_node is None or _is_claimed_params(params_node, def_ids):
            continue
        owner = params_node.parent
        if owner is None or owner.type == "ERROR":
            continue
        enclosing = _enclosing(spans, params_node.start_byte)
        owner_id = enclosing.id if enclosing else None
        params = _parse_params(spec.param_style, params_node)
        # The TS parameter parser emits one ``Param`` per named child,
        # in order, so the pairing below recovers each parameter's own
        # line; a parser that ever skips children falls back to the
        # list's line rather than misattribute.
        nodes = params_node.named_children
        aligned = len(nodes) == len(params)
        for i, param in enumerate(params):
            if not param.type:
                continue
            at = nodes[i] if aligned else params_node
            out.append(
                TypeUse(
                    owner_id=owner_id,
                    path=rel,
                    line=at.start_point[0] + 1,
                    site=owner.type,
                    usage="param",
                    param_name=param.name,
                    type=param.type,
                )
            )
        returns = _return_type_text(owner)
        if returns is not None:
            out.append(
                TypeUse(
                    owner_id=owner_id,
                    path=rel,
                    line=owner.start_point[0] + 1,
                    site=owner.type,
                    usage="return",
                    param_name=None,
                    type=returns,
                )
            )
    return out


# ---------------------------------------------------------------------
# Imports


def _import_matches(spec: LanguageSpec, root: Node) -> _QueryMatches:
    """Run the import query once; refs and imports both read it."""
    if spec.import_query is None:
        return []
    return _run_query(spec.grammar, spec.import_query, root)


def _import_binding_bytes(matches: _QueryMatches) -> frozenset[int]:
    """Start bytes of every local name an import match binds.

    A function-local ``const { X } = await import("./x")`` is a
    ``variable_declarator`` like any other local, and
    ``_local_bindings`` has to know it binds an import, not a shadowing
    variable (about 40 true edges on claude-code).
    The import query already found them, so the answer is read off its
    matches rather than re-derived a second way. Applies the same
    ``@binder`` check ``_imports_js`` does: ``const x = t("key")`` is
    not an import.
    """
    out: set[int] = set()
    for _, caps in matches:
        binder = _one(caps, "binder")
        if binder is not None and _text(binder) != "require":
            continue
        local = _one(caps, "alias") or _one(caps, "name")
        if local is not None:
            out.add(local.start_byte)
    return frozenset(out)


def _collect_imports(
    spec: LanguageSpec, rel: str, matches: _QueryMatches
) -> list[Import]:
    """Extract imported names for the resolver, per language."""
    if spec.import_query is None:
        return []
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
    """Normalize JS/TS import statements (named, default, namespace,
    and side-effect/bare), plus the two binding forms that aren't
    ``import`` statements at all: a dynamic ``const { X } = await
    import("./x")`` and a CommonJS ``const { X } = require("./x")`` /
    ``const x = require("./x")``.

    Those two bind a cross-file name exactly as a
    static import does, and the reference-visibility veto
    (``resolver._ref_target_visible``) needs to see them. Without
    them it dropped 23 true edges on claude-code, every one a lazily
    imported component. The query can't spell "a call to ``require``"
    without a predicate, so it captures any ``f("...")`` initializer as
    ``@binder`` and the name is checked here.
    """
    out: list[Import] = []
    for _, caps in matches:
        module = _one(caps, "from_module")
        if module is None:
            continue
        binder = _one(caps, "binder")
        if binder is not None and _text(binder) != "require":
            continue
        source = _strip_quotes(_text(module))
        name = _one(caps, "name")
        if name is None:
            # Side-effect import (`import "./foo.css";`) — no local
            # binding. ``name=""`` is the signal downstream JS-specific
            # resolver code (``resolver._resolve_import_js``,
            # ``bare_import_source``) uses to know this source has no
            # appended "/name" suffix to strip.
            out.append(Import(path=rel, name="", source=source))
            continue
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
    (see its whole-file-include fallback), not this ``name``.
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
