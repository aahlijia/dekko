"""Language registry: extensions, grammars, and tree-sitter queries.

Tier-1 languages get dedicated queries with full parameter/return-type
fidelity. Tier-2 languages (everything else in the language pack) are
handled by the generic fallback extractor and only need a grammar name.
"""

import hashlib
from dataclasses import dataclass, field, fields


@dataclass(frozen=True)
class LanguageSpec:
    """Static description of how to extract symbols for one language.

    Attributes:
        name: Registry name, also used in output.
        grammar: Grammar name for ``tree-sitter-language-pack``.
        extensions: File extensions (with dot) mapped to this language.
        definition_query: Query capturing ``@def``/``@name``/``@params``
            /``@ret`` for callables and ``@classdef``/``@classname``
            for type containers.
        call_query: Query capturing ``@call``/``@callee``.
        import_query: Query for import statements, or ``None``.
        container_types: Node type → name-field for ancestors that
            qualify a definition's name (classes, impls, namespaces).
        method_containers: Subset of ``container_types`` that make a
            contained function a method (classes/impls, not modules).
        param_style: Dispatch key for parameter-list parsing.
        function_boundary_types: Function/method/closure node types
            that stop the container-qualification climb in
            ``extractor._qualify``. A definition nested inside another
            function/method/closure is a closure-local helper, never a
            member of whatever contains that outer function — without
            this, a local helper defined inside a method climbs past
            it to the enclosing class and is mislabeled as one of that
            class's members. Empty for languages whose ``container_
            types`` never register a function-like node type, so
            there is nothing to accidentally climb through (Python,
            C, C++, Go, Java as of this writing).
        reference_query: Query capturing non-call usage edges — a
            single ``@ref`` capture per match, fed into the same
            ``referenced_in``/``referenced_out`` pipeline
            ``call_query`` feeds. Three distinct shapes so far: bare
            identifiers used as *values* rather than invoked —
            object-literal property values, array elements, call
            arguments, assignment/declarator right-hand sides, and (for
            JS/TS/TSX) JSX attribute values and JSX element tag names —
            which a plain call-expression query structurally cannot see
            (a callback passed by reference, or a component used only
            as ``<Foo />``, is never a call site); and
            type-position identifiers — parameter/return/variable/
            const declaration types and composite-literal types (Go's
            struct/interface usage) — which a
            definition/call query never visits at all since they name
            a *type*, not a callable or a value; and (Java) method
            references (``this::method``/``Class::method``), which
            are call-shaped nowhere in the syntax
            tree (no argument list) and so are invisible to
            ``call_query`` too. ``None`` for languages still lacking
            one (Rust, C, C++ as of this writing).
        enum_variant_query: Query capturing an enum's *tuple* variants
            (``@enum`` the owning enum's name, ``@variant`` the
            variant's), Rust only. Variants are not symbols. They are a
            repo-wide name registry the resolver consults before
            taking ``Left(x)`` for a construction of ``struct Left``:
            with ``enum Side { Left(u8) }`` anywhere in the repo that
            call has two readings and the map only indexes one of
            them. Unit and struct-like variants are left
            out: neither can be written ``Name(..)``.
        binding_query: Query locating the places a *local* name is
            bound, so ``extractor._collect_refs`` can tell a bare
            identifier that names a repo symbol from one that names a
            parameter or a local of the same spelling. The capture
            name says where the binding lives:
            ``@params`` (a parameter list, binds in its parent),
            ``@param`` (one parameter pattern, same), ``@local``
            (nearest block or function scope), ``@funclocal`` (nearest
            function scope: JS ``var``), ``@scoped`` with a sibling
            ``@scope`` (binds in exactly that node: a catch clause, a
            ``for..of`` head, a comprehension), ``@defname`` (a nested
            definition's name: recorded only so it *stops* the lookup,
            since the map indexes it as a symbol), and Python's
            ``@global``/``@nonlocal``/``@import``. Destructuring nests
            to any depth and a query can't recurse, so the query finds
            the pattern's root and ``extractor._pattern_identifiers``
            walks it. ``None`` for languages whose reference shapes
            can't be shadowed by a value local (Go's type identifiers,
            Java's ``Type::method``).
        binding_function_scopes: Node types that open a function-level
            scope for ``binding_query``. For Python this includes
            ``class_definition``: a class body binds its own
            statements' names (and is skipped again on lookup from
            inside a method, see ``extractor._CLASS_SCOPES``).
        binding_block_scopes: Node types that open a block-level scope
            (``let``/``const``). Empty for Python, which has none.
        heritage_query: Query capturing a type definition's own
            ``extends``/``implements`` clause(s) — one ``@classdef``
            match per type, with the clause(s) attached as sibling
            captures on the same match rather than a second query over
            the same node (the "container node walked like a param
            list" shape: a whole heritage container — Python's
            ``argument_list``, TS's ``class_heritage``/
            ``extends_type_clause``, Java's ``superclass``/
            ``super_interfaces``/``extends_interfaces`` — is captured
            whole and walked by a dedicated per-language parser in
            ``extractor.py``, mirroring ``_params_*``). Rust is the
            one exception to the "container attached to ``@classdef``"
            shape: ``impl Trait for Type`` is its own top-level
            construct (``@implblock``, no ``@classdef`` at all), so
            ``extractor._collect_heritage`` special-cases it, resolving
            the type side by same-file name lookup instead of span
            correlation (see ``extractor._heritage_rust_impl``). ``None``
            for languages without one yet (Go, deferred). Covers
            Python/JavaScript/TypeScript/TSX/Java and Rust/C++.
        throw_query: Query capturing raise/throw sites (``@throw``) and,
            Java only, a method's declared checked-exception clause
            (``@throws_clause``) — a second, independent signal for
            "what can calling this raise" beyond throw-site scanning
            (see ``extractor._collect_throws``). Exception/error
            handling is not a uniform language feature: this is a deliberately
            scoped pilot covering Python/Java/C++/JS/TS only.
            ``None`` for Rust/Go/C — a **permanent** exclusion, not a
            placeholder awaiting a future pass the way an unset
            ``heritage_query`` slot can be: Rust's ``Result``/``?``
            propagation and Go's returned-``error``-value convention
            are type-inference problems, not tree-sitter-query-able
            syntax, and C has no exception concept at all.
        catch_query: Query capturing except/catch clauses (``@catch``,
            plus Java's ``@catch_type``/C++'s ``@catch_params`` helper
            captures for their container-node shapes — see
            ``extractor._collect_catches``). Same Python/Java/C++/JS/TS
            scope and same permanent Rust/Go/C exclusion as
            ``throw_query``. JS/TS catch clauses are never
            type-discriminated at the syntax level (no runtime type
            check on the caught value), so every JS catch and every
            untyped TS catch extracts as a catch-all (``bare=True``) —
            only a TS ``catch (e: SomeType)`` annotation (rare) yields
            a real type name; a "weak signal" caveat, not a bug,
            disclosed in ``dekko query catches``' own output.
        env_read_query: Query capturing statically-known
            environment-variable read call sites (``@call``, plus
            per-shape helper captures — ``@mod``/``@fn``/``@sub`` for
            Python/Java/Go's attribute-chain calls, ``@proc``/``@env``
            for JS/TS's ``process.env`` member access — see
            ``extractor._collect_env_reads``). Unlike every other
            query field, this targets a small, hand-curated allowlist
            of known "config/env read" call shapes, not a general
            syntactic category — closer in spirit to ``stats.py``'s
            ``_NOISE_NAMES`` than to a broad grammar-driven capture.
            The key argument must
            be a literal string node for a match to occur at all — a
            dynamic key (``os.getenv(some_var)``) or an f-string/
            template-literal key structurally does not match, so no
            filtering-out step is needed for those; identifier-text
            filtering (``@mod`` must actually read ``"os"``, etc.) is
            done in ``extractor.py`` Python code rather than the query
            itself, matching the codebase's established
            capture-broadly-then-filter convention (see
            ``extractor._CLASSDEF_KIND``). Set for every Tier-1
            language (no permanent exclusion the way ``throw_query``/
            ``catch_query`` exclude Rust/Go/C — an env-var read is
            just a call/member-access expression, not a language
            feature some languages structurally lack).
        type_alias_query: Query capturing a type-alias declaration's
            bare name (``@name``) — e.g. TS's ``type X = {...}``,
            which is never extracted as a ``Symbol`` (see
            ``query._heritage_external_label``'s docstring for why
            this matters: an ``implements``/``extends`` clause naming
            a same-file type alias has no other structural signal to
            distinguish it from a genuinely external base type).
            ``None`` for languages
            without one (only TS/TSX have this construct as of this
            writing — plain JS has no ``type`` keyword at all).
        type_use_query: Query capturing every parameter-list node
            (``@params``) so ``extractor._collect_type_uses`` can
            record the parameter/return annotations of the
            function-shaped nodes ``definition_query`` does not name
            (see ``model.TypeUse``). ``None`` for languages whose
            parameter lists carry no annotations the read side could
            match (plain JS) or that have no measurement yet.
    """

    name: str
    grammar: str
    extensions: tuple[str, ...]
    definition_query: str
    call_query: str
    import_query: str | None = None
    container_types: dict[str, str] = field(default_factory=dict)
    method_containers: tuple[str, ...] = ()
    param_style: str = "generic"
    function_boundary_types: tuple[str, ...] = ()
    reference_query: str | None = None
    heritage_query: str | None = None
    throw_query: str | None = None
    catch_query: str | None = None
    env_read_query: str | None = None
    type_alias_query: str | None = None
    type_use_query: str | None = None
    enum_variant_query: str | None = None
    binding_query: str | None = None
    binding_function_scopes: tuple[str, ...] = ()
    binding_block_scopes: tuple[str, ...] = ()


