"""Tier-2 rows: what a definition and a call look like, per grammar.

A Tier-2 language gets one ``Tier2Spec``: which node types define a
function or a type, where the name sits, and which node types are
calls. Nothing is inferred from how a node type is spelled. A grammar
without a row is not a Tier-2 language, because a rule loose enough to
cover an unknown grammar also reads PHP parameters as classes and R
functions as all being named ``function``.

A row reaches a node by a path, written as ``>``-joined steps, with
``,`` between alternatives tried in order:

==========  ======================================================
``name``    the child in field ``name``
``:a|b``    the first named child of type ``a`` or ``b``
``#0``      a named child by position (``#-1`` is the last)
``^``       the parent
``<``       the previous named sibling
``x=a|b``   step ``x``, and it must land on type ``a`` or ``b``
``=text``   (a whole path) the fixed name ``text``
``*:a>p``   (a type path) every child of type ``a``, each named
            by path ``p``
``p?q~w``   path ``p``, taken only when path ``q`` reads ``w``
==========  ======================================================

A row is admitted on a measurement, not on reading the grammar:
``benchmarks/tier2_corpus.py`` runs it over a real repository and
compares the names it yields with a per-language line regex.
"""

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

from tree_sitter import Node

FUNCTION = "function"


@dataclass(frozen=True)
class FormSpec:
    """Definitions written as a call or a list whose head is a word.

    Elixir's ``def``, a Lisp's ``(defn name ...)``, Tcl's ``method``:
    the grammar gives these the same node type as any other call, so
    the head word decides.

    Attributes:
        nodes: Node types that are such a form.
        functions: Head word to the name paths to try, in order.
        types: Head word to ``(kind, name path)``.
        members: Type head words whose child forms are its methods
            (a Clojure protocol's signatures).
        calls: Whether any other word-headed form is a call named by
            its head.
        skip: Head words that are neither a definition nor a call.
        bindings: Head word to the position of the child that holds a
            binding or parameter list. Lists inside it are not calls.
        data: Type head word to how many levels of its child forms are
            field and parent lists rather than code. ``(defclass c
            (base) ((slot :initarg :slot)))`` calls neither ``base``
            nor ``slot``. Forms nested deeper (a slot's default value)
            are still read.
        params: Node types that are a parameter list when one directly
            follows a function's name. Not descended into. A form of
            one of ``nodes`` in that position is one already.
    """

    nodes: tuple[str, ...]
    functions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    types: dict[str, tuple[str, str]] = field(default_factory=dict)
    members: frozenset[str] = frozenset()
    calls: bool = False
    skip: frozenset[str] = frozenset()
    bindings: dict[str, int] = field(default_factory=dict)
    data: dict[str, int] = field(default_factory=dict)
    params: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tier2Spec:
    """How to read one Tier-2 grammar.

    Attributes:
        functions: Node type to the path of its name.
        types: Node type to ``(kind, name path)``.
        bound: Node type to ``(kind, name path, value path, value
            types)``: a name bound to a function value
            (``f = function() ... end``).
        guard: Node type to a path that must resolve for the node to
            be a definition at all.
        scopes: Node type to a name path. The node qualifies what it
            contains and is not a symbol itself.
        calls: Node type to the path of its callee. An empty path
            reads the node's own callee fields.
        form: Definitions written as calls or lists, or ``None``.
        unwrap: Name node type to a path one level deeper
            (``f(x) where T`` to ``f``).
        curried: Call node types that nest as their parent's callee.
            The chain is one call, named by its innermost function.
        body_after: Definition node type to the type of the sibling
            that holds its body, for grammars that put the body next
            to the signature.
        merge_clauses: Whether consecutive same-named clauses are one
            function.
        split: Regex dividing a written name into containers and name,
            or ``None`` to take the name whole.
        call_split: Regex dividing a callee into receiver and name, or
            ``None`` to take it whole. It has to agree with ``split``:
            a Scheme function defined as ``list->set`` is only ever
            reached by a call that is still named ``list->set``.
        opaque: Node types never descended into (quoted data).
        skip_paths: Node type to paths of the subtrees that hold its
            parameters. Nothing in them is a call.
        sigils: Characters stripped from the front of a callee name
            (Perl's ``&f``).
        unquote: Whether names arrive quoted (SQL's ``"users"``).
    """

    functions: dict[str, str] = field(default_factory=dict)
    types: dict[str, tuple[str, str]] = field(default_factory=dict)
    bound: dict[str, tuple[str, str, str, tuple[str, ...]]] = field(
        default_factory=dict
    )
    guard: dict[str, str] = field(default_factory=dict)
    scopes: dict[str, str] = field(default_factory=dict)
    calls: dict[str, str] = field(default_factory=dict)
    form: FormSpec | None = None
    unwrap: dict[str, str] = field(default_factory=dict)
    curried: frozenset[str] = frozenset()
    body_after: dict[str, str] = field(default_factory=dict)
    merge_clauses: bool = False
    split: str | None = r"[.:]+"
    call_split: str | None = r"\.|::|->|:"
    opaque: frozenset[str] = frozenset()
    skip_paths: dict[str, tuple[str, ...]] = field(default_factory=dict)
    sigils: str = ""
    unquote: bool = False


