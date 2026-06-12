"""Language registry: extensions, grammars, and tree-sitter queries.

Tier-1 languages get dedicated queries with full parameter/return-type
fidelity. Tier-2 languages (everything else in the language pack) are
handled by the generic fallback extractor and only need a grammar name.
"""

from dataclasses import dataclass, field


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
)

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
    type: (struct_type))) @classdef

(type_declaration
  (type_spec
    name: (type_identifier) @classname
    type: (interface_type))) @classdef
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