# Bare identifiers used as *values* rather than invoked -- keyword-
# argument values, positional call arguments, dict/list/tuple/set
# literal elements, assignment/default-parameter right-hand sides, and
# bare `return` values. Closes the dispatch-table/callback-wiring
# shape a plain call-expression query structurally cannot see (a
# function passed by name and never itself called at that site is not
# a call site) -- mirrors _JS_REFERENCE_BASE's identical shape for
# JS/TS (`check_success=valid_ndk_path` is a call *keyword
# argument*, not a call). Verified against the pinned
# tree-sitter-python grammar.
_PY_REFERENCE_QUERY = """
(keyword_argument value: (identifier) @ref)
(argument_list (identifier) @ref)
(pair value: (identifier) @ref)
(list (identifier) @ref)
(tuple (identifier) @ref)
(set (identifier) @ref)
(assignment right: (identifier) @ref)
(default_parameter value: (identifier) @ref)
(return_statement (identifier) @ref)
"""

# Where Python binds a local name (see
# ``LanguageSpec.binding_query``). Assignment targets that aren't
# names (``self.x = ..``, ``table[k] = ..``) fall out by themselves:
# ``extractor._pattern_identifiers`` only descends into pattern nodes.
# ``global`` makes a name *not* local (recorded as a lookup stop);
# ``nonlocal`` leaves it to the enclosing function's own binding. A
# function-level ``import`` binds an import, not a local, the same
# exemption JS's lazy ``await import()`` gets. Node shapes verified
# against the pinned tree-sitter-python grammar (``with .. as (a, b)``
# is an ``as_pattern_target`` holding a plain ``tuple``, not a
# ``tuple_pattern``).
_PY_BINDING_QUERY = """
(function_definition parameters: (parameters) @params)
(lambda parameters: (lambda_parameters) @params)
(assignment left: (_) @local)
(augmented_assignment left: (_) @local)
(named_expression name: (identifier) @local)
(for_statement left: (_) @local)
(as_pattern alias: (as_pattern_target) @local)
(list_comprehension (for_in_clause left: (_) @scoped)) @scope
(set_comprehension (for_in_clause left: (_) @scoped)) @scope
(dictionary_comprehension (for_in_clause left: (_) @scoped)) @scope
(generator_expression (for_in_clause left: (_) @scoped)) @scope
(function_definition name: (identifier) @defname)
(class_definition name: (identifier) @defname)
(global_statement (identifier) @global)
(nonlocal_statement (identifier) @nonlocal)
(import_statement) @import
(import_from_statement) @import
"""

PYTHON = LanguageSpec(
    name="python",
    grammar="python",
    extensions=(".py", ".pyi"),
    definition_query="""
(function_definition
  name: (identifier) @name
  parameters: (parameters) @params
  return_type: (type)? @ret) @def

(class_definition
  name: (identifier) @classname) @classdef
""",
    call_query="""
(call function: (_) @callee arguments: (_)? @args) @call
""",
    import_query="""
(import_statement
  name: (dotted_name) @module)

(import_statement
  name: (aliased_import
    name: (dotted_name) @module
    alias: (identifier) @alias))

(import_from_statement
  module_name: (_) @from_module
  name: (dotted_name) @name)

(import_from_statement
  module_name: (_) @from_module
  name: (aliased_import
    name: (dotted_name) @name
    alias: (identifier) @alias))
""",
    container_types={"class_definition": "name"},
    method_containers=("class_definition",),
    param_style="python",
    reference_query=_PY_REFERENCE_QUERY,
    binding_query=_PY_BINDING_QUERY,
    binding_function_scopes=(
        "function_definition",
        "lambda",
        "class_definition",
    ),
    heritage_query="""
(class_definition
  name: (identifier) @classname
  superclasses: (argument_list)? @bases) @classdef
""",
    # ``raise_statement``'s raised expression is an unfielded first
    # named child (absent for a bare re-raise) — the same shape as
    # C++'s ``throw_statement``, walked by the shared
    # ``extractor._raise_expr`` helper. ``except_clause``'s optional
    # ``value`` field is an identifier/attribute (single type), a
    # ``tuple`` (multi-catch), or an ``as_pattern`` wrapping either
    # (``except X as e:``) — absent entirely for a bare ``except:``.
    # Verified against the pinned tree-sitter-python grammar.
    throw_query="""
(raise_statement) @throw
""",
    catch_query="""
(except_clause) @catch
""",
    # Three independent shapes, each a separate top-level pattern (one
    # match per shape, disambiguated in ``extractor._env_read_python``
    # by which captures are present — mirrors ``throw_query``'s
    # ``@throw``/``@throws_clause`` dispatch). Every pattern requires
    # the key argument/subscript to be a literal ``(string)`` node —
    # a dynamic key (a bare variable) or an f-string key structurally
    # fails to match the first two shapes at all; an f-string *does*
    # still match (an f-string is also node type ``string``), so
    # ``extractor._string_literal_value`` rejects it by its ``f``/``F``
    # prefix. ``.`` anchors the key to the argument list's first
    # child, so a default-value second argument
    # (``os.getenv("PORT", "8080")``) is never captured. Verified live
    # against the pinned tree-sitter-python grammar.
    env_read_query="""
(call
  function: (attribute
    object: (identifier) @mod
    attribute: (identifier) @fn)
  arguments: (argument_list . (string) @key)) @call

(call
  function: (attribute
    object: (attribute
      object: (identifier) @mod
      attribute: (identifier) @sub)
    attribute: (identifier) @fn)
  arguments: (argument_list . (string) @key)) @call

(subscript
  value: (attribute
    object: (identifier) @mod
    attribute: (identifier) @sub)
  subscript: (string) @key) @call
""",
)

# Tuple variants only: ``Left(u8)``, not ``Mid`` or ``Right { a: u8 }``
# (see ``LanguageSpec.enum_variant_query``). Node shapes verified
# against the pinned tree-sitter-rust grammar.
_RUST_ENUM_VARIANT_QUERY = """
(enum_item
  name: (type_identifier) @enum
  body: (enum_variant_list
    (enum_variant
      name: (identifier) @variant
      body: (ordered_field_declaration_list))))
"""

