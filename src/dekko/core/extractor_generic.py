"""Tier-2 extraction: symbols and calls, read off a grammar's row.

Tier-1 languages have hand-written queries. Every other language is
read by this one engine, driven by that grammar's ``Tier2Spec`` row in
``tier2.py``: which node types define something, where the name sits,
which node types are calls. It yields names, raw parameter text and
call links, without Tier-1's type fidelity or imports.

The engine never decides from how a node type is spelled. A node type
the row does not list is not a definition and not a call.
"""

import re
from pathlib import Path

from dekko.core.extractor import (
    _callee_parts,
    _cap_callee,
    _doc_comment_above,
    _enclosing,
    _module_doc,
    _params_generic,
    _text,
)
from dekko.core.model import FileMap, RawCall, Symbol
from dekko.core.tier2 import (
    FUNCTION,
    TIER2_SPECS,
    FormSpec,
    Tier2Spec,
    walk,
)
from tree_sitter import Node, Parser
from dekko.core.grammars import get_grammar

# A shell ``command`` is any word in command position: ``./build.sh``,
# ``"$TOOL"``, ``-f``. Only a plain identifier can ever be a call to a
# repo-defined function, so everything else is dropped at the source
# rather than inflating ``external`` with one entry per path and flag.
# The optional ``::`` is Tcl's absolute namespace (``::ns::proc``),
# whose commands share the node type.
_SHELL_FUNCTION_NAME = re.compile(r"^(?:::)?[A-Za-z_][\w.:-]*$")

_CALLEE_FIELDS = ("function", "callee", "constructor")
_NAME_FIELDS = ("method", "name", "field")
_RECEIVER_FIELDS = ("receiver", "object", "operand", "value")

# A definition name holding whitespace, an expansion or a bracket is an
# expression the grammar filed under a name field.
_BAD_NAME = re.compile(r"[\s${}()]")
_MAX_NAME = 120
# The same for a callee: ``(0..<rank).map`` and ``"$TOOL"`` are
# expressions. No symbol is named that, so keeping them only adds
# ``external`` rows nothing can ever match.
_CALL_NAME = re.compile(r"^[^\s(){}\[\];,\"'`$]{1,80}$")
_QUOTES = "\"'`[]"
_SCOPE_SPLIT = re.compile(r"[.:]+")
# How far a name may sit below the node a path lands on.
_MAX_UNWRAP = 8
_MAX_CALLEE_UNWRAP = 4
_HEAD_TYPES = ("sym_lit", "symbol", "identifier")
_CALLABLE = (FUNCTION, "method")

_Containers = tuple[str, ...]
# What a definition rule reports: the container path the node opens
# for its children (``None`` leaves it as it was), and whether the
# node was a definition.
_Outcome = tuple[_Containers | None, bool]
# The same for a form, plus whether its child forms are its methods.
_FormOutcome = tuple[_Containers | None, bool, bool]
_Callee = tuple[str, str, str | None]


def extract_file_generic(root: Path, rel: str, grammar: str) -> FileMap:
    """Extract symbols and calls from a Tier-2 language file.

    Args:
        root: Repository root.
        rel: Repo-relative POSIX path of the file.
        grammar: Grammar name for ``tree-sitter-language-pack``.

    Returns:
        A ``FileMap``; on read/parse/grammar failure, or for a grammar
        with no row, one with ``error`` set and no symbols.
    """
    spec = TIER2_SPECS.get(grammar)
    if spec is None:
        return FileMap(
            path=rel,
            language=grammar,
            error=f"no Tier-2 row for grammar {grammar!r}",
        )
    try:
        source = (root / rel).read_bytes()
        parser = Parser(get_grammar(grammar))
        tree = parser.parse(source)
    except Exception as exc:  # grammar download/parse can fail
        return FileMap(path=rel, language=grammar, error=str(exc))

    walker = _Extractor(spec, rel, grammar)
    walker.run(tree.root_node)

    return FileMap(
        path=rel,
        language=grammar,
        symbols=walker.symbols,
        calls=walker.raw_calls(),
        doc=_module_doc(grammar, tree.root_node),
    )


def _last_line(node: Node) -> int:
    """The 1-based last line of a node.

    Some grammars end a definition on the newline after it (Fortran's
    ``end function``), which puts the node's end at column 0 of the
    line below.
    """
    row, col = node.end_point
    if col == 0 and row > node.start_point[0]:
        return row

    return row + 1


