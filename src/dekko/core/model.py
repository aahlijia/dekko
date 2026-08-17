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
        id: Stable identifier, ``relpath::Qualified.name``. When a
            file defines more than one symbol with the same
            ``(path, qualname)`` — an overload set, e.g. Java/C++
            method overloading, or a same-name closure-local helper —
            every id past the first gets a ``#N`` suffix
            (``#2``, ``#3``, ...) in definition order, keeping every
            symbol's id unique within the map even though ``path``
            and ``qualname`` alone can't tell them apart (see
            ``extractor._make_symbol``'s ``seen`` dict). Notes,
            call-graph edges, and every other id-keyed structure key
            off this already-disambiguated id, not off the bare
            ``path::qualname`` form.
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
            ``classify.is_test_path``, applied in
            ``repo_ops.map_repository``)
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
class RawHeritage:
    """A heritage clause (extends/implements/impl-for/embeds) as
    written, before resolution.

    Unlike ``RawCall.caller_id``, ``subtype_id`` is never ``None`` — a
    heritage clause only ever appears attached to a ``TYPE_KINDS``
    definition already extracted as its own ``Symbol`` by the time
    heritage extraction runs (there's no "module-level heritage" the
    way there's a module-level call).

    Attributes:
        subtype_id: Symbol id of the type declaring this clause.
        path: File the clause appears in.
        text: Full supertype text as written (``Base``, ``mod::Trait``,
            ``pkg.Interface``).
        name: Base identifier (last path/attribute segment) — mirrors
            ``RawCall.name`` so the resolver's existing candidate
            ladder (``_pick_candidate``) works unmodified.
        receiver: Leading qualifier segment when present, else
            ``None`` — mirrors ``RawCall.receiver``.
        relation: How the subtype relates to the supertype:
            ``"extends"`` (class/interface extends), ``"implements"``
            (class implements interface), ``"impl"`` (Rust
            ``impl Trait for Type``, Phase 2 — not produced by any
            Phase 1 extractor), or ``"embeds"`` (Go anonymous struct
            field, Phase 2 — not produced by any Phase 1 extractor).
        line: 1-based line of the clause.
    """

    subtype_id: str
    path: str
    text: str
    name: str
    receiver: str | None = None
    relation: str = "extends"
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
    heritage: list[RawHeritage] = field(default_factory=list)
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
class HeritageEdge:
    """A resolved subtype -> supertype relationship.

    Attributes:
        subtype: Symbol id of the type declaring the heritage clause.
        supertype: Symbol id of the resolved base type/interface/trait.
        relation: ``"extends"`` / ``"implements"`` / ``"impl"`` /
            ``"embeds"`` — kept per-edge rather than collapsed into one
            undifferentiated kind, since "what does Foo extend" and
            "what does Foo implement" are different questions for a
            caller in languages that distinguish them (Java, TS), even
            though every relation collapses to the same subtype ->
            supertype graph shape for traversal purposes.
        lines: Sorted, deduplicated 1-based clause-site lines — almost
            always a single entry, kept as a list purely for shape
            parity with ``Edge.lines``.
    """

    subtype: str
    supertype: str
    relation: str
    lines: list[int] = field(default_factory=list)


@dataclass
class ModuleEdge:
    """A resolved file -> file import dependency.

    Attributes:
        importer: Repo-relative path of the importing file.
        imported: Repo-relative path of the imported file.
        names: Local names imported across this edge (deduplicated,
            sorted) — kept for disclosure ("imports X, Y from this
            file"), not used for graph traversal itself.
    """

    importer: str
    imported: str
    names: list[str] = field(default_factory=list)


@dataclass
class ModuleGraph:
    """Resolved file-to-file dependency graph.

    Built by ``resolver.resolve_imports()`` from every ``FileMap``'s
    ``imports`` — a separate resolution pass from the symbol-level
    call/reference/heritage graph, keyed on file paths rather than
    symbol ids (an import statement names a module/file, not a
    callable or type).

    Attributes:
        edges: Deduplicated resolved import edges.
        deps_out: File path -> sorted paths it imports.
        deps_in: File path -> sorted paths that import it.
        external: Per-file, the raw source strings that did not
            resolve to an in-repo file (stdlib/third-party/framework
            imports) — kept for disclosure, not traversal.
    """

    edges: list[ModuleEdge] = field(default_factory=list)
    deps_out: dict[str, list[str]] = field(default_factory=dict)
    deps_in: dict[str, list[str]] = field(default_factory=dict)
    external: dict[str, list[str]] = field(default_factory=dict)


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
        heritage: Deduplicated resolved subtype -> supertype edges
            (see ``RawHeritage``/``HeritageEdge``) — extends/implements/
            impl-for/embeds clauses, kept structurally separate from
            ``edges`` the same way ``referenced`` is: a heritage clause
            is neither a call nor a value reference.
        heritage_out: Symbol id (subtype) → sorted supertype ids.
        heritage_in: Symbol id (supertype) → sorted subtype ids.
        heritage_ambiguous: Per subtype, the unresolved supertype name
            and its candidate symbol ids — same ``(subtype, name,
            candidates)`` shape as ``ambiguous``.
        heritage_external: Heritage clauses whose supertype is outside
            the repo (``ExternalCall.caller`` = subtype id,
            ``.callee`` = the raw supertype text) — reused verbatim
            rather than a new type, since the shape fits exactly and a
            class extending a framework base class is a common,
            expected case here (unlike ``external``'s "large but
            usually uninteresting" role for calls).
        modules: Resolved file-to-file import dependency graph (see
            ``ModuleGraph``) — a distinct resolution domain from
            everything else on this dataclass (file paths, not symbol
            ids), attached here purely so every resolved-graph
            structure lives on the one object ``resolve()`` already
            returns, mirroring how ``resolve_refs()``/
            ``resolve_heritage()``'s results land on this same object
            rather than being threaded through every caller
            separately.
    """

    edges: list[Edge] = field(default_factory=list)
    calls_out: dict[str, list[str]] = field(default_factory=dict)
    calls_in: dict[str, list[str]] = field(default_factory=dict)
    ambiguous: list[tuple[str, str, list[str]]] = field(default_factory=list)
    external: list[ExternalCall] = field(default_factory=list)
    referenced: list[Edge] = field(default_factory=list)
    referenced_out: dict[str, list[str]] = field(default_factory=dict)
    referenced_in: dict[str, list[str]] = field(default_factory=dict)
    heritage: list[HeritageEdge] = field(default_factory=list)
    heritage_out: dict[str, list[str]] = field(default_factory=dict)
    heritage_in: dict[str, list[str]] = field(default_factory=dict)
    heritage_ambiguous: list[tuple[str, str, list[str]]] = field(
        default_factory=list
    )
    heritage_external: list[ExternalCall] = field(default_factory=list)
    modules: ModuleGraph = field(default_factory=ModuleGraph)