RUST = LanguageSpec(
    name="rust",
    grammar="rust",
    extensions=(".rs",),
    definition_query="""
(function_item
  name: (identifier) @name
  parameters: (parameters) @params
  return_type: (_)? @ret) @def

(function_signature_item
  name: (identifier) @name
  parameters: (parameters) @params
  return_type: (_)? @ret) @def

(struct_item name: (type_identifier) @classname) @classdef
(enum_item name: (type_identifier) @classname) @classdef
(trait_item name: (type_identifier) @classname) @classdef

(source_file
  (type_item name: (type_identifier) @classname) @classdef)
(mod_item
  body: (declaration_list
    (type_item name: (type_identifier) @classname) @classdef))
""",
    # The two ``type_item`` patterns above are anchored to a file or
    # ``mod`` body on purpose: ``type Alias = Real;`` is
    # a module-level alias worth a ``type_alias`` symbol, but the same
    # node type inside an ``impl`` block is an *associated type*
    # (``type Output = Foo;``), and indexing those would mint thousands
    # of ``Item``/``Output``/``Target`` symbols that name nothing a
    # ``Type::name()`` path could be rooted at.
    call_query="""
(call_expression function: (_) @callee arguments: (_)? @args) @call
""",
    import_query="""
(use_declaration argument: (_) @use)
""",
    container_types={
        "impl_item": "type",
        "trait_item": "name",
        "mod_item": "name",
    },
    method_containers=("impl_item", "trait_item"),
    param_style="rust",
    function_boundary_types=("function_item", "closure_expression"),
    enum_variant_query=_RUST_ENUM_VARIANT_QUERY,
    # ``impl_item`` with a ``trait:`` field is heritage (``impl Trait
    # for Type``); an inherent ``impl Type { ... }`` (no ``trait:``
    # field) never matches this pattern at all, so no query-time
    # filtering is needed to exclude it. ``trait_item``'s optional
    # ``bounds: (trait_bounds)`` field covers supertrait bounds
    # (``trait Sub: Super``), attached to ``@classdef`` the same way
    # the other languages attach their heritage container.
    heritage_query="""
(impl_item
  trait: (_) @impl_trait
  type: (_) @impl_type) @implblock

(trait_item
  name: (type_identifier) @classname
  bounds: (trait_bounds)? @bounds) @classdef
""",
    # ``std::env::var("X")``/``env::var("X")``/``*_os`` variants all
    # parse as ``call_expression`` whose ``function`` is a (possibly
    # nested) ``scoped_identifier`` — captured whole as ``@fn`` rather
    # than matched shape-by-shape, since ``std::env::var`` and bare
    # ``env::var`` differ in nesting depth but both render to plain
    # ``::``-joined text; ``extractor._env_read_rust`` checks that
    # text ends with ``env::var``/``env::var_os`` rather than the
    # query itself branching on depth. ``.`` anchors the key to the
    # first argument. Verified live against the pinned
    # tree-sitter-rust grammar.
    env_read_query="""
(call_expression
  function: (scoped_identifier) @fn
  arguments: (arguments . (string_literal) @key)) @call
""",
)

_C_DEFINITIONS = """
(function_definition
  type: (_) @ret
  declarator: (function_declarator
    declarator: (identifier) @name
    parameters: (parameter_list) @params)) @def

(function_definition
  type: (_) @ret
  declarator: (pointer_declarator
    declarator: (function_declarator
      declarator: (identifier) @name
      parameters: (parameter_list) @params))) @def

(struct_specifier
  name: (type_identifier) @classname
  body: (field_declaration_list)) @classdef
"""

# C/C++'s bare ``getenv("X")`` is a plain ``call_expression`` whose
# function is a bare ``identifier`` — no ``std``/``os``-style
# namespace/attribute chain to walk, unlike every other language's
# shape. ``.`` anchors the key to the first argument; ``extractor.
# _env_read_c_cpp`` still requires ``@fn`` to read exactly
# ``"getenv"`` (not merely contain it — see the
# ``my_getenv_wrapper`` false-positive test). Verified live against
# the pinned tree-sitter-c/tree-sitter-cpp grammars (both accept this
# identical query).
_C_ENV_READ_QUERY = """
(call_expression
  function: (identifier) @fn
  arguments: (argument_list . (string_literal) @key)) @call
"""

C = LanguageSpec(
    name="c",
    grammar="c",
    extensions=(".c", ".h"),
    definition_query=_C_DEFINITIONS,
    call_query="""
(call_expression function: (_) @callee arguments: (_)? @args) @call
""",
    import_query="""
(preproc_include path: (_) @module)
""",
    param_style="c",
    env_read_query=_C_ENV_READ_QUERY,
)

CPP = LanguageSpec(
    name="cpp",
    grammar="cpp",
    extensions=(".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"),
    definition_query="""
(function_definition
  type: (_)? @ret
  declarator: (function_declarator
    declarator: [
      (identifier)
      (field_identifier)
      (qualified_identifier)
      (destructor_name)
    ] @name
    parameters: (parameter_list) @params)) @def

(function_definition
  type: (_)? @ret
  declarator: (pointer_declarator
    declarator: (function_declarator
      declarator: [
        (identifier)
        (field_identifier)
        (qualified_identifier)
      ] @name
      parameters: (parameter_list) @params))) @def

(function_definition
  type: (_)? @ret
  declarator: (reference_declarator
    (function_declarator
      declarator: [
        (identifier)
        (field_identifier)
        (qualified_identifier)
      ] @name
      parameters: (parameter_list) @params))) @def

(struct_specifier
  name: (type_identifier) @classname
  body: (field_declaration_list)) @classdef

(class_specifier
  name: (type_identifier) @classname
  body: (field_declaration_list)) @classdef
""",
    call_query="""
(call_expression function: (_) @callee arguments: (_)? @args) @call
""",
    import_query="""
(preproc_include path: (_) @module)
""",
    container_types={
        "class_specifier": "name",
        "struct_specifier": "name",
        "namespace_definition": "name",
    },
    method_containers=("class_specifier", "struct_specifier"),
    param_style="c",
    # ``base_class_clause``'s named children alternate between an
    # ``access_specifier`` wrapper (``public``/``private``/
    # ``protected`` — absent on a struct base with no explicit
    # specifier) and the actual base type node; the specifier must be
    # stripped before the type name is usable (see
    # ``extractor._heritage_cpp``). The pattern mirrors ``definition_
    # query``'s own ``@classdef`` shape exactly (``body: (field_
    # declaration_list)`` required) so the two queries' ``@classdef``
    # spans line up byte-for-byte for correlation — a forward
    # declaration (``class Foo;``, no body) is excluded from heritage
    # extraction the same way it's already excluded from definitions.
    heritage_query="""
(struct_specifier
  name: (type_identifier) @classname
  (base_class_clause)? @heritage
  body: (field_declaration_list)) @classdef

(class_specifier
  name: (type_identifier) @classname
  (base_class_clause)? @heritage
  body: (field_declaration_list)) @classdef
""",
    # ``throw_statement``'s raised expression is an unfielded first
    # named child, same shape as Python's ``raise_statement`` (see
    # ``extractor._raise_expr``) — absent for a bare ``throw;``
    # re-raise. ``catch_clause``'s ``parameters`` field is a
    # ``parameter_list`` with either a single ``parameter_declaration``
    # (a typed catch, its own ``type``/``declarator`` fields) or zero
    # named children for a catch-all ``catch (...)`` (``...`` parses as
    # an anonymous token, not a named node — confirmed live against the
    # pinned tree-sitter-cpp grammar, so ``named_child_count == 0``
    # reliably means catch-all; C++ has no true empty ``catch ()``).
    throw_query="""
(throw_statement) @throw
""",
    catch_query="""
(catch_clause
  parameters: (parameter_list) @catch_params) @catch
""",
    env_read_query=_C_ENV_READ_QUERY,
)

# Function/method/closure node types shared by JS/TS/TSX's
# ``function_boundary_types`` — a definition nested inside any of
# these is a closure-local helper, never a member of whatever
# contains the outer function (see ``LanguageSpec.
# function_boundary_types``).
_JS_FUNCTION_BOUNDARIES = (
    "function_declaration",
    "generator_function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
)