def _text(node: Node) -> str:
    return (node.text or b"").decode("utf-8", "replace").strip()


def _move(cur: Node, step: str) -> Node | None:
    """Take one step of a path, without its ``=type`` assertion."""
    if step == "":
        return cur
    if step == "^":
        return cur.parent
    if step == "<":
        return cur.prev_named_sibling
    if step.startswith("#"):
        kids = cur.named_children
        idx = int(step[1:])
        return kids[idx] if -len(kids) <= idx < len(kids) else None
    if step.startswith(":"):
        wanted = step[1:].split("|")
        return next((c for c in cur.named_children if c.type in wanted), None)

    return cur.child_by_field_name(step)


def step(cur: Node, spec: str) -> Node | None:
    """Take one path step from a node.

    Args:
        cur: The node the step starts on.
        spec: One step, with an optional ``=a|b`` type assertion.

    Returns:
        The node the step lands on, or ``None`` when there is none or
        its type is not one the step asserts.
    """
    move, sep, wanted = spec.partition("=")
    out = _move(cur, move)
    if out is None or not sep:
        return out

    return out if out.type in wanted.split("|") else None


def _follow(node: Node, steps: str) -> Node | None:
    cur: Node | None = node
    for one in steps.split(">"):
        if cur is None:
            return None
        cur = step(cur, one)

    return cur


def walk(node: Node, path: str) -> Node | None:
    """Follow a path from a node.

    Args:
        node: The node the path starts on.
        path: One or more ``,``-separated alternatives. See the module
            docstring for the step syntax.

    Returns:
        The node the first alternative that resolves lands on, or
        ``None`` when none resolves.
    """
    for alt in path.split(","):
        steps, _, cond = alt.strip().partition("?")
        found = _follow(node, steps)
        if found is None:
            continue
        if cond:
            probe_path, _, words = cond.partition("~")
            probe = walk(node, probe_path)
            if probe is None or _text(probe) not in words.split("|"):
                continue

        return found

    return None


def _path_steps(path: str) -> list[str]:
    """Every step of a path, across its alternatives and conditions."""
    if path.startswith("="):
        return []

    steps: list[str] = []
    for alt in path.lstrip("*").split(","):
        main, _, cond = alt.strip().partition("?")
        steps += main.split(">")
        if cond:
            steps += cond.partition("~")[0].split(">")

    return steps


def _path_types(path: str) -> set[str]:
    """Node types a path names in its ``:a|b`` and ``=a|b`` steps."""
    found: set[str] = set()
    for one in _path_steps(path):
        move, _, wanted = one.partition("=")
        if move.startswith(":"):
            found.update(move[1:].split("|"))
        if wanted:
            found.update(wanted.split("|"))

    return found


