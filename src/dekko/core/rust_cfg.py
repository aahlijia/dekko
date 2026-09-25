"""Rust test scope: what the compiler builds only under ``cargo test``.

Rust marks test code with an attribute, not a name: ``#[cfg(test)]`` on
a module, a single item, or (as ``#![cfg(test)]``) a whole file. A
module declared out of line (``#[cfg(test)] mod editor_tests;``) puts
the attribute in the *parent* file and the code in the child. This
module answers both halves from syntax alone:

- ``in_test_scope``: whether a definition node sits anywhere the
  compiler gates on ``test`` (used per symbol by the extractor).
- ``collect_submodules``: every out-of-line ``mod x;`` in a file, with
  the file paths it can resolve to and whether it is test-only (used
  by ``repo_ops`` to flag whole child files, a cross-file fact no
  single extraction can see).

A ``cfg`` predicate is *evaluated*, not pattern-matched: ``test`` is
false and every other atom unknown, with Kleene ``all``/``any``/
``not``, and an item is test-only exactly when its predicate is false.
A regex reads ``all(target_os = "macos", any(test, feature =
"test-support"))`` as test-only; it isn't, since the ``test-support``
feature builds that code into benchmarks and binaries.
"""

import re

from tree_sitter import Node

from dekko.core.model import Submodule

# Inline module names conventionally used for unit tests (never applied
# to an out-of-line ``mod test;``, see ``_is_test_scope_node``). Every such
# module in the eval repos is also ``cfg``-gated, so the attribute rule
# alone would catch them; the name rule is kept so nothing flagged
# before the attribute rule existed stops being flagged (two zed
# ``mod tests`` blocks are gated ``any(test, feature = "test-support")``).
TEST_MOD_NAMES = frozenset({"tests", "test"})

# ``#[cfg(...)]`` or ``#![cfg(...)]``, capturing the predicate.
_CFG_ATTR = re.compile(r"^#!?\[\s*cfg\s*\((?P<pred>.*)\)\s*\]$", re.S)
_TOKEN = re.compile(r'\s*(?:([A-Za-z_]\w*)|("(?:[^"\\]|\\.)*")|([(),=]))')
_PATH_ATTR = re.compile(r'^#\[\s*path\s*=\s*"(?P<path>[^"]+)"\s*\]$')
# Rust's index-file stems: a ``mod.rs``/``lib.rs``/``main.rs`` file is
# its directory's own module, so its submodules sit beside it.
_INDEX_STEMS = frozenset({"mod", "lib", "main"})
_COMMENT_TYPES = frozenset({"line_comment", "block_comment"})


def cfg_is_test_only(attr_text: str) -> bool:
    """Whether a ``#[cfg(...)]`` attribute limits its item to test builds.

    Args:
        attr_text: One attribute's source text, e.g. ``#[cfg(test)]``.

    Returns:
        True when the predicate is false with ``test`` false and every
        other atom unknown. False for any other attribute, for a
        predicate that could hold outside tests, and for one that
        doesn't parse (unknown is never "test").
    """
    match = _CFG_ATTR.match(attr_text.strip())
    if match is None:
        return False

    tokens = _tokenize(match.group("pred"))
    if not tokens:
        return False

    try:
        value, end = _evaluate(tokens, 0)
    except IndexError:
        return False

    return end == len(tokens) and value is False


def _tokenize(text: str) -> list[str] | None:
    """Split a predicate into identifiers, strings and ``(),=``."""
    tokens: list[str] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        match = _TOKEN.match(text, pos)
        if match is None or match.end() == pos:
            return None
        tokens.append(next(g for g in match.groups() if g is not None))
        pos = match.end()

    return tokens


def _evaluate(tokens: list[str], pos: int) -> tuple[bool | None, int]:
    """Evaluate one predicate starting at ``tokens[pos]``.

    Returns:
        ``(value, next_pos)``: ``False``/``True``, or ``None`` for
        unknown.
    """
    name = tokens[pos]
    pos += 1
    if name in ("all", "any", "not") and tokens[pos] == "(":
        args, pos = _evaluate_args(tokens, pos + 1)
        return _combine(name, args), pos

    if pos < len(tokens) and tokens[pos] == "=":
        return None, pos + 2

    return (False if name == "test" else None), pos


def _evaluate_args(
    tokens: list[str], pos: int
) -> tuple[list[bool | None], int]:
    """Evaluate a comma-separated argument list up to its ``)``."""
    args: list[bool | None] = []
    while tokens[pos] != ")":
        value, pos = _evaluate(tokens, pos)
        args.append(value)
        if tokens[pos] == ",":
            pos += 1

    return args, pos + 1


def _combine(op: str, args: list[bool | None]) -> bool | None:
    """Kleene ``all``/``any``/``not`` over three-valued arguments."""
    if op == "not":
        return None if not args or args[0] is None else not args[0]

    if op == "all":
        if False in args:
            return False
        return None if None in args else True

    if True in args:
        return True

    return None if None in args else False


def _outer_attributes(node: Node) -> list[str]:
    """Source text of the ``#[...]`` attributes directly above ``node``."""
    texts: list[str] = []
    sib = node.prev_named_sibling
    while sib is not None and (
        sib.type == "attribute_item" or sib.type in _COMMENT_TYPES
    ):
        if sib.type == "attribute_item":
            texts.append(_node_text(sib))
        sib = sib.prev_named_sibling

    return texts