# Bare-identifier value references shared by JS/TS/TSX: a
# callback wired up by name rather than invoked at that site — an
# object-literal property value, a shorthand property, an array
# element, a bare call argument, an assignment/declarator
# right-hand side, a ``${...}`` template-literal substitution, or
# an operand of a binary expression
# (``x >= 0``, ``a + clearLine``) or a branch/condition of a ternary
# (``cond ? a : b``) — plain reads that ``dekko unused`` was silently
# missing for module-level ``const``-bound variables read (not
# called) elsewhere in the same file, e.g. a guard condition or a
# string-concatenation operand. Only ``identifier`` nodes are
# captured, so a string/number literal can never be mistaken for one;
# a same-named local variable shadowing a repo-wide function is a
# real ambiguity, resolved by reusing ``resolver.py``'s existing
# candidate ladder.
#
# The shorthand-property capture is scoped to an ``object`` (value)
# parent, not a bare ``(shorthand_property_identifier) @ref`` — this
# is belt-and-suspenders, not a behavior change on the currently
# pinned grammar: tree-sitter-javascript (>=0.23, 0.25.0 verified
# live) already uses a *distinct* node type,
# ``shorthand_property_identifier_pattern``, for the destructuring-
# binding case (``const { x } = y`` / ``function f({ x }) {}``), so
# an unscoped ``(shorthand_property_identifier) @ref`` never actually
# matched a destructuring binding to begin with — confirmed by
# parsing both shapes and diffing the query's captures. Scoping to
# ``object`` documents that invariant in the query itself and guards
# against an older/future grammar release that might not keep the
# node types split. ``array`` vs. ``array_pattern`` already has this
# same split for the same reason (the existing
# ``(array (identifier) @ref)`` line is already parent-scoped).
_JS_REFERENCE_BASE = """
(pair value: (identifier) @ref)
(object (shorthand_property_identifier) @ref)
(array (identifier) @ref)
(arguments (identifier) @ref)
(variable_declarator value: (identifier) @ref)
(assignment_expression right: (identifier) @ref)
(template_substitution (identifier) @ref)
(binary_expression left: (identifier) @ref)
(binary_expression right: (identifier) @ref)
(ternary_expression condition: (identifier) @ref)
(ternary_expression consequence: (identifier) @ref)
(ternary_expression alternative: (identifier) @ref)
(spread_element (identifier) @ref)
(subscript_expression object: (identifier) @ref)
(member_expression object: (identifier) @ref)
(return_statement (identifier) @ref)
(parenthesized_expression (identifier) @ref)
(unary_expression argument: (identifier) @ref)
(await_expression (identifier) @ref)
(arrow_function body: (identifier) @ref)
(for_in_statement right: (identifier) @ref)
(export_statement value: (identifier) @ref)
"""
# The last eight positions above are plain *reads* that were never
# captured before: ``handle.close()``, ``return
# DEFAULT_PORT``, ``if (status)``, ``!enabled``, ``for (const e of
# IMAGE_EXTENSIONS)``, ``export default styles``. Nobody noticed,
# because a module-level ``let knownChannelsVersion`` read only by
# ``return knownChannelsVersion`` was being kept "alive" by a *false*
# reference edge from an unrelated file's same-named local. Removing
# those false edges (``resolver._ref_target_visible``) exposed the
# gap: ``dekko unused`` newly flagged 53 symbols across three repos
# and 40 of them were alive through exactly these positions (measured
# by parsing each file and tallying the parent node of every same-file
# use). Writes are deliberately absent: an assignment target or
# ``x++`` is not a use, and a variable that is only ever written is
# dead. A subscript *index* / computed key (``table[key]``) stays
# out too: that pattern is scoped to ``object:`` on purpose,
# since keys are overwhelmingly locals. It costs one known symbol
# across three repos (cline ``hook-factory.ts::exec``).

# TypeScript-only: ``typeof T`` as a *type* (a ``type_query`` node),
# e.g. ``type X = typeof TOOL_DEFAULTS;`` or ``const w: typeof T = y;``.
# ``type_query`` is a TS-grammar-only node type -- plain JS has no type
# syntax at all, so this stays a separate fragment appended only to
# TypeScript/TSX's reference_query, never merged into
# ``_JS_REFERENCE_BASE`` (compiling a query containing ``type_query``
# against tree-sitter-javascript raises "Invalid node type"). Mirrors
# ``_JSX_REFERENCE_EXTRA``'s identical "grammar lacks this node type"
# scoping discipline.
_TS_TYPE_REFERENCE_EXTRA = """
(type_query (identifier) @ref)
(non_null_expression (identifier) @ref)
(as_expression (identifier) @ref)
(satisfies_expression (identifier) @ref)
"""

# JSX attribute/expression values (``<Button onClick={handleClick}
# />``) plus the JSX element tag name itself (``<Sidebar />``'s
# ``Sidebar``) — a separate fragment because plain ``.ts`` (non-JSX
# TypeScript) has no ``jsx_expression``/``jsx_opening_element`` node
# types and would fail to compile with it included. The tag-name
# capture also picks up lowercase host elements (``<div>``) — harmless
# no-ops, since ``resolve_refs`` already drops any ref with zero
# in-repo candidates, and host element names are never repo symbols.
_JSX_REFERENCE_EXTRA = """
(jsx_expression (identifier) @ref)
(jsx_opening_element name: (identifier) @ref)
(jsx_self_closing_element name: (identifier) @ref)
"""

_JS_REFERENCE_QUERY = _JS_REFERENCE_BASE + _JSX_REFERENCE_EXTRA

# Where JS/TS/TSX bind a local name (see
# ``LanguageSpec.binding_query``). ``const``/``let`` bind in the
# nearest block, ``var`` in the nearest function. ``for (x of xs)``
# with no ``const``/``let``/``var`` assigns to an existing name and
# binds nothing, hence ``kind: _``. TS wraps each parameter in a
# ``required_parameter``/``optional_parameter`` and names a class with
# a ``type_identifier``; both are absorbed by ``(_)`` and
# ``extractor._pattern_identifiers`` rather than a second query. The
# two TS-only declaration nodes can't be: naming them against
# tree-sitter-javascript raises "Invalid node type", the same split
# ``_TS_TYPE_REFERENCE_EXTRA`` exists for.
_JS_BINDING_QUERY = """
(formal_parameters) @params
(arrow_function parameter: (identifier) @param)
(lexical_declaration (variable_declarator name: (_) @local))
(variable_declaration (variable_declarator name: (_) @funclocal))
(catch_clause parameter: (_) @scoped) @scope
(for_in_statement kind: _ left: (_) @scoped) @scope
(function_declaration name: (identifier) @defname)
(generator_function_declaration name: (identifier) @defname)
(class_declaration name: (_) @defname)
"""
_TS_BINDING_EXTRA = """
(abstract_class_declaration name: (_) @defname)
(enum_declaration name: (_) @defname)
"""
_JS_BLOCK_SCOPES = (
    "statement_block",
    "for_statement",
    "for_in_statement",
    "catch_clause",
    "switch_body",
)

# JS/TS/TSX throw/catch: ``throw_statement``'s expression is an
# unfielded first named child (see ``extractor._raise_expr``, shared
# with Python/C++'s identically-shaped ``raise_statement``/
# ``throw_statement``). ``catch_clause`` is captured whole for every
# JS/TS/TSX file, but the *walk* differs by language: plain JS never
# type-discriminates a caught value (no ``type`` field exists on JS's
# grammar node at all), so a JS catch always extracts as a catch-all
# regardless of whether it binds a name; TS/TSX additionally carry an
# optional ``type`` field (a ``type_annotation`` wrapping the actual
# type node) for a real, if rare, typed catch — see
# ``extractor._catches_js``/``_catches_ts``. Confirmed against the
# pinned tree-sitter-javascript/typescript grammars.
_JS_THROW_QUERY = """
(throw_statement) @throw
"""
_JS_CATCH_QUERY = """
(catch_clause) @catch
"""

