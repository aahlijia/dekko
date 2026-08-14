"""Shared data model for the code map: symbols, calls, edges."""

from dataclasses import dataclass, field

# Type-container ``Symbol.kind`` values — every non-callable, non-
# variable definition a language can produce (classes plus the
# interface/enum/struct/record/trait shapes other languages use for
# the same "named type" concept). Shared by every renderer/summary
# that used to check ``kind == "class"`` alone and would otherwise
# silently undercount once ``extractor.py`` stopped lumping all of
# these into ``"class"`` (see ``extractor._CLASSDEF_KIND``).
TYPE_KINDS = frozenset(
    {"class", "interface", "enum", "struct", "record", "trait"}
)


@dataclass
class Param:
    """A single function parameter."""

    name: str
    type: str | None = None


@dataclass
class Symbol:
    """A function, method, or class definition found in a file.

    Attributes:
        id: Stable identifier, ``relpath::Qualified.name``.
        name: Bare name of the symbol.
        qualname: Name qualified by its container, e.g. ``Config.load``.
        kind: One of ``function``, ``method``, ``variable`` (module-
            scope ``const``/``let`` exports in JS/TS; a plain data
            binding, not a callable), or one of ``TYPE_KINDS``
            (``class``, ``interface``, ``enum``, ``struct``,
            ``record``, ``trait``) for named-type definitions.
        path: Repo-relative POSIX path of the defining file.
        language: Language name from the registry.
        params: Ordered parameters with types when declared.
        returns: Declared return type, or ``None``.
        start_line: 1-based first line of the definition.
        end_line: 1-based last line of the definition.
        decorated: Whether the definition carries a decorator,
            attribute, or annotation (used by ``unused`` to treat
            framework-invoked symbols as roots).
        exported: Whether the language marks the symbol as part of the
            public surface (Rust ``pub``, Java ``public``, JS/TS
            ``export``); language-implicit visibility (Go capitals,
            Python dunders) is derived at analysis time, not here.
        doc: First line of the symbol's docstring or doc comment, or
            ``None`` when none was found (best-effort, per language).
        test: Whether the symbol is classified as test code — either
            because the defining file is (path-based; see
            ``classify.is_test_path``, applied in ``cli.map_repository``)
            or because the extractor found it nested inside a
            language-specific test-only AST container (currently just
            Rust's inline ``mod tests { ... }``; see
            ``extractor._qualify``'s ``in_test_module`` signal). The
            two signals only ever add ``True``, never reset a
            ``True`` back to ``False``.
    """

    id: str
    name: str
    qualname: str
    kind: str
    path: str
    language: str
    params: list[Param] = field(default_factory=list)
    returns: str | None = None
    start_line: int = 0
    end_line: int = 0
    decorated: bool = False
    exported: bool = False
    doc: str | None = None
    test: bool = False


@dataclass
class RawCall:
    """A call expression as written, before resolution.

    Attributes:
        caller_id: Symbol id of the enclosing definition, or ``None``
            for module/top-level calls.
        path: File the call appears in.
        text: Full callee text as written (``mod.func``, ``a::b``).
        name: Base identifier (last path/attribute segment).
        receiver: Leading segment when present (``self``, ``obj``,
            module alias), else ``None``.
        line: 1-based line of the call.
    """

    caller_id: str | None
    path: str
    text: str
    name: str
    receiver: str | None = None
    line: int = 0


@dataclass
class RawRef:
    """A bare identifier used as a value (not invoked), before resolution.

    Structurally mirrors ``RawCall`` so ``resolver.py``'s resolution
    ladder can be reused unmodified, but represents a different kind
    of usage entirely: an object-literal property value, array
    element, call argument, or assignment/declarator right-hand side
    — a function passed *by reference* rather than called. Kept as
    its own type, resolved into its own ``referenced``/
    ``referenced_in``/``referenced_out`` tables (never merged with
    ``calls_in``/``calls_out``), so a caller can always tell "this was
    wired up as a callback" apart from "this was invoked here" (bug
    #2b — a bare-reference callback used to be invisible to
    ``get_callers``/``unused``/fan-in entirely).

    Attributes:
        caller_id: Symbol id of the enclosing definition, or ``None``
            for module/top-level references.
        path: File the reference appears in.
        name: The referenced identifier.
        receiver: Always ``None`` — a raw reference is always a bare
            identifier, never a ``.`` accessor. Present only so the
            call-resolution ladder's shared helpers work unmodified.
        line: 1-based line of the reference.
    """

    caller_id: str | None
    path: str
    name: str
    receiver: str | None = None
    line: int = 0


@dataclass
class Import:
    """A name imported into a file.

    Attributes:
        path: File the import appears in.
        name: Local binding name.
        source: Module/path string the name comes from.
    """

    path: str
    name: str
    source: str


@dataclass
class FileMap:
    """Everything extracted from a single source file.

    Attributes:
        refs: Bare-identifier value references (JS/TS/TSX only as of
            this writing — see ``languages.LanguageSpec.
            reference_query``).
        doc: First line of the file's module docstring or leading
            comment, or ``None`` (best-effort, per language).
    """

    path: str
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    refs: list[RawRef] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    error: str | None = None
    doc: str | None = None


@dataclass
class Edge:
    """A resolved caller → callee relationship.

    Attributes:
        lines: Sorted, deduplicated 1-based call-site lines in the
            caller's file (one edge may have many sites).
    """

    caller: str
    callee: str
    lines: list[int] = field(default_factory=list)


@dataclass
class ExternalCall:
    """A call whose target is outside the repo.

    Attributes:
        caller: Symbol id of the calling definition; module-level
            calls use the ``path::<module>`` convention.
        callee: Callee text as written (``mod.func``, ``Path``).
        lines: Sorted, deduplicated 1-based call-site lines.
    """

    caller: str
    callee: str
    lines: list[int] = field(default_factory=list)


@dataclass
class CallGraph:
    """Resolution results across the whole repo.

    Attributes:
        edges: Deduplicated resolved edges.
        calls_out: Symbol id → sorted callee ids.
        calls_in: Symbol id → sorted caller ids.
        ambiguous: Per caller, the unresolved name and its candidate
            symbol ids.
        external: Calls whose target is outside the repo.
        referenced: Deduplicated resolved bare-reference edges — kept
            structurally separate from ``edges`` (see ``RawRef``); a
            reference is a "wired up as a value" fact, never a "this
            call site invoked it" fact.
        referenced_out: Symbol id → sorted ids it references (but
            does not call) as values.
        referenced_in: Symbol id → sorted ids that reference it (but
            do not call it) as a value.
    """

    edges: list[Edge] = field(default_factory=list)
    calls_out: dict[str, list[str]] = field(default_factory=dict)
    calls_in: dict[str, list[str]] = field(default_factory=dict)
    ambiguous: list[tuple[str, str, list[str]]] = field(default_factory=list)
    external: list[ExternalCall] = field(default_factory=list)
    referenced: list[Edge] = field(default_factory=list)
    referenced_out: dict[str, list[str]] = field(default_factory=dict)
    referenced_in: dict[str, list[str]] = field(default_factory=dict)