def _path_fields(path: str) -> set[str]:
    """Field names a path steps through."""
    moves = (one.partition("=")[0] for one in _path_steps(path))
    return {m for m in moves if m and m[0] not in ":#^<"}


def _paths(spec: Tier2Spec) -> list[str]:
    """Every path a row holds, its form's included."""
    paths: list[str] = []
    for table in (spec.functions, spec.guard, spec.scopes, spec.unwrap):
        paths.extend(table.values())
    paths.extend(p for p in spec.calls.values() if p)
    paths.extend(path for _, path in spec.types.values())
    for _, name, value, _ in spec.bound.values():
        paths += [name, value]
    for skipped in spec.skip_paths.values():
        paths.extend(skipped)
    if spec.form is not None:
        for alternatives in spec.form.functions.values():
            paths.extend(alternatives)
        paths.extend(path for _, path in spec.form.types.values())

    return paths


def named_node_types(spec: Tier2Spec) -> set[str]:
    """Every grammar node type a row names.

    A grammar update that renames one of these turns the row into
    dead weight without an error, so the test suite checks each name
    against the grammar's own node kinds.

    Args:
        spec: The row to read.

    Returns:
        The node types named as a rule's key or inside one of its
        paths.
    """
    found: set[str] = set()
    for table in (
        spec.functions,
        spec.types,
        spec.bound,
        spec.guard,
        spec.scopes,
        spec.calls,
        spec.unwrap,
        spec.skip_paths,
    ):
        found.update(table)
    for _, _, _, value_types in spec.bound.values():
        found.update(value_types)
    for node_type, sibling in spec.body_after.items():
        found |= {node_type, sibling}
    found |= spec.curried | spec.opaque
    if spec.form is not None:
        found |= set(spec.form.nodes) | set(spec.form.params)
    for path in _paths(spec):
        found |= _path_types(path)

    return found


def named_fields(spec: Tier2Spec) -> set[str]:
    """Every grammar field name a row's paths step through.

    Args:
        spec: The row to read.

    Returns:
        The field names, for the same check ``named_node_types``
        serves.
    """
    found: set[str] = set()
    for path in _paths(spec):
        found |= _path_fields(path)

    return found