# JS/TS/TSX ``process.env.X``/``process.env["X"]``: dot access reads
# the key as a plain ``property_identifier`` (never dynamic — there is
# no dot-access syntax for a computed name), so
# ``extractor._env_read_js`` reads its text directly, no quote
# stripping needed. Bracket access requires a literal ``(string)``
# index — ``process.env[SOME_VAR]`` structurally fails to match (the
# index node type is ``identifier``, not ``string``) and a template
# literal (`` `APP_${x}` ``) parses as ``template_string``, a distinct
# node type this pattern never captures either — both dynamic-key
# forms are excluded by the query shape itself, no extra filtering
# needed (unlike Python's f-string, which shares node type ``string``
# with a plain literal). Confirmed live against the pinned
# tree-sitter-javascript/typescript grammars.
_JS_ENV_READ_QUERY = """
(member_expression
  object: (member_expression
    object: (identifier) @proc
    property: (property_identifier) @env)
  property: (property_identifier) @key) @call

(subscript_expression
  object: (member_expression
    object: (identifier) @proc
    property: (property_identifier) @env)
  index: (string) @key) @call
"""

JAVASCRIPT = LanguageSpec(
    name="javascript",
    grammar="javascript",
    extensions=(".js", ".jsx", ".mjs", ".cjs"),
    definition_query="""
(function_declaration
  name: (identifier) @name
  parameters: (formal_parameters) @params) @def

(generator_function_declaration
  name: (identifier) @name
  parameters: (formal_parameters) @params) @def

(method_definition
  name: (property_identifier) @name
  parameters: (formal_parameters) @params) @def

(variable_declarator
  name: (identifier) @name
  value: (arrow_function
    parameters: (formal_parameters) @params)) @def

(variable_declarator
  name: (identifier) @name
  value: (function_expression
    parameters: (formal_parameters) @params)) @def

(class_declaration name: (identifier) @classname) @classdef

(program
  (export_statement
    declaration: (lexical_declaration
      (variable_declarator
        name: (identifier) @name
        value: (_) @value) @vardef)))

(program
  (lexical_declaration
    (variable_declarator
      name: (identifier) @name
      value: (_) @value) @vardef))
""",
    call_query="""
(call_expression function: (_) @callee arguments: (_)? @args) @call
(new_expression constructor: (_) @callee arguments: (_)? @args) @call
""",
    import_query="""
(import_statement
  (import_clause
    (named_imports
      (import_specifier
        name: (identifier) @name
        alias: (identifier)? @alias)))
  source: (string) @from_module)

(import_statement
  (import_clause (identifier) @name)
  source: (string) @from_module)

(import_statement
  (import_clause
    (namespace_import
      (identifier) @name))
  source: (string) @from_module)

(import_statement
  . (string) @from_module) @stmt

(variable_declarator
  name: (object_pattern
    [(shorthand_property_identifier_pattern) @name
     (pair_pattern
       key: (property_identifier) @name
       value: (identifier) @alias)])
  value: [
    (await_expression
      (call_expression
        function: (import)
        arguments: (arguments . (string) @from_module)))
    (call_expression
      function: (import)
      arguments: (arguments . (string) @from_module))
    (call_expression
      function: (identifier) @binder
      arguments: (arguments . (string) @from_module))
    (await_expression
      (call_expression
        function: (identifier) @binder
        arguments: (arguments . (string) @from_module)))
  ])

(variable_declarator
  name: (identifier) @name
  value: [
    (await_expression
      (call_expression
        function: (import)
        arguments: (arguments . (string) @from_module)))
    (call_expression
      function: (identifier) @binder
      arguments: (arguments . (string) @from_module))
  ])
""",
    container_types={"class_declaration": "name"},
    method_containers=("class_declaration",),
    param_style="js",
    function_boundary_types=_JS_FUNCTION_BOUNDARIES,
    reference_query=_JS_REFERENCE_QUERY,
    binding_query=_JS_BINDING_QUERY,
    binding_function_scopes=_JS_FUNCTION_BOUNDARIES,
    binding_block_scopes=_JS_BLOCK_SCOPES,
    heritage_query="""
(class_declaration
  name: (identifier) @classname
  (class_heritage)? @heritage) @classdef
""",
    throw_query=_JS_THROW_QUERY,
    catch_query=_JS_CATCH_QUERY,
    env_read_query=_JS_ENV_READ_QUERY,
)

_TS_DEFINITIONS = """
(function_declaration
  name: (identifier) @name
  parameters: (formal_parameters) @params
  return_type: (type_annotation)? @ret) @def

(generator_function_declaration
  name: (identifier) @name
  parameters: (formal_parameters) @params
  return_type: (type_annotation)? @ret) @def

(method_definition
  name: (property_identifier) @name
  parameters: (formal_parameters) @params
  return_type: (type_annotation)? @ret) @def

(variable_declarator
  name: (identifier) @name
  value: (arrow_function
    parameters: (formal_parameters) @params
    return_type: (type_annotation)? @ret)) @def

(variable_declarator
  name: (identifier) @name
  value: (function_expression
    parameters: (formal_parameters) @params
    return_type: (type_annotation)? @ret)) @def

(class_declaration name: (type_identifier) @classname) @classdef

(abstract_class_declaration
  name: (type_identifier) @classname) @classdef

(interface_declaration
  name: (type_identifier) @classname) @classdef

(enum_declaration name: (identifier) @classname) @classdef

(type_alias_declaration
  name: (type_identifier) @classname) @classdef

(program
  (export_statement
    declaration: (lexical_declaration
      (variable_declarator
        name: (identifier) @name
        value: (_) @value) @vardef)))

(program
  (lexical_declaration
    (variable_declarator
      name: (identifier) @name
      value: (_) @value) @vardef))
"""

_TS_CALLS = """
(call_expression function: (_) @callee arguments: (_)? @args) @call
(new_expression constructor: (_) @callee arguments: (_)? @args) @call
"""

_TS_CONTAINERS = {
    "class_declaration": "name",
    "abstract_class_declaration": "name",
    "interface_declaration": "name",
}

# TS/TSX heritage: ``class_declaration``/``abstract_class_declaration``
# carry an optional ``class_heritage`` node (in turn containing an
# ``extends_clause`` and/or ``implements_clause``); ``interface_
# declaration`` carries an optional ``extends_type_clause`` list
# directly (an interface has no ``implements`` of its own). Both
# container shapes land in the same ``@heritage`` capture name — the
# per-language parser in ``extractor.py`` dispatches on the captured
# node's own ``.type`` to walk each shape correctly. Confirmed against
# the pinned tree-sitter-typescript grammar at implementation time
# (``class_heritage``'s ``extends_clause`` has a ``value`` field;
# ``implements_clause``/``extends_type_clause`` list their types as
# plain, unfielded named children).
_TS_HERITAGE = """
(class_declaration
  name: (type_identifier) @classname
  (class_heritage)? @heritage) @classdef

(abstract_class_declaration
  name: (type_identifier) @classname
  (class_heritage)? @heritage) @classdef

(interface_declaration
  name: (type_identifier) @classname
  (extends_type_clause)? @heritage) @classdef
"""

# Same-file type-alias registry:
# originally built because TS's ``type X = {...}``/``type X = A | B``/
# etc. was never extracted as a ``Symbol`` at all, so an ``implements``/
# ``extends`` clause naming a same-file alias had no structural signal
# to tell it apart from a genuinely external base type --
# ``_heritage_external_label`` in ``query.py`` consults this field's
# output (``FileMap.type_aliases`` -> ``MapIndex.type_aliases_by_path``)
# as a same-file counterpart to its existing same-named-relative-import
# check. ``type_alias_declaration`` now also has a real
# ``@classdef`` pattern in ``_TS_DEFINITIONS`` above (kind
# ``"type_alias"``), so this bare-name registry is now a redundant,
# narrower parallel extraction kept only for
# ``_heritage_external_label``'s presentation fallback.
# Confirmed against the pinned tree-sitter-typescript grammar:
# ``type_alias_declaration``'s ``name`` field is a ``type_identifier``,
# fielded as ``name:`` -- no anchor tricks needed. Not wired onto
# ``JAVASCRIPT``: the ``javascript`` tree-sitter grammar has no
# ``type_alias_declaration`` node type at all (no ``type`` keyword in
# the language).
_TS_TYPE_ALIAS_QUERY = """
(type_alias_declaration name: (type_identifier) @name)
"""