def _inner_attributes(scope: Node) -> list[str]:
    """Source text of the leading ``#![...]`` attributes of a scope."""
    texts: list[str] = []
    for child in scope.named_children:
        if child.type == "inner_attribute_item":
            texts.append(_node_text(child))
        elif child.type not in _COMMENT_TYPES:
            break

    return texts


def _node_text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", errors="replace")


def _is_test_scope_node(node: Node) -> bool:
    """Whether ``node`` itself opens a test-only scope."""
    if node.type in ("source_file", "declaration_list"):
        return any(cfg_is_test_only(a) for a in _inner_attributes(node))

    if not node.type.endswith("_item"):
        return False

    if any(cfg_is_test_only(a) for a in _outer_attributes(node)):
        return True

    # The name rule is for inline ``mod tests { }`` blocks only. An
    # out-of-line ``pub mod test;`` is often a test-support module gated
    # ``any(test, feature = "test-support")`` (zed has nine), and its
    # own attributes already decided above.
    if node.type == "mod_item" and node.child_by_field_name("body"):
        name = node.child_by_field_name("name")
        return name is not None and _node_text(name) in TEST_MOD_NAMES

    return False


def in_test_scope(node: Node) -> bool:
    """Whether ``node`` is compiled only under test.

    Climbs from ``node`` itself to the file root, through function
    bodies (a helper nested in a test function is test code too).

    Args:
        node: A definition's syntax node.

    Returns:
        True when any enclosing item carries a test-only ``cfg``, an
        enclosing module is named ``tests``/``test``, or an enclosing
        scope opens with ``#![cfg(test)]``.
    """
    current: Node | None = node
    while current is not None:
        if _is_test_scope_node(current):
            return True
        current = current.parent

    return False


def submodule_candidates(
    path: str, name: str, path_attr: str | None, nest: tuple[str, ...]
) -> list[str]:
    """Files a ``mod name;`` declared in ``path`` can resolve to.

    In rustc's order. An index file (``mod.rs``/``lib.rs``/``main.rs``,
    or a crate root named after its crate, ``<crate>/src/<crate>.rs``,
    the resolver's own convention) keeps its submodules beside it; a
    leaf ``foo.rs`` keeps them in ``foo/``, with the sibling pair as a
    fallback for crate roots neither convention names (a leaf file's
    ``mod x;`` can only resolve beside it when the file is a crate
    root, so the fallback never picks a wrong file in code that
    compiles).

    Args:
        path: Repo-relative path of the declaring file.
        name: The declared module name.
        path_attr: A ``#[path = "..."]`` override, resolved against the
            declaring file's directory.
        nest: Names of the inline ``mod a { ... }`` blocks enclosing
            the declaration, outermost first.

    Returns:
        Candidate repo-relative paths, most likely first.
    """
    directory, _, filename = path.rpartition("/")
    stem = filename.removesuffix(".rs")
    if _is_index_file(directory, stem):
        base = directory
    else:
        base = _join(directory, stem)
    base = _join(base, *nest)
    if path_attr is not None:
        return [_join(base if nest else directory, path_attr)]

    candidates = [_join(base, f"{name}.rs"), _join(base, name, "mod.rs")]
    if not nest and base != directory:
        candidates += [
            _join(directory, f"{name}.rs"),
            _join(directory, name, "mod.rs"),
        ]

    return candidates


def _is_index_file(directory: str, stem: str) -> bool:
    if stem in _INDEX_STEMS:
        return True

    parent, _, last = directory.rpartition("/")
    return last == "src" and parent.rpartition("/")[2] == stem


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)


def collect_submodules(root: Node, rel: str) -> list[Submodule]:
    """Every out-of-line ``mod x;`` in a Rust file.

    Args:
        root: The file's parse-tree root.
        rel: Repo-relative path of the file.

    Returns:
        One ``Submodule`` per declaration without a body, in source
        order, test-only when the declaration sits in a test scope.
    """
    found: list[Submodule] = []
    stack: list[tuple[Node, tuple[str, ...]]] = [(root, ())]
    while stack:
        node, nest = stack.pop()
        for child in node.named_children:
            if child.type == "mod_item":
                found.extend(_submodule_of(child, rel, nest, stack))
            elif child.type not in ("function_item", "closure_expression"):
                stack.append((child, nest))

    return sorted(found, key=lambda s: s.line)


def _submodule_of(
    node: Node,
    rel: str,
    nest: tuple[str, ...],
    stack: list[tuple[Node, tuple[str, ...]]],
) -> list[Submodule]:
    """The ``Submodule`` for one ``mod_item``; queue an inline body."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return []

    name = _node_text(name_node)
    body = node.child_by_field_name("body")
    if body is not None:
        stack.append((body, (*nest, name)))
        return []

    path_attr = next(
        (
            m.group("path")
            for a in _outer_attributes(node)
            if (m := _PATH_ATTR.match(a.strip())) is not None
        ),
        None,
    )
    return [
        Submodule(
            candidates=submodule_candidates(rel, name, path_attr, nest),
            test_only=in_test_scope(node),
            line=node.start_point[0] + 1,
        )
    ]
