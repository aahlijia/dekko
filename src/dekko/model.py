"""Shared data model for the code map: symbols, calls, edges."""

from dataclasses import dataclass, field


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
        kind: One of ``function``, ``method``, ``class``.
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
        test: Whether the defining file is classified as test code
            (path-based; see ``classify.is_test_path``).
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
        doc: First line of the file's module docstring or leading
            comment, or ``None`` (best-effort, per language).
    """

    path: str
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
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
    """

    edges: list[Edge] = field(default_factory=list)
    calls_out: dict[str, list[str]] = field(default_factory=dict)
    calls_in: dict[str, list[str]] = field(default_factory=dict)
    ambiguous: list[tuple[str, str, list[str]]] = field(default_factory=list)
    external: list[ExternalCall] = field(default_factory=list)