# Every parameter list in the file. ``_TS_DEFINITIONS`` above names
# five function shapes and parses only their parameters; a
# ``formal_parameters`` node under anything else (a returned or
# callback ``arrow_function``, a ``function_type`` on an interface
# member or type alias, a ``method_signature``/``call_signature``/
# ``construct_signature``, an overload ``function_signature``, a
# class-field arrow) was parsed by nobody, so a type used only there
# was invisible to ``dekko query type`` and looked dead to ``unused``.
# The query is deliberately the bare node: ``extractor.
# _collect_type_uses`` drops the lists the definition pass already
# claimed, which keeps "unnamed" defined by what ``_TS_DEFINITIONS``
# matches rather than by a second, parallel list of parent node
# types that would drift. Not wired onto ``JAVASCRIPT``: its
# parameter lists carry no annotations, so there is nothing to record.
_TS_TYPE_USE_QUERY = """
(formal_parameters) @params
"""

# TypeScript-only binding form, appended to ``JAVASCRIPT.import_query``
# for the TS/TSX specs: a CommonJS require wearing a type assertion,
# ``const { X } = require("./x") as typeof import("./x")``. claude-code
# has 167 of these (its feature-gated lazy requires). ``as_expression``
# isn't a node type in the JavaScript grammar, so it can't live in the
# shared query. Same ``@binder`` contract as the shared patterns: the
# extractor keeps the match only when the callee is ``require``.
_TS_IMPORT_EXTRA = """
(variable_declarator
  name: (object_pattern
    [(shorthand_property_identifier_pattern) @name
     (pair_pattern
       key: (property_identifier) @name
       value: (identifier) @alias)])
  value: (as_expression
    (call_expression
      function: (identifier) @binder
      arguments: (arguments . (string) @from_module))))

(variable_declarator
  name: (identifier) @name
  value: (as_expression
    (call_expression
      function: (identifier) @binder
      arguments: (arguments . (string) @from_module))))
"""

TYPESCRIPT = LanguageSpec(
    name="typescript",
    grammar="typescript",
    extensions=(".ts", ".mts", ".cts"),
    definition_query=_TS_DEFINITIONS,
    call_query=_TS_CALLS,
    import_query=JAVASCRIPT.import_query + _TS_IMPORT_EXTRA,
    container_types=_TS_CONTAINERS,
    method_containers=tuple(_TS_CONTAINERS),
    param_style="ts",
    function_boundary_types=_JS_FUNCTION_BOUNDARIES,
    # Plain (non-JSX) TypeScript has no jsx_expression node type.
    reference_query=_JS_REFERENCE_BASE + _TS_TYPE_REFERENCE_EXTRA,
    binding_query=_JS_BINDING_QUERY + _TS_BINDING_EXTRA,
    binding_function_scopes=_JS_FUNCTION_BOUNDARIES,
    binding_block_scopes=_JS_BLOCK_SCOPES,
    heritage_query=_TS_HERITAGE,
    throw_query=_JS_THROW_QUERY,
    catch_query=_JS_CATCH_QUERY,
    env_read_query=_JS_ENV_READ_QUERY,
    type_alias_query=_TS_TYPE_ALIAS_QUERY,
    type_use_query=_TS_TYPE_USE_QUERY,
)

TSX = LanguageSpec(
    name="tsx",
    grammar="tsx",
    extensions=(".tsx",),
    definition_query=_TS_DEFINITIONS,
    call_query=_TS_CALLS,
    import_query=JAVASCRIPT.import_query + _TS_IMPORT_EXTRA,
    container_types=_TS_CONTAINERS,
    method_containers=tuple(_TS_CONTAINERS),
    param_style="ts",
    function_boundary_types=_JS_FUNCTION_BOUNDARIES,
    reference_query=_JS_REFERENCE_QUERY + _TS_TYPE_REFERENCE_EXTRA,
    binding_query=_JS_BINDING_QUERY + _TS_BINDING_EXTRA,
    binding_function_scopes=_JS_FUNCTION_BOUNDARIES,
    binding_block_scopes=_JS_BLOCK_SCOPES,
    heritage_query=_TS_HERITAGE,
    throw_query=_JS_THROW_QUERY,
    catch_query=_JS_CATCH_QUERY,
    env_read_query=_JS_ENV_READ_QUERY,
    type_alias_query=_TS_TYPE_ALIAS_QUERY,
    type_use_query=_TS_TYPE_USE_QUERY,
)

# Type-reference edges: a struct/interface type used only
# as a parameter type (this also covers a method's *receiver* type,
# since a receiver is just a ``parameter_declaration`` under a
# different field name), a named or unnamed return type, a var/const
# declaration's type, a composite-literal type, or a struct field's own
# declared type (``field_declaration type: ...`` — also matches an
# anonymous embedded field, e.g. ``type Wrapper struct { RepoMeta }``,
# since tree-sitter-go still tags the sole child ``type``-field-named
# even with no separate field-name node) — never constructed via a
# call-shaped site and therefore invisible to ``call_query`` alone. A
# second group of patterns handles every *wrapped* form (``*T``,
# ``[]T``, ``[N]T``, ``map[K]V``, ``chan T``) via the wrapper node
# types themselves (``pointer_type``/``slice_type``/``array_type``/
# ``map_type``/``channel_type``) rather than enumerating each wrapper
# under each of the plain patterns' parent node types above — safe to
# leave field-unanchored because none of these wrapper node types ever
# occur in a *definition*-name position (only ``type_spec``'s bare,
# unwrapped ``type_identifier`` name field can be that, and it is never
# reachable through a pointer/slice/array/map/channel wrapper), so
# every match is a genuine usage, not a symbol's own declaration. This
# incidentally also closes the ``[]T``/``map[K]V``-shaped false
# positives the plain patterns alone still missed (confirmed live
# against awesome-go's ``tagEntry``, referenced only via ``var tags
# []tagEntry``). Field names and node shapes confirmed against the
# actual ``tree-sitter-go`` grammar (not just read off the .go source)
# during implementation, including the new ``field_declaration``
# pattern (1.1's deliberately-left-uncovered case, closed as a
# follow-up — see ``field_declaration type:``'s throwaway-parse-script
# verification).
_GO_REFERENCE_QUERY = """
(parameter_declaration type: (type_identifier) @ref)
(function_declaration result: (type_identifier) @ref)
(method_declaration result: (type_identifier) @ref)
(var_spec type: (type_identifier) @ref)
(const_spec type: (type_identifier) @ref)
(composite_literal type: (type_identifier) @ref)
(field_declaration type: (type_identifier) @ref)
(pointer_type (type_identifier) @ref)
(slice_type element: (type_identifier) @ref)
(array_type element: (type_identifier) @ref)
(map_type key: (type_identifier) @ref)
(map_type value: (type_identifier) @ref)
(channel_type value: (type_identifier) @ref)
"""

GO = LanguageSpec(
    name="go",
    grammar="go",
    extensions=(".go",),
    definition_query="""
(function_declaration
  name: (identifier) @name
  parameters: (parameter_list) @params
  result: (_)? @ret) @def

(method_declaration
  receiver: (parameter_list) @recv
  name: (field_identifier) @name
  parameters: (parameter_list) @params
  result: (_)? @ret) @def

(type_declaration
  (type_spec
    name: (type_identifier) @classname
    type: (struct_type) @classkind)) @classdef

(type_declaration
  (type_spec
    name: (type_identifier) @classname
    type: (interface_type) @classkind)) @classdef
""",
    call_query="""
(call_expression function: (_) @callee arguments: (_)? @args) @call
""",
    import_query="""
(import_spec
  name: (_)? @alias
  path: (_) @module)
""",
    param_style="go",
    reference_query=_GO_REFERENCE_QUERY,
    # ``os.Getenv("X")``/``os.LookupEnv("X")`` are ``call_expression``
    # whose function is a ``selector_expression`` with ``operand:``/
    # ``field:`` fields; the key is an ``interpreted_string_literal``
    # (Go also has ``raw_string_literal`` — backtick-quoted — not
    # matched here, since a raw-string env-var key is vanishingly rare
    # and only the interpreted form is supported). ``.``
    # anchors the key to the first argument. Verified live against the
    # pinned tree-sitter-go grammar.
    env_read_query="""
(call_expression
  function: (selector_expression
    operand: (identifier) @mod
    field: (field_identifier) @fn)
  arguments: (argument_list . (interpreted_string_literal) @key)) @call
""",
)