def canonical(value: Any) -> Any:
    """Reduce a row to a value whose ``repr`` is the same every run.

    A ``frozenset`` of strings iterates in hash order, which changes
    between processes, so a fingerprint built from a row's plain
    ``repr`` would differ on every invocation and throw the
    extraction cache away each time.

    Args:
        value: A row, or any value nested in one.

    Returns:
        The same content with sets and dicts as sorted tuples.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return tuple(
            (f.name, canonical(getattr(value, f.name))) for f in fields(value)
        )
    if isinstance(value, dict):
        return tuple(sorted((k, canonical(v)) for k, v in value.items()))
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(value))
    if isinstance(value, (tuple, list)):
        return tuple(canonical(v) for v in value)

    return value


# Lisp head words that open a form which is not a call.
_LISP_SPECIAL = frozenset(
    "quote quasiquote unquote function if cond case when unless and or "
    "not begin progn do else t otherwise declare declaim setq setf set! "
    "in-package provide require eval-when the".split()
)
# Lisp head words whose first argument is a binding or parameter list.
# ``(let ((x (f))) ...)``: ``(x (f))`` is a binding, not a call to x.
_LISP_BIND = dict.fromkeys(
    "let let* letrec letrec* lambda λ flet labels macrolet let-values "
    "let*-values destructuring-bind multiple-value-bind dolist dotimes "
    "with-slots do do* fluid-let parameterize when-let if-let when-let* "
    "if-let* pcase-let pcase-let* cl-flet cl-labels "
    "cl-destructuring-bind define-values receive case-lambda "
    "named-lambda".split(),
    1,
)
_LISP_SKIP = _LISP_SPECIAL | frozenset(_LISP_BIND)

_BASH = Tier2Spec(
    functions={"function_definition": "name"},
    calls={"command": ""},
    split=None,
    call_split=None,
)

_SCHEME = Tier2Spec(
    form=FormSpec(
        nodes=("list",),
        functions={
            "define": (
                "#1=list>#0=symbol",
                # ``(define f (lambda ...))``
                "#1=symbol?#2=list>#0=symbol~lambda|case-lambda"
                "|λ|named-lambda",
            ),
            "define*": ("#1=list>#0=symbol",),
            "define-syntax": ("#1=symbol",),
            "define-syntax-rule": ("#1=list>#0=symbol",),
            "define-macro": ("#1=list>#0=symbol",),
            "define/contract": ("#1=list>#0=symbol",),
            "define-method": ("#1=list>#0=symbol",),
        },
        types={
            "define-record-type": ("struct", "#1=symbol,#1=list>#0=symbol"),
            "struct": ("struct", "#1=symbol"),
            "define-struct": ("struct", "#1=symbol,#1=list>#0"),
        },
        data={"define-record-type": 1, "struct": 1, "define-struct": 1},
        calls=True,
        skip=_LISP_SKIP
        | {
            "define-library",
            "import",
            "export",
            "include",
            "syntax-rules",
            "module",
            "provide",
            "define-values",
            "cond-expand",
            "library",
            "use-modules",
            "define-module",
        },
        bindings=_LISP_BIND,
    ),
    opaque=frozenset({"quote"}),
    split=None,
    call_split=None,
)

TIER2_SPECS: dict[str, Tier2Spec] = {
    "ruby": Tier2Spec(
        functions={"method": "name", "singleton_method": "name"},
        types={"class": ("class", "name"), "module": ("class", "name")},
        calls={"call": ""},
    ),
    "php": Tier2Spec(
        functions={
            "function_definition": "name",
            "method_declaration": "name",
        },
        types={
            "class_declaration": ("class", "name"),
            "interface_declaration": ("interface", "name"),
            "trait_declaration": ("trait", "name"),
            "enum_declaration": ("class", "name"),
        },
        calls={
            "member_call_expression": "",
            "function_call_expression": "",
            "scoped_call_expression": "",
            "nullsafe_member_call_expression": "",
        },
        split=None,
    ),
    "csharp": Tier2Spec(
        functions={
            "method_declaration": "name",
            "constructor_declaration": "name",
            "local_function_statement": "name",
        },
        types={
            "class_declaration": ("class", "name"),
            "interface_declaration": ("interface", "name"),
            "struct_declaration": ("struct", "name"),
            "record_declaration": ("class", "name"),
            "enum_declaration": ("class", "name"),
            "namespace_declaration": ("class", "name"),
        },
        calls={"invocation_expression": ""},
    ),
    "swift": Tier2Spec(
        functions={
            "function_declaration": "name",
            "protocol_function_declaration": "name",
        },
        types={
            "class_declaration": ("class", "name"),
            "protocol_declaration": ("interface", "name"),
        },
        calls={"call_expression": "", "macro_invocation": ""},
    ),
    "scala": Tier2Spec(
        functions={
            "function_definition": "name",
            "function_declaration": "name",
        },
        types={
            "class_definition": ("class", "name"),
            "object_definition": ("class", "name"),
            "trait_definition": ("trait", "name"),
            "enum_definition": ("class", "name"),
            "package_object": ("class", "name"),
        },
        calls={"call_expression": ""},
    ),
    "lua": Tier2Spec(
        functions={"function_declaration": "name"},
        bound={
            "assignment_statement": (
                FUNCTION,
                ":variable_list>name",
                ":expression_list>value",
                ("function_definition",),
            ),
            "field": (FUNCTION, "name", "value", ("function_definition",)),
        },
        calls={"function_call": ""},
    ),
    "perl": Tier2Spec(
        functions={"subroutine_declaration_statement": "name"},
        types={"package_statement": ("class", "name")},
        calls={
            "method_call_expression": "",
            "func1op_call_expression": "",
            "ambiguous_function_call_expression": "",
            "function_call_expression": "",
            "func0op_call_expression": "",
            "coderef_call_expression": "",
        },
        split=r"::",
        sigils="&",
    ),
    "r": Tier2Spec(
        bound={
            "binary_operator": (
                FUNCTION,
                "lhs=identifier|string",
                "rhs",
                ("function_definition",),
            )
        },
        calls={"call": ""},
        # ``is.na`` is one name. Only ``pkg::f`` has a qualifier.
        split=None,
        call_split=r":::?",
    ),
    "julia": Tier2Spec(
        functions={
            "function_definition": ":signature>#0",
            "macro_definition": ":signature>#0",
            # ``f(x) = ...``, with or without ``where`` or a return type.
            "assignment": (
                "#0=call_expression>#0,"
                "#0=where_expression>#0=call_expression>#0,"
                "#0=typed_expression>#0=call_expression>#0"
            ),
        },
        types={
            "struct_definition": ("struct", ":type_head>#0"),
            "abstract_definition": ("class", ":type_head>#0"),
            "module_definition": ("class", "name"),
        },
        unwrap={
            "typed_expression": "#0",
            "where_expression": "#0",
            "call_expression": "#0",
            "field_expression": "#-1",
            "binary_expression": "#0",
            "parametrized_type_expression": "#0",
            "type_head": "#0",
        },
        calls={
            "call_expression": "#0",
            "macrocall_expression": "",
            "broadcast_call_expression": "#0",
        },
    ),
    "dart": Tier2Spec(
        functions={
            "function_signature": "name",
            "constructor_signature": "name",
            "getter_signature": "name",
            "setter_signature": "name",
        },
        types={
            "class_definition": ("class", "name"),
            "mixin_declaration": ("class", ":identifier"),
            "extension_declaration": ("class", "name"),
            "enum_declaration": ("class", "name"),
        },
        body_after={
            "function_signature": "function_body",
            "constructor_signature": "function_body",
            "getter_signature": "function_body",
            "setter_signature": "function_body",
        },
        # No call node: a call is an argument list after a name.
        calls={"argument_part": "^><=identifier|selector"},
        unwrap={
            "selector": ":unconditional_assignable_selector"
            "|conditional_assignable_selector>:identifier"
        },
    ),
    "zig": Tier2Spec(
        functions={"FnProto": "function"},
        # ``const T = struct { ... };``
        bound={
            "VarDecl": (
                "struct",
                "variable_type_function",
                ":ErrorUnionExpr>:SuffixExpr>:ContainerDecl",
                ("ContainerDecl",),
            )
        },
        body_after={"FnProto": "Block"},
        calls={"FnCallArguments": "<=IDENTIFIER"},
    ),
    "haskell": Tier2Spec(
        functions={"function": "name", "bind": "name=variable"},
        types={
            "data_type": ("class", "name"),
            "newtype": ("class", "name"),
            "class": ("class", "name"),
            # The grammar's own spelling.
            "type_synomym": ("class", "name"),
        },
        scopes={"instance": "name"},
        unwrap={"apply": "function"},
        # A zero-argument binding is a function only at declaration
        # level. Inside ``where`` or ``let`` it is a local value.
        guard={
            "bind": "^=declarations|instance_declarations|class_declarations"
        },
        calls={"apply": "function"},
        curried=frozenset({"apply"}),
        merge_clauses=True,
    ),
    "elixir": Tier2Spec(
        form=FormSpec(
            nodes=("call",),
            functions=dict.fromkeys(
                (
                    "def",
                    "defp",
                    "defmacro",
                    "defmacrop",
                    "defguard",
                    "defguardp",
                    "defdelegate",
                ),
                (":arguments>#0",),
            ),
            types={
                "defmodule": ("class", ":arguments>#0=alias"),
                "defprotocol": ("interface", ":arguments>#0=alias"),
            },
        ),
        unwrap={"call": "target", "binary_operator": "left"},
        calls={"call": ""},
        merge_clauses=True,
    ),
    "erlang": Tier2Spec(
        functions={"function_clause": "name"},
        types={"module_attribute": ("class", "name")},
        calls={"call": ""},
        merge_clauses=True,
    ),
    "ocaml": Tier2Spec(
        functions={"let_binding": "pattern=value_name"},
        # ``let x = 1`` is a value. A function takes a parameter or
        # binds a ``fun``.
        guard={
            "let_binding": (
                ":parameter,body=fun_expression|function_expression"
            )
        },
        types={
            "module_binding": ("class", ":module_name"),
            "module_type_definition": ("interface", ":module_type_name"),
        },
        calls={"application_expression": "function"},
    ),
    "fsharp": Tier2Spec(
        functions={
            "function_or_value_defn": (
                ":function_declaration_left>:identifier"
            ),
            "method_or_prop_defn": "name>method,name>#-1",
        },
        types={
            "named_module": ("class", "name"),
            "module_defn": ("class", ":identifier"),
            "anon_type_defn": ("class", ":type_name>type_name"),
            "record_type_defn": ("class", ":type_name>type_name"),
            "union_type_defn": ("class", ":type_name>type_name"),
            "enum_type_defn": ("class", ":type_name>type_name"),
            "interface_type_defn": ("interface", ":type_name>type_name"),
            "type_abbrev_defn": ("class", ":type_name>type_name"),
        },
        scopes={"namespace": "name"},
        calls={"application_expression": "#0"},
        curried=frozenset({"application_expression"}),
        unwrap={"application_expression": "#0"},
    ),
    "elm": Tier2Spec(
        functions={
            "value_declaration": (
                "functionDeclarationLeft>:lower_case_identifier"
            )
        },
        types={
            "type_declaration": ("class", "name"),
            "type_alias_declaration": ("class", "name"),
        },
        # Top-level values, and anything that takes arguments. A
        # ``let``-bound value is a local.
        guard={"value_declaration": "functionDeclarationLeft>pattern,^=file"},
        calls={"function_call_expr": "target"},
    ),
    "clojure": Tier2Spec(
        form=FormSpec(
            nodes=("list_lit",),
            functions=dict.fromkeys(
                (
                    "defn",
                    "defn-",
                    "defmacro",
                    "defmulti",
                    "defmethod",
                    "deftest",
                ),
                ("#1=sym_lit",),
            ),
            types={
                "defprotocol": ("interface", "#1=sym_lit"),
                "definterface": ("interface", "#1=sym_lit"),
                "defrecord": ("class", "#1=sym_lit"),
                "deftype": ("class", "#1=sym_lit"),
            },
            members=frozenset(
                {"defprotocol", "defrecord", "deftype", "definterface"}
            ),
            calls=True,
            skip=frozenset(
                "ns quote catch finally recur var def declare in-ns "
                "comment let fn if do loop try throw new set! . letfn "
                "when cond case and or not".split()
            ),
        ),
        unwrap={"sym_lit": "name"},
        opaque=frozenset({"quoting_lit"}),
        split=None,
        call_split=r"/",
    ),
    "commonlisp": Tier2Spec(
        functions={"defun": ":defun_header>function_name=sym_lit|package_lit"},
        form=FormSpec(
            nodes=("list_lit",),
            types={
                "defclass": ("class", "#1=sym_lit"),
                "defstruct": ("struct", "#1=sym_lit,#1=list_lit>#0"),
                "define-condition": ("class", "#1=sym_lit"),
            },
            data={"defclass": 2, "define-condition": 2, "defstruct": 1},
            calls=True,
            skip=_LISP_SKIP
            | {
                "defvar",
                "defparameter",
                "defconstant",
                "defpackage",
                "defgeneric",
                "lambda",
            },
            bindings=_LISP_BIND,
        ),
        unwrap={"package_lit": "symbol"},
        opaque=frozenset({"quoting_lit"}),
        skip_paths={"defun": (":defun_header>lambda_list",)},
        # ``pkg:f`` and ``pkg::f`` name ``f``.
        split=None,
        call_split=r":+",
    ),
    "elisp": Tier2Spec(
        functions={
            "function_definition": "name",
            "macro_definition": "name",
        },
        form=FormSpec(
            nodes=("list", "special_form"),
            functions=dict.fromkeys(
                (
                    "cl-defun",
                    "cl-defmethod",
                    "cl-defgeneric",
                    "cl-defmacro",
                    "cl-defsubst",
                    "define-inline",
                    "define-minor-mode",
                    "define-derived-mode",
                    "define-globalized-minor-mode",
                    "transient-define-prefix",
                    "transient-define-suffix",
                    "transient-define-infix",
                    "transient-define-argument",
                ),
                ("#1=symbol",),
            ),
            types={
                "defclass": ("class", "#1=symbol"),
                "cl-defstruct": ("struct", "#1=symbol,#1=list>#0"),
            },
            data={"defclass": 2, "cl-defstruct": 1},
            calls=True,
            skip=_LISP_SKIP
            | {
                "defvar",
                "defconst",
                "defcustom",
                "defface",
                "defgroup",
                "defvar-local",
                "defvaralias",
                "lambda",
                "interactive",
                "declare-function",
                "defalias",
                "put",
                "while",
                "prog1",
                "prog2",
                "save-excursion",
                "condition-case",
            },
            bindings=_LISP_BIND,
        ),
        opaque=frozenset({"quote"}),
        skip_paths={
            "function_definition": ("parameters",),
            "macro_definition": ("parameters",),
        },
        split=None,
        call_split=None,
    ),
    "scheme": _SCHEME,
    "racket": _SCHEME,
    "cmake": Tier2Spec(
        functions={
            "function_def": ":function_command>:argument_list>#0>#0",
            "macro_def": ":macro_command>:argument_list>#0>#0",
        },
        calls={"normal_command": ":identifier"},
        split=None,
    ),
    "nix": Tier2Spec(
        # ``name = args: body;``
        bound={
            "binding": (
                FUNCTION,
                "attrpath",
                "expression",
                ("function_expression",),
            )
        },
        calls={"apply_expression": "function"},
        curried=frozenset({"apply_expression"}),
        unwrap={"apply_expression": "function"},
    ),
    "d": Tier2Spec(
        functions={"function_declaration": ":identifier"},
        types={
            "class_declaration": ("class", ":identifier"),
            "struct_declaration": ("struct", ":identifier"),
            "interface_declaration": ("interface", ":identifier"),
            "template_declaration": ("class", ":identifier"),
            "union_declaration": ("struct", ":identifier"),
        },
        calls={"call_expression": ""},
    ),
    "odin": Tier2Spec(
        functions={
            "procedure_declaration": ":identifier",
            "overloaded_procedure_declaration": ":identifier",
        },
        types={
            "struct_declaration": ("struct", ":identifier"),
            "enum_declaration": ("class", ":identifier"),
            "union_declaration": ("struct", ":identifier"),
        },
        calls={"call_expression": "function", "selector_call_expression": ""},
    ),
    "nim": Tier2Spec(
        functions={"routine": ":symbol>:ident"},
        # One ``type`` section declares many types.
        types={"typeDef": ("class", "*:symbol>:ident")},
        calls={"functionCall": "^><", "cmdCall": "^><"},
    ),
    "pascal": Tier2Spec(
        # The implementation only. An interface section's declaration
        # of the same routine would be its twin.
        functions={"defProc": "header>name"},
        types={"declType": ("class", "name")},
        guard={"declType": "type=declClass|declIntf|declHelper"},
        calls={"exprCall": "entity"},
    ),
    "powershell": Tier2Spec(
        functions={
            "function_statement": ":function_name",
            "class_method_definition": ":simple_name",
        },
        types={"class_statement": ("class", ":simple_name")},
        calls={"command": ""},
        split=r":",
    ),
    "sql": Tier2Spec(
        functions={
            "create_function": ":object_reference>name",
            "create_procedure": ":object_reference>name",
        },
        # A table is its ``CREATE TABLE``. Every other statement that
        # mentions it is a use.
        types={"create_table": ("class", ":object_reference>name")},
        calls={"invocation": ""},
        unquote=True,
    ),
    "fortran": Tier2Spec(
        functions={
            "subroutine": ":subroutine_statement>name",
            "function": ":function_statement>name",
        },
        types={
            "module": ("class", ":module_statement>:name"),
            "derived_type_definition": (
                "struct",
                ":derived_type_statement>:type_name",
            ),
        },
        calls={"subroutine_call": "", "call_expression": ""},
        call_split=r"\.|::|->|:|%",
    ),
    "ada": Tier2Spec(
        functions={
            "subprogram_body": (
                ":procedure_specification|function_specification>name"
            ),
            "subprogram_declaration": (
                ":procedure_specification|function_specification>name"
            ),
            "expression_function_declaration": ":function_specification>name",
        },
        types={
            "package_declaration": ("class", "name"),
            "package_body": ("class", "name"),
        },
        calls={"function_call": "name", "procedure_call_statement": "name"},
    ),
    "hare": Tier2Spec(
        functions={"function_declaration": "name"},
        types={"type_declaration": ("struct", ":identifier")},
        calls={"call_expression": ""},
    ),
    "gleam": Tier2Spec(
        functions={"function": "name", "external_function": "name"},
        types={
            "type_definition": ("class", ":type_name>name"),
            "type_alias": ("class", ":type_name>name"),
            "external_type": ("class", ":type_name>name"),
        },
        calls={"function_call": ""},
    ),
    "solidity": Tier2Spec(
        functions={
            "function_definition": "name",
            "modifier_definition": "name",
        },
        types={
            "contract_declaration": ("class", "name"),
            "interface_declaration": ("interface", "name"),
            "library_declaration": ("class", "name"),
            "struct_declaration": ("struct", "name"),
        },
        calls={
            "call_expression": "",
            "yul_function_call": "",
            "modifier_invocation": "",
        },
    ),
    "tcl": Tier2Spec(
        functions={"procedure": "name"},
        form=FormSpec(
            nodes=("command",),
            functions=dict.fromkeys(
                ("method", "proc", "typemethod"),
                ("arguments>#0",),
            ),
            types={
                "oo::class": ("class", "arguments>#1"),
                "snit::type": ("class", "arguments>#0"),
                "snit::widget": ("class", "arguments>#0"),
            },
            # ``method push {v} {...}``: ``{v}`` parses as a command.
            params=("braced_word",),
        ),
        scopes={"namespace": ":word_list>#1"},
        calls={"command": ""},
        split=r"::",
    ),
    "vim": Tier2Spec(
        # The definition, not its ``function_declaration`` header line:
        # the body is the header's sibling.
        functions={"function_definition": ":function_declaration>name"},
        calls={"call_expression": ""},
    ),
    "crystal": Tier2Spec(
        functions={
            "method_def": "name",
            "abstract_method_def": "name",
        },
        types={
            "class_def": ("class", "name"),
            "module_def": ("class", "name"),
            "struct_def": ("struct", "name"),
            "enum_def": ("class", "name"),
        },
        calls={"call": "", "implicit_object_call": "", "assign_call": ""},
        split=r"::",
    ),
    "haxe": Tier2Spec(
        functions={"function_declaration": "name"},
        types={
            "class_declaration": ("class", "name"),
            "interface_declaration": ("interface", "name"),
            "typedef_declaration": ("class", "name"),
            "enum_declaration": ("class", "name"),
            "enum_abstract_declaration": ("class", "name"),
        },
        calls={"call_expression": ""},
    ),
    "gdscript": Tier2Spec(
        functions={
            "function_definition": "name",
            "constructor_definition": "=_init",
        },
        types={
            "class_definition": ("class", "name"),
            "class_name_statement": ("class", "name"),
        },
        calls={"attribute_call": "", "call": ""},
    ),
    "starlark": Tier2Spec(
        functions={"function_definition": "name"},
        calls={"call": ""},
    ),
    "bash": _BASH,
    "zsh": _BASH,
}