def _head(node: Node) -> tuple[str | None, Node | None]:
    """Head word of a call or list form, and its node when it is named.

    Elixir and Tcl keep the head in a field. A Lisp list has no fields,
    so its head is the first child that is not a bracket or a comment,
    and only a symbol counts: ``((f x) y)`` has no head word.
    """
    target = node.child_by_field_name("target")
    if target is None:
        target = node.child_by_field_name("name")
    if target is not None:
        return _text(target), target

    for child in node.children:
        if child.type in ("(", "[", "comment"):
            continue
        if not child.is_named:
            # elisp ``special_form``: the head is an anonymous token.
            return child.type, None
        if child.type in _HEAD_TYPES:
            return _text(child), child

        return None, None

    return None, None


def _holds_parameters(
    form: FormSpec, node: Node, name_node: Node, after: Node
) -> bool:
    """Whether the node right after a definition's name is its parameters.

    Args:
        form: The grammar's form rules.
        node: The defining form.
        name_node: The definition's name.
        after: The named sibling that follows the name.

    Returns:
        ``True`` for ``(cl-defun f (x y) ...)``'s ``(x y)`` and for
        Tcl's ``method f {x y} {...}``. ``False`` for
        ``(define f (lambda (x) ...))``: that list is the function
        itself and its body holds the calls.
    """
    if after.type in form.params:
        return True

    parent = name_node.parent
    if parent is None or parent.id != node.id:
        return False
    if after.type not in form.nodes:
        return False

    word, _ = _head(after)
    return word not in form.bindings


def _call_parts(node: Node) -> _Callee | None:
    """Find the callee name/receiver on a call node by its fields."""
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