# A Java 8 method reference
# (``this::configureBuildInfoTask``, ``Foo::staticMethod``,
# ``java.util.Objects::requireNonNull`` -- tree-sitter node type
# ``method_reference``) is neither a call (no argument list, no
# invocation) nor covered by any reference query, since JAVA previously
# set none at all -- a method used only as a callback reference (e.g.
# passed to a functional-interface parameter) was indistinguishable
# from dead code to ``dekko unused``. The ``"::"`` anchor requires the
# captured ``@ref`` node to immediately follow the literal ``::``
# token; Java's ``Class::new`` constructor-reference form has ``new``
# as an anonymous keyword token in that position, not an
# ``identifier`` node, so it never matches this pattern -- no separate
# exclusion predicate needed. Verified live against the pinned
# tree-sitter-java grammar for ``this::baz``/``Foo::staticMethod``/
# ``java.util.Objects::requireNonNull``/``Class::new``.
_JAVA_REFERENCE_QUERY = """
(method_reference "::" (identifier) @ref)
"""

JAVA = LanguageSpec(
    name="java",
    grammar="java",
    extensions=(".java",),
    definition_query="""
(method_declaration
  type: (_) @ret
  name: (identifier) @name
  parameters: (formal_parameters) @params) @def

(constructor_declaration
  name: (identifier) @name
  parameters: (formal_parameters) @params) @def

(class_declaration name: (identifier) @classname) @classdef
(interface_declaration name: (identifier) @classname) @classdef
(enum_declaration name: (identifier) @classname) @classdef
(record_declaration name: (identifier) @classname) @classdef
""",
    call_query="""
(method_invocation arguments: (_)? @args) @callee @call
(object_creation_expression arguments: (_)? @args) @callee @call
""",
    import_query="""
(import_declaration (scoped_identifier) @module)
""",
    container_types={
        "class_declaration": "name",
        "interface_declaration": "name",
        "enum_declaration": "name",
        "record_declaration": "name",
    },
    method_containers=(
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
    ),
    param_style="generic",
    reference_query=_JAVA_REFERENCE_QUERY,
    heritage_query="""
(class_declaration
  name: (identifier) @classname
  superclass: (superclass)? @superclass
  interfaces: (super_interfaces)? @interfaces) @classdef

(interface_declaration
  name: (identifier) @classname
  (extends_interfaces)? @heritage) @classdef
""",
    # ``throw_statement``'s raised expression is always an
    # ``object_creation_expression`` in practice (walked via
    # ``extractor._callee_java``, reused from call resolution). The
    # second pattern, a bare ``(throws)`` node, is Java's declared
    # checked-exception clause on a method — a distinct, independent
    # signal beyond throw-site scanning (see ``LanguageSpec.
    # throw_query``'s docstring); unfielded on ``method_declaration``,
    # so it's captured standalone rather than nested in the first
    # pattern, and ``extractor._enclosing`` still attributes it to the
    # right method by byte-range containment.
    throw_query="""
(throw_statement) @throw
(throws) @throws_clause
""",
    # ``catch_clause``'s ``catch_formal_parameter`` child is an
    # unfielded positional child (not a named field), itself wrapping
    # an unfielded ``catch_type`` child that lists one or more
    # ``type_identifier`` types directly as named children — a single
    # type for an ordinary catch, 2+ separated by unnamed ``|`` tokens
    # for Java's multi-catch (``catch (A | B e)``). Java requires a
    # typed parameter on every catch clause — there is no catch-all
    # syntax the way C++/Python have one, so ``bare`` is always
    # ``False`` here. Confirmed against the pinned tree-sitter-java
    # grammar.
    catch_query="""
(catch_clause
  (catch_formal_parameter
    (catch_type) @catch_type)) @catch
""",
    # ``System.getenv("X")`` is a ``method_invocation`` with an
    # ``object:``/``name:`` field pair (unlike JS/Python's attribute-
    # chain shape, Java fields these directly). ``.`` anchors the key
    # to the first argument. Verified live against the pinned
    # tree-sitter-java grammar.
    env_read_query="""
(method_invocation
  object: (identifier) @sys
  name: (identifier) @fn
  arguments: (argument_list . (string_literal) @key)) @call
""",
)

# ``RUST``, ``GO``, and ``C`` above deliberately leave ``throw_query``/
# ``catch_query`` at their default ``None`` — a **permanent** exclusion,
# not a placeholder awaiting a future pass (contrast with
# ``heritage_query``'s Go slot, left ``None`` only because that
# extraction hasn't been written yet). Rust's ``Result<T, E>``/``?``
# propagation and Go's returned-``error``-value convention are
# type-inference problems, not syntax a tree-sitter query can point at;
# C has no exception concept to extract at all.

TIER1_SPECS: tuple[LanguageSpec, ...] = (
    PYTHON,
    RUST,
    C,
    CPP,
    JAVASCRIPT,
    TYPESCRIPT,
    TSX,
    GO,
    JAVA,
)

EXTENSION_MAP: dict[str, LanguageSpec] = {
    ext: spec for spec in TIER1_SPECS for ext in spec.extensions
}

SPEC_BY_NAME: dict[str, LanguageSpec] = {
    spec.name: spec for spec in TIER1_SPECS
}


def exception_handling_supported(language: str) -> bool:
    """Whether ``throws``/``catches`` extract anything for ``language``.

    False for Rust/Go/C (permanent exclusion — see the comment above
    ``TIER1_SPECS``) and for any language not in ``TIER1_SPECS`` at
    all (Tier-2 generic-grammar languages, which never extract
    throw/catch sites in the first place — same practical answer, a
    different reason).

    Args:
        language: A language name as recorded on ``Symbol.language``
            or in ``MapIndex.languages_by_path``.

    Returns:
        ``True`` if the language's ``LanguageSpec`` defines a
        ``throw_query``, ``False`` otherwise.
    """
    spec = SPEC_BY_NAME.get(language)
    return spec is not None and spec.throw_query is not None


# Bumped whenever ``repo_ops._resolve_header_spec``'s C/C++ ``.h``
# dispatch heuristic (``extractor.looks_like_cpp_header``) changes in
# a way that could reclassify a file differently than before. That
# heuristic lives outside any ``LanguageSpec``, so ``spec_fingerprint``
# would otherwise be blind to it entirely -- a ``.dekko/`` cache built
# before the heuristic shipped (or before a later change to it) would
# keep silently reusing a stale, possibly-wrong-grammar ``FileMap`` for
# every ``.h`` file after an upgrade, exactly the silent-wrong-answer
# failure mode the heuristic itself was added to close, just relocated
# to the upgrade boundary. Folding this
# into the fingerprint below means a cache built under old dispatch
# logic is discarded automatically -- every file re-parses once on the
# next non-``--full`` ``dekko map`` run -- rather than requiring users
# to know to pass ``--full`` themselves.
_HEADER_DISPATCH_HEURISTIC_VERSION = 1

# Bumped whenever ``extractor._heritage_rust_impl``'s subject-recovery
# logic changes: an ``impl Trait
# for Type`` block whose ``Type`` isn't defined in the same file used
# to be silently dropped at extraction (no same-file symbol to attach
# a ``RawHeritage`` to); it's now emitted with ``subtype_id=""`` and
# ``subtype_name`` set instead, for ``resolver.resolve_heritage`` to
# resolve repo-wide. Like ``_HEADER_DISPATCH_HEURISTIC_VERSION`` above,
# this is Python logic inside ``extractor.py``, not a ``LanguageSpec``
# field, so the field-hashing loop below is structurally blind to it —
# without this constant, a ``.dekko`` cache built before this change
# would keep silently reusing ``FileMap``s missing every
# cross-file-``impl`` heritage clause after an upgrade.
_RUST_HERITAGE_IMPL_SUBTYPE_RECOVERY_VERSION = 1

