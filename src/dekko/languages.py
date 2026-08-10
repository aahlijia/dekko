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
            ``call_query`` feeds. Two distinct shapes so far: bare
            identifiers used as *values* rather than invoked —
            object-literal property values, array elements, call
            arguments, assignment/declarator right-hand sides, and (for
            JS/TS/TSX) JSX attribute values and JSX element tag names —
            which a plain call-expression query structurally cannot see
            (a callback passed by reference, or a component used only
            as ``<Foo />``, is never a call site; bug #2b/#1.1b); and
            type-position identifiers — parameter/return/variable/
            const declaration types and composite-literal types (Go's
            struct/interface usage, bug #1.1a) — which a
            definition/call query never visits at all since they name
            a *type*, not a callable or a value. ``None`` for languages
            without one yet (JS, TS, TSX, Go as of this writing).
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
(call function: (_) @callee) @call
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
)

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
""",
    call_query="""
(call_expression function: (_) @callee) @call
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

C = LanguageSpec(
    name="c",
    grammar="c",
    extensions=(".c", ".h"),
    definition_query=_C_DEFINITIONS,
    call_query="""
(call_expression function: (_) @callee) @call
""",
    import_query="""
(preproc_include path: (_) @module)
""",
    param_style="c",
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
(call_expression function: (_) @callee) @call
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

# Bare-identifier value references shared by JS/TS/TSX (bug #2b): a
# callback wired up by name rather than invoked at that site — an
# object-literal property value, a shorthand property, an array
# element, a bare call argument, an assignment/declarator
# right-hand side, or a ``${...}`` template-literal substitution.
# Only ``identifier`` nodes are captured, so a string/number literal
# can never be mistaken for one; a same-named local variable
# shadowing a repo-wide function is a real ambiguity, resolved by
# reusing ``resolver.py``'s existing candidate ladder.
_JS_REFERENCE_BASE = """
(pair value: (identifier) @ref)
(shorthand_property_identifier) @ref
(array (identifier) @ref)
(arguments (identifier) @ref)
(variable_declarator value: (identifier) @ref)
(assignment_expression right: (identifier) @ref)
(template_substitution (identifier) @ref)
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
(call_expression function: (_) @callee) @call
(new_expression constructor: (_) @callee) @call
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
""",
    container_types={"class_declaration": "name"},
    method_containers=("class_declaration",),
    param_style="js",
    function_boundary_types=_JS_FUNCTION_BOUNDARIES,
    reference_query=_JS_REFERENCE_QUERY,
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
(call_expression function: (_) @callee) @call
(new_expression constructor: (_) @callee) @call
"""

_TS_CONTAINERS = {
    "class_declaration": "name",
    "abstract_class_declaration": "name",
    "interface_declaration": "name",
}

TYPESCRIPT = LanguageSpec(
    name="typescript",
    grammar="typescript",
    extensions=(".ts", ".mts", ".cts"),
    definition_query=_TS_DEFINITIONS,
    call_query=_TS_CALLS,
    import_query=JAVASCRIPT.import_query,
    container_types=_TS_CONTAINERS,
    method_containers=tuple(_TS_CONTAINERS),
    param_style="ts",
    function_boundary_types=_JS_FUNCTION_BOUNDARIES,
    # Plain (non-JSX) TypeScript has no jsx_expression node type.
    reference_query=_JS_REFERENCE_BASE,
)

TSX = LanguageSpec(
    name="tsx",
    grammar="tsx",
    extensions=(".tsx",),
    definition_query=_TS_DEFINITIONS,
    call_query=_TS_CALLS,
    import_query=JAVASCRIPT.import_query,
    container_types=_TS_CONTAINERS,
    method_containers=tuple(_TS_CONTAINERS),
    param_style="ts",
    function_boundary_types=_JS_FUNCTION_BOUNDARIES,
    reference_query=_JS_REFERENCE_QUERY,
)

# Type-reference edges (bug #1.1a): a struct/interface type used only
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
(call_expression function: (_) @callee) @call
""",
    import_query="""
(import_spec
  name: (_)? @alias
  path: (_) @module)
""",
    param_style="go",
    reference_query=_GO_REFERENCE_QUERY,
)

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
(method_invocation) @callee @call
(object_creation_expression) @callee @call
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
)

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


def spec_fingerprint() -> str:
    """Hash every Tier-1 extraction spec into one invalidation key.

    Captures everything that changes what ``extractor.py`` pulls out
    of a file — queries, container/method-container types, parameter
    style, and any field added to ``LanguageSpec`` later (the loop is
    driven by ``dataclasses.fields``, not a hand-kept list, so a new
    field is covered automatically). Used to invalidate a stale
    ``.dekko`` cache entry or flag a stale ``map.json`` even when the
    released package version string hasn't changed — a dev iteration
    or hotfix that reuses the same version, or an unreleased checkout.

    Returns:
        A stable hex digest, unchanged as long as extraction behavior
        is unchanged.
    """
    parts: list[str] = []
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
# answers for symbols only used in these files (2026-07-31 eval,
# gitaustin/Astro repo). Extend this list as new gaps are confirmed;
# it intentionally does not attempt to enumerate every non-code
# extension (``.md``, ``.json``, images, ...), which stay silently
# ignored as before.
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