class _Extractor:
    """One file's walk: the row's rules applied to every node.

    Attributes:
        symbols: Definitions found, in source order.
        spans: ``(start byte, end byte, symbol)`` for each definition
            node. A function with several clauses has one per clause.
        calls: ``(call node, callee text, name, receiver)``.
        consumed: Ids of nodes already used up by a definition (its
            name, the wrappers around the name) or ruled out as calls
            (a binding list). Never a definition or a call again.
        skipped: Ids of subtrees not descended into (parameter lists).
    """

    def __init__(self, spec: Tier2Spec, rel: str, grammar: str) -> None:
        self.spec = spec
        self.rel = rel
        self.grammar = grammar
        self.symbols: list[Symbol] = []
        self.spans: list[tuple[int, int, Symbol]] = []
        self.calls: list[tuple[Node, str, str, str | None]] = []
        self.consumed: set[int] = set()
        self.skipped: set[int] = set()
        self._seen: dict[str, int] = {}
        # (qualname, symbol, len(symbols) right after it) of the last
        # function recorded, for clause merging.
        self._last_fn: tuple[str, Symbol, int] | None = None

    def run(self, root: Node) -> None:
        """Walk the tree depth-first, in source order."""
        opaque = self.spec.opaque
        stack: list[tuple[Node, _Containers, bool]] = [(root, (), False)]
        while stack:
            node, containers, member_of = stack.pop()
            if node.type in opaque or node.id in self.skipped:
                continue
            inherited, members = self._visit(node, containers, member_of)
            stack.extend(
                (child, inherited, members)
                for child in reversed(node.named_children)
            )

    def raw_calls(self) -> list[RawCall]:
        """The calls found, each attributed to its enclosing symbol."""
        out: list[RawCall] = []
        for node, text, name, receiver in self.calls:
            text, receiver = _cap_callee(text, name, receiver)
            caller = _enclosing(self.spans, node.start_byte)
            out.append(
                RawCall(
                    caller_id=caller.id if caller else None,
                    path=self.rel,
                    text=text,
                    name=name,
                    receiver=receiver,
                    line=node.start_point[0] + 1,
                )
            )

        return out

    def _visit(
        self, node: Node, containers: _Containers, member_of: bool
    ) -> tuple[_Containers, bool]:
        """Apply the rules to one node.

        Returns:
            The container path the node's children inherit, and
            whether its child forms are its methods.
        """
        if node.id in self.consumed:
            return containers, False

        inherited = containers
        members = False
        opened, is_def = self._definition(node, containers)
        if opened is not None:
            inherited = opened
        if not is_def:
            opened, is_def, members = self._form(node, containers, member_of)
            if opened is not None:
                inherited = opened
        if not is_def and self._is_call(node):
            self._call_at(node)

        return inherited, members

    # -- names and symbols --------------------------------------------

    def _name_node(self, node: Node, path: str) -> Node | None:
        """Follow a name path, then unwrap down to the bare name."""
        unwrap = self.spec.unwrap
        found = walk(node, path)
        hops = 0
        while found is not None and found.type in unwrap:
            if hops == _MAX_UNWRAP:
                break
            found = walk(found, unwrap[found.type])
            hops += 1

        return found

    def _consume_path(self, node: Node, name_node: Node) -> None:
        """A definition's name is not a call, nor are its wrappers.

        Julia's ``function f(x)`` holds ``f(x)`` as a call expression,
        and Elixir's ``def f(x)`` likewise.
        """
        cur: Node | None = name_node
        while cur is not None and cur.id != node.id:
            self.consumed.add(cur.id)
            cur = cur.parent

    def _written_name(self, name_node: Node | None, fixed: str | None) -> str:
        """The name as written, or ``""`` when it is not a usable name."""
        if fixed is not None:
            raw = fixed
        elif name_node is None:
            return ""
        else:
            raw = _text(name_node)
            if self.spec.unquote:
                raw = raw.strip(_QUOTES)
        if _BAD_NAME.search(raw) or len(raw) > _MAX_NAME:
            return ""

        return raw

    def _name_parts(self, raw: str) -> list[str]:
        if self.spec.split is None:
            return [raw]

        return [p for p in re.split(self.spec.split, raw) if p]

    def _span_end(self, node: Node) -> Node:
        """The node a definition's span runs to.

        Normally the definition itself. Dart and Zig put the body next
        to the signature, as a sibling of it or of its parent.
        """
        body = self.spec.body_after.get(node.type)
        if body is None:
            return node

        parent = node.parent
        for cand in (
            node.next_named_sibling,
            parent.next_named_sibling if parent is not None else None,
        ):
            if cand is not None and cand.type == body:
                return cand

        return node

    def _merge_clause(
        self, qualname: str, kind: str, node: Node, end: Node
    ) -> bool:
        """Fold a clause into the function the previous clause opened.

        Erlang, Haskell and Elixir write one function as several
        same-named clauses. Only a clause that directly follows the
        previous one merges: a symbol in between means a new function.
        """
        last = self._last_fn
        if not self.spec.merge_clauses or last is None:
            return False
        if kind not in _CALLABLE or last[0] != qualname:
            return False
        if last[2] != len(self.symbols):
            return False

        sym = last[1]
        sym.end_line = _last_line(end)
        self.spans.append((node.start_byte, end.end_byte, sym))
        return True

    def _symbol(
        self,
        node: Node,
        name_node: Node | None,
        kind: str,
        containers: _Containers,
        fixed: str | None = None,
    ) -> _Containers | None:
        """Record a definition.

        Returns:
            The container path the definition opens for what it
            contains, or ``None`` when its name is not usable and
            nothing was recorded.
        """
        raw = self._written_name(name_node, fixed)
        parts = self._name_parts(raw) if raw else []
        if not parts:
            return None

        name = parts[-1]
        scope = (*containers, *parts[:-1])
        qualname = ".".join([*scope, name])
        if kind == FUNCTION and scope:
            kind = "method"
        end = self._span_end(node)
        if not self._merge_clause(qualname, kind, node, end):
            self._record(node, end, name, qualname, kind)

        return (*scope, name)

    def _record(
        self, node: Node, end: Node, name: str, qualname: str, kind: str
    ) -> None:
        sym_id = f"{self.rel}::{qualname}"
        count = self._seen.get(sym_id, 0)
        self._seen[sym_id] = count + 1
        if count:
            sym_id = f"{sym_id}#{count + 1}"

        params_node = node.child_by_field_name("parameters")
        ret = node.child_by_field_name(
            "return_type",
        ) or node.child_by_field_name(
            "result",
        )
        sym = Symbol(
            id=sym_id,
            name=name,
            qualname=qualname,
            kind=kind,
            path=self.rel,
            language=self.grammar,
            params=(
                _params_generic(params_node) if params_node is not None else []
            ),
            returns=_text(ret) if ret is not None else None,
            start_line=node.start_point[0] + 1,
            end_line=_last_line(end),
            doc=_doc_comment_above(node),
        )
        self.symbols.append(sym)
        self.spans.append((node.start_byte, end.end_byte, sym))
        if kind in _CALLABLE:
            self._last_fn = (qualname, sym, len(self.symbols))

    # -- definitions by node type -------------------------------------

    def _definition(self, node: Node, containers: _Containers) -> _Outcome:
        """Try each node-type rule of the row on a node, in order.

        A rule whose name path does not resolve falls through to the
        next, so one node type can be listed under two rules.
        """
        spec = self.spec
        # Before anything else: a parameter list must be skipped even
        # when the definition around it records normally.
        for path in spec.skip_paths.get(node.type, ()):
            target = walk(node, path)
            if target is not None:
                self.skipped.add(target.id)

        guard = spec.guard.get(node.type)
        if guard is not None and walk(node, guard) is None:
            return None, False

        for rule in (
            self._function_rule,
            self._type_rule,
            self._bound_rule,
            self._scope_rule,
        ):
            outcome = rule(node, containers)
            if outcome is not None:
                return outcome

        return None, False

    def _function_rule(
        self, node: Node, containers: _Containers
    ) -> _Outcome | None:
        path = self.spec.functions.get(node.type)
        if path is None:
            return None
        if path.startswith("="):
            self._symbol(node, None, FUNCTION, containers, fixed=path[1:])
            return None, True

        name_node = self._name_node(node, path)
        if name_node is None:
            return None

        self._consume_path(node, name_node)
        self._symbol(node, name_node, FUNCTION, containers)
        return None, True

    def _type_rule(
        self, node: Node, containers: _Containers
    ) -> _Outcome | None:
        if node.type not in self.spec.types:
            return None

        kind, path = self.spec.types[node.type]
        if path.startswith("*"):
            self._each_type(node, kind, path[1:], containers)
            return None, True

        name_node = self._name_node(node, path)
        if name_node is None:
            return None

        opened = self._symbol(node, name_node, kind, containers)
        return opened, opened is not None

    def _each_type(
        self, node: Node, kind: str, path: str, containers: _Containers
    ) -> None:
        """One node declaring many types: a symbol per matching child."""
        first, _, rest = path.partition(">")
        wanted = first[1:].split("|")
        for child in node.named_children:
            if child.type in wanted:
                name_node = walk(child, rest) if rest else child
                self._symbol(child, name_node, kind, containers)

    def _bound_rule(
        self, node: Node, containers: _Containers
    ) -> _Outcome | None:
        if node.type not in self.spec.bound:
            return None

        kind, name_path, value_path, value_types = self.spec.bound[node.type]
        value = walk(node, value_path)
        if value is None or value.type not in value_types:
            return None

        name_node = self._name_node(node, name_path)
        if name_node is None:
            return None

        opened = self._symbol(node, name_node, kind, containers)
        # A bound function is not a container for what its body holds.
        return (opened if kind != FUNCTION else None), opened is not None

    def _scope_rule(
        self, node: Node, containers: _Containers
    ) -> _Outcome | None:
        path = self.spec.scopes.get(node.type)
        if path is None:
            return None

        name_node = self._name_node(node, path)
        if name_node is None:
            return None

        parts = [p for p in _SCOPE_SPLIT.split(_text(name_node)) if p]
        return (*containers, *parts), False

    # -- definitions written as a call or a list ----------------------

    def _form(
        self, node: Node, containers: _Containers, member_of: bool
    ) -> _FormOutcome:
        form = self.spec.form
        if form is None or node.type not in form.nodes:
            return None, False, False

        word, head_node = _head(node)
        if word is None:
            return None, False, False
        if member_of and head_node is not None:
            # A signature inside a protocol or record body.
            self.consumed.add(node.id)
            self._symbol(node, head_node, FUNCTION, containers)
            return None, True, False
        if word in form.bindings:
            self._consume_bindings(
                node, form.bindings[word], head_node is not None
            )

        defined = self._form_function(form, node, word, containers)
        if defined is None:
            defined = self._form_type(form, node, word, containers)
        if defined is not None:
            return defined

        self._form_call(form, node, word, head_node)
        return None, False, False

    def _consume_bindings(
        self, node: Node, position: int, head_named: bool
    ) -> None:
        """Rule a binding or parameter list out as calls.

        ``(let ((x (f))) ...)``: ``(x (f))`` binds ``x``, it does not
        call it. ``(f)`` inside it is still walked and still a call.
        """
        kids = node.named_children
        # An anonymous head is not among the named children.
        idx = position - (0 if head_named else 1)
        if not 0 <= idx < len(kids):
            return

        target = kids[idx]
        if target.type in ("symbol", "sym_lit") and idx + 1 < len(kids):
            # A named ``let``: the bindings follow the loop's name.
            target = kids[idx + 1]
        self.consumed.add(target.id)
        for child in target.named_children:
            self.consumed.add(child.id)

    def _form_function(
        self,
        form: FormSpec,
        node: Node,
        word: str,
        containers: _Containers,
    ) -> _FormOutcome | None:
        for path in form.functions.get(word, ()):
            name_node = self._name_node(node, path)
            if name_node is None:
                continue

            self.consumed.add(node.id)
            self._consume_path(node, name_node)
            self._symbol(node, name_node, FUNCTION, containers)
            after = name_node.next_named_sibling
            if after is not None and _holds_parameters(
                form, node, name_node, after
            ):
                self.skipped.add(after.id)
            return None, True, False

        return None

    def _form_type(
        self,
        form: FormSpec,
        node: Node,
        word: str,
        containers: _Containers,
    ) -> _FormOutcome | None:
        if word not in form.types:
            return None

        kind, path = form.types[word]
        name_node = self._name_node(node, path)
        if name_node is None:
            return None

        self.consumed.add(node.id)
        self._consume_path(node, name_node)
        opened = self._symbol(node, name_node, kind, containers)
        self._consume_data(form, node, form.data.get(word, 0))
        return opened, True, word in form.members

    def _consume_data(self, form: FormSpec, node: Node, depth: int) -> None:
        """Rule a type form's field and parent lists out as calls.

        ``(define-record-type point (make-point x y) point? (x
        point-x))`` calls neither ``make-point`` nor ``x``. Only the
        outer ``depth`` levels are data; a form nested below them (a
        slot's default value) is walked as usual.
        """
        level = [node]
        for _ in range(depth):
            level = [
                child
                for parent in level
                for child in parent.named_children
                if child.type in form.nodes
            ]
            self.consumed.update(child.id for child in level)

    def _form_call(
        self, form: FormSpec, node: Node, word: str, head_node: Node | None
    ) -> None:
        """A form that defines nothing: a call, or a special form."""
        if form.calls and head_node is not None and word not in form.skip:
            if word not in form.functions and word not in form.types:
                self._call(node, head_node)
        elif word in form.skip:
            self.consumed.add(node.id)

    # -- calls ----------------------------------------------------------

    def _is_call(self, node: Node) -> bool:
        spec = self.spec
        if node.type not in spec.calls or node.id in self.consumed:
            return False
        if self._is_inner_curried(node):
            return False

        form = spec.form
        if form is None or node.type not in form.nodes:
            return True

        # A definition word whose name path did not resolve is still
        # not a call to ``def``.
        word, _ = _head(node)
        return word not in form.functions and word not in form.types

    def _is_inner_curried(self, node: Node) -> bool:
        """Whether a call node is the callee of a call of its own type.

        ``f a b`` parses as ``(f a) b``. The outer application is the
        call; the inner one is only how the grammar spells it.
        """
        if node.type not in self.spec.curried:
            return False

        parent = node.parent
        if parent is None or parent.type != node.type:
            return False

        callee = walk(parent, self.spec.calls[node.type])
        return callee is not None and callee.id == node.id

    def _call_at(self, node: Node) -> None:
        path = self.spec.calls[node.type]
        if not path:
            self._call(node, None)
            return

        callee = walk(node, path)
        if callee is not None:
            self._call(node, callee)

    def _call(self, node: Node, callee: Node | None) -> None:
        """Record a call, from its callee node or from its own fields."""
        if callee is not None:
            found = self._callee_from_node(callee)
        else:
            found = self._callee_from_fields(node)
        if found is None:
            return

        text, name, receiver = found
        if self.spec.sigils:
            name = name.lstrip(self.spec.sigils)
        # ``&optional`` and ``&rest`` reach here from Lisp lists.
        if not _CALL_NAME.match(name) or name.startswith("&"):
            return

        self.calls.append((node, text, name, receiver))

    def _callee_from_node(self, callee: Node) -> _Callee | None:
        spec = self.spec
        hops = 0
        while callee.type in spec.unwrap and hops < _MAX_CALLEE_UNWRAP:
            inner = walk(callee, spec.unwrap[callee.type])
            if inner is None:
                break
            callee = inner
            hops += 1

        text = _text(callee).strip()
        if spec.call_split is None:
            return text, text, None

        parts = [p for p in re.split(spec.call_split, text) if p]
        if not parts:
            return None

        name = parts[-1].strip()
        receiver = text[: len(text) - len(parts[-1])].rstrip(".:>-%/ ")
        return text, name, receiver or None

    def _callee_from_fields(self, node: Node) -> _Callee | None:
        found = _call_parts(node)
        if found is None:
            return None

        text, name, receiver = found
        if node.type == "command" and not _SHELL_FUNCTION_NAME.match(name):
            return None

        split = self.spec.call_split
        if split is not None:
            tail = [p for p in re.split(split, name) if p]
            if len(tail) > 1:
                name = tail[-1]

        return text, name, receiver