# Bumped whenever ``extractor._collect_rust_macro_calls`` changes what
# it emits for a call recovered from a macro's token stream -- round
# 32: those calls used to be rendered ``Type.name`` with no argument
# count whatever the source said, and now carry their real ``::``/``.``
# joiner, full path, and a count. Same blind spot as the two constants
# above (Python logic, not a ``LanguageSpec`` field), and the same
# failure if it's missed: a ``.dekko`` cache built before the change
# keeps serving dot-joined ``RawCall``s, so after an upgrade the fix
# would appear not to work on exactly the repos already mapped.
_RUST_MACRO_CALL_RECOVERY_VERSION = 1

# Bumped whenever ``extractor._canonical_expr`` changes how a call's
# receiver is rendered into ``RawCall.text``/``receiver``. Those two
# strings used to be the receiver's source text verbatim, arguments
# and all; they are now a bounded structural rendering (``expect()
# .toBe``, ``[].join``). Same blind spot as the constants above: a
# ``.dekko`` cache built before the change keeps serving the old
# strings, and the external id table would stay unbounded on exactly
# the repos already mapped.
_CALLEE_TEXT_CANONICAL_VERSION = 1

# Bumped whenever ``extractor._rust_error_attribute_spans`` changes
# which calls it drops. tree-sitter-rust can't parse an attribute on a
# struct-pattern field, and its error recovery leaves the attribute's
# payload (``cfg_attr(not(..), allow(..))``) as real call nodes; those
# calls are now dropped. Same blind spot as the constants above: a
# ``.dekko`` cache built before the change keeps serving them.
_RUST_ERROR_ATTRIBUTE_RECOVERY_VERSION = 1


def spec_fingerprint() -> str:
    """Hash every Tier-1 extraction spec into one invalidation key.

    Captures everything that changes what ``extractor.py`` pulls out
    of a file — queries, container/method-container types, parameter
    style, and any field added to ``LanguageSpec`` later (the loop is
    driven by ``dataclasses.fields``, not a hand-kept list, so a new
    field is covered automatically) — plus
    ``_HEADER_DISPATCH_HEURISTIC_VERSION``,
    ``_RUST_HERITAGE_IMPL_SUBTYPE_RECOVERY_VERSION``,
    ``_RUST_MACRO_CALL_RECOVERY_VERSION``,
    ``_CALLEE_TEXT_CANONICAL_VERSION`` and
    ``_RUST_ERROR_ATTRIBUTE_RECOVERY_VERSION``, which each cover
    one piece of dispatch/recovery logic that lives outside any
    ``LanguageSpec`` (see those constants' own comments). Used to
    invalidate a stale ``.dekko`` cache entry or flag a stale
    ``map.json`` even when the released package version string hasn't
    changed — a dev iteration or hotfix that reuses the same version,
    or an unreleased checkout.

    Returns:
        A stable hex digest, unchanged as long as extraction behavior
        is unchanged.
    """
    parts: list[str] = [
        f"header_dispatch_heuristic={_HEADER_DISPATCH_HEURISTIC_VERSION}",
        "rust_heritage_impl_subtype_recovery="
        f"{_RUST_HERITAGE_IMPL_SUBTYPE_RECOVERY_VERSION}",
        f"rust_macro_call_recovery={_RUST_MACRO_CALL_RECOVERY_VERSION}",
        f"callee_text_canonical={_CALLEE_TEXT_CANONICAL_VERSION}",
        "rust_error_attribute_recovery="
        f"{_RUST_ERROR_ATTRIBUTE_RECOVERY_VERSION}",
    ]
    for spec in TIER1_SPECS:
        for f in fields(spec):
            value = getattr(spec, f.name)
            if isinstance(value, dict):
                value = tuple(sorted(value.items()))
            parts.append(f"{f.name}={value!r}")
    canonical = "\x1f".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Tier-2: extension → tree-sitter-language-pack grammar name. These are
# handled by the generic extractor (names + calls, raw parameter text).
# Grammars are downloaded on demand by the language pack on first use.
TIER2_GRAMMARS: dict[str, str] = {
    ".rb": "ruby",
    ".rake": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".sc": "scala",
    ".lua": "lua",
    ".pl": "perl",
    ".pm": "perl",
    ".r": "r",
    ".jl": "julia",
    ".dart": "dart",
    ".zig": "zig",
    ".hs": "haskell",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hrl": "erlang",
    ".ml": "ocaml",
    ".mli": "ocaml_interface",
    ".clj": "clojure",
    ".gleam": "gleam",
    ".nim": "nim",
    ".groovy": "groovy",
    ".gradle": "groovy",
    ".sol": "solidity",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "zsh",
    ".ps1": "powershell",
    ".sql": "sql",
    ".f90": "fortran",
    ".f95": "fortran",
    ".pas": "pascal",
    ".elm": "elm",
    ".fs": "fsharp",
    ".rkt": "racket",
    ".scm": "scheme",
    ".lisp": "commonlisp",
    ".el": "elisp",
    ".vim": "vim",
    ".tcl": "tcl",
    ".d": "d",
    ".adb": "ada",
    ".ads": "ada",
    ".ha": "hare",
    ".odin": "odin",
    ".cr": "crystal",
    ".hx": "haxe",
    ".gd": "gdscript",
    ".mojo": "mojo",
    ".nix": "nix",
    ".bzl": "starlark",
    ".cmake": "cmake",
    ".vue": "vue",
    ".svelte": "svelte",
}

# Extensions dekko recognizes as source code but has no grammar for at
# all (not even a Tier-2 attempt) — confirmed gaps rather than a
# blanket guess about every unfamiliar extension. Unlike Tier-2 files,
# these never reach the extractor, so without this registry they were
# silently dropped by ``walker.discover`` with no warning anywhere:
# ``dekko map``'s own output, ``map.json``, and every read command
# (``query callers``, ``find_usages``, ``summary``) treated a partially
# mapped repo as complete, producing confident "no callers found"
# answers for symbols only used in these files. Extend this list as new gaps
# are confirmed; it intentionally does not attempt to enumerate every non-code
# extension (``.md``, ``.json``, images, ...), which stay silently ignored as
# before.
KNOWN_UNSUPPORTED: dict[str, str] = {
    ".astro": "astro",
}


def spec_for_path(filename: str) -> LanguageSpec | None:
    """Return the Tier-1 spec for a filename, or ``None``.

    Args:
        filename: Any path or basename; only the extension is used.

    Returns:
        The matching ``LanguageSpec``, or ``None`` when the extension
        is not a Tier-1 language.
    """
    dot = filename.rfind(".")
    if dot == -1:
        return None

    return EXTENSION_MAP.get(filename[dot:].lower())


def tier2_grammar_for_path(filename: str) -> str | None:
    """Return the Tier-2 grammar name for a filename, or ``None``."""
    dot = filename.rfind(".")
    if dot == -1:
        return None

    return TIER2_GRAMMARS.get(filename[dot:].lower())


def is_supported(filename: str) -> bool:
    """Check whether any registered language handles this filename."""
    return (
        spec_for_path(filename) is not None
        or tier2_grammar_for_path(filename) is not None
    )


def known_unsupported_language(filename: str) -> str | None:
    """Return the display name of a confirmed-unsupported language.

    Distinct from every extension dekko simply doesn't recognize
    (``.md``, ``.json``, images, ...), which are non-code and stay
    silently ignored. This only covers extensions in
    ``KNOWN_UNSUPPORTED`` — languages dekko has confirmed look like
    source code but has no grammar for — so a caller can surface a
    targeted "no parser for X" warning instead of treating the gap as
    ordinary non-code noise.

    Args:
        filename: Any path or basename; only the extension is used.

    Returns:
        The language's display name, or ``None``.
    """
    dot = filename.rfind(".")
    if dot == -1:
        return None

    return KNOWN_UNSUPPORTED.get(filename[dot:].lower())
