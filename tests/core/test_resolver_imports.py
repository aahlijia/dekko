"""Resolver tests for the module dependency graph (resolve_imports(),
find_cycles()).

Most cases use synthetic ``FileMap``/``Import`` fixtures (mirroring
``test_resolver_heritage.py``'s style) since ``resolve_imports`` only
reads ``FileMap.path``/``.language``/``.imports`` — no candidate ladder
to exercise the way call/reference/heritage resolution has. A handful
of cases run real source through ``extract_file`` + ``map_repository``
end to end, to pin down the trickier ``Import.source`` shapes (the
appended-imported-name quirk both Python's and JS's extractors share,
and Java/Python's nested-source-root layouts) against what the real
extractor actually produces, not just an assumed shape.
"""

from pathlib import Path

from dekko.core.model import FileMap, Import
from dekko.core.resolver import find_cycles, resolve_imports
from dekko.integrations import cli
from dekko.render import mapfile

from conftest import RepoFactory


def _fm(path: str, language: str, imports: list[Import]) -> FileMap:
    return FileMap(path, language, imports=imports)


def _imp(path: str, name: str, source: str) -> Import:
    return Import(path=path, name=name, source=source)


# ---------------------------------------------------------------------
# Python


def test_python_relative_import_resolves() -> None:
    files = [
        _fm(
            "pkg/main.py",
            "python",
            [_imp("pkg/main.py", "helper", "..outside.helper")],
        ),
        _fm("outside.py", "python", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["pkg/main.py"] == ["outside.py"]
    assert graph.deps_in["outside.py"] == ["pkg/main.py"]
    assert graph.edges[0].names == ["helper"]


def test_python_relative_import_to_submodule_file() -> None:
    files = [
        _fm(
            "pkg/main.py",
            "python",
            [_imp("pkg/main.py", "helper", ".sub.mod.helper")],
        ),
        _fm("pkg/__init__.py", "python", []),
        _fm("pkg/sub/__init__.py", "python", []),
        _fm("pkg/sub/mod.py", "python", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["pkg/main.py"] == ["pkg/sub/mod.py"]


def test_python_bare_submodule_import_wins_over_parent_symbol_reading() -> (
    None
):
    # "from pkg.sub import mod" produces Import.source="pkg.sub.mod" --
    # ambiguous in principle between "mod is a submodule" and "mod is
    # a symbol in pkg/sub/__init__.py", but pkg/sub/mod.py existing as
    # a real file makes the submodule reading unconditionally correct
    # (real Python import semantics), so it must win even though
    # pkg/sub/__init__.py also exists.
    files = [
        _fm("other.py", "python", [_imp("other.py", "mod", "pkg.sub.mod")]),
        _fm("pkg/__init__.py", "python", []),
        _fm("pkg/sub/__init__.py", "python", []),
        _fm("pkg/sub/mod.py", "python", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["other.py"] == ["pkg/sub/mod.py"]


def test_python_symbol_in_parent_module_falls_back() -> None:
    # "from pkg import helper" where helper is a symbol defined inside
    # pkg/__init__.py, not its own submodule file -- no pkg/helper.py
    # exists, so the "symbol in parent" reading is the only match.
    files = [
        _fm("other.py", "python", [_imp("other.py", "helper", "pkg.helper")]),
        _fm("pkg/__init__.py", "python", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["other.py"] == ["pkg/__init__.py"]


def test_python_absolute_import_uses_src_layout_package_root() -> None:
    # The repo's own package sits under src/, not the repo root --
    # absolute-import resolution must search for the package root by
    # basename, not assume it's a direct child of the repo root.
    files = [
        _fm(
            "tools/script.py",
            "python",
            [_imp("tools/script.py", "resolver", "dekko.core.resolver")],
        ),
        _fm("src/dekko/__init__.py", "python", []),
        _fm("src/dekko/core/__init__.py", "python", []),
        _fm("src/dekko/core/resolver.py", "python", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["tools/script.py"] == ["src/dekko/core/resolver.py"]


def test_python_stdlib_import_is_external() -> None:
    files = [_fm("a.py", "python", [_imp("a.py", "os", "os")])]
    graph = resolve_imports(files)
    assert graph.deps_out == {}
    assert graph.external["a.py"] == ["os"]


def test_python_absolute_import_without_package_root_is_external() -> None:
    # No directory in the repo is a real top-level package (no
    # __init__.py anywhere) -- must not guess a path match.
    files = [
        _fm("a.py", "python", [_imp("a.py", "thing", "somepkg.mod.thing")]),
        _fm("somepkg/mod.py", "python", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out == {}
    assert graph.external["a.py"] == ["somepkg.mod.thing"]


def test_python_self_import_is_a_resolved_edge_not_external() -> None:
    files = [
        _fm("pkg/a.py", "python", [_imp("pkg/a.py", "a", ".a.a")]),
        _fm("pkg/__init__.py", "python", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["pkg/a.py"] == ["pkg/a.py"]
    assert "pkg/a.py" not in graph.external


# ---------------------------------------------------------------------
# JS/TS/TSX


def test_js_relative_import_resolves_with_extension_guessing() -> None:
    files = [
        _fm(
            "src/index.ts",
            "typescript",
            [_imp("src/index.ts", "Button", "./components/Button/Button")],
        ),
        _fm("src/components/Button.tsx", "tsx", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["src/index.ts"] == ["src/components/Button.tsx"]


def test_js_relative_import_resolves_index_file() -> None:
    files = [
        _fm(
            "src/app.js",
            "javascript",
            [_imp("src/app.js", "widget", "./widgets/widget")],
        ),
        _fm("src/widgets/index.js", "javascript", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["src/app.js"] == ["src/widgets/index.js"]


def test_js_bare_specifier_is_external_without_repo_root_search() -> None:
    # Bare specifiers are external by construction, even if a
    # same-named directory happens to exist in the repo.
    files = [
        _fm("src/app.js", "javascript", [_imp("src/app.js", "x", "utils/x")]),
        _fm("utils/x.js", "javascript", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out == {}
    assert graph.external["src/app.js"] == ["utils"]


def test_js_multiple_named_imports_collapse_to_one_external_label() -> None:
    files = [
        _fm(
            "src/app.js",
            "javascript",
            [
                _imp("src/app.js", "useState", "react/useState"),
                _imp("src/app.js", "useEffect", "react/useEffect"),
            ],
        )
    ]
    graph = resolve_imports(files)
    assert graph.external["src/app.js"] == ["react"]


# ---------------------------------------------------------------------
# Rust


def test_rust_crate_path_resolves_against_crate_root() -> None:
    files = [
        _fm("src/lib.rs", "rust", []),
        _fm("src/utils/mod.rs", "rust", []),
        _fm(
            "src/utils/extra.rs",
            "rust",
            [_imp("src/utils/extra.rs", "helper", "crate::utils::helper")],
        ),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["src/utils/extra.rs"] == ["src/utils/mod.rs"]


def test_rust_crate_path_to_item_in_crate_root() -> None:
    files = [
        _fm("src/lib.rs", "rust", []),
        _fm("src/utils/mod.rs", "rust", []),
        _fm(
            "src/utils/extra.rs",
            "rust",
            [_imp("src/utils/extra.rs", "top", "crate::top")],
        ),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["src/utils/extra.rs"] == ["src/lib.rs"]


def test_rust_super_resolves_to_parent_module() -> None:
    files = [
        _fm("src/lib.rs", "rust", []),
        _fm("src/utils/mod.rs", "rust", []),
        _fm(
            "src/utils/sub/mod.rs",
            "rust",
            [_imp("src/utils/sub/mod.rs", "helper", "super::helper")],
        ),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["src/utils/sub/mod.rs"] == ["src/utils/mod.rs"]


def test_rust_bare_crate_name_is_external() -> None:
    files = [
        _fm(
            "src/lib.rs",
            "rust",
            [_imp("src/lib.rs", "Deserialize", "serde::Deserialize")],
        )
    ]
    graph = resolve_imports(files)
    assert graph.deps_out == {}
    assert graph.external["src/lib.rs"] == ["serde::Deserialize"]


# ---------------------------------------------------------------------
# Java


def test_java_import_resolves_flat_layout() -> None:
    files = [
        _fm(
            "com/example/Foo.java",
            "java",
            [_imp("com/example/Foo.java", "Bar", "com.example.Bar")],
        ),
        _fm("com/example/Bar.java", "java", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["com/example/Foo.java"] == ["com/example/Bar.java"]


def test_java_import_resolves_nested_maven_module_layout() -> None:
    # Confirmed against test-repos/spring-boot's real layout: each
    # module nests its own src/main/java under a module directory, not
    # the repo root, so a literal-prefix-only check would miss this.
    files = [
        _fm(
            "spring-core/src/main/java/com/example/Foo.java",
            "java",
            [
                _imp(
                    "spring-core/src/main/java/com/example/Foo.java",
                    "Bar",
                    "com.example.Bar",
                )
            ],
        ),
        _fm("spring-core/src/main/java/com/example/Bar.java", "java", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out[
        "spring-core/src/main/java/com/example/Foo.java"
    ] == ["spring-core/src/main/java/com/example/Bar.java"]


def test_java_stdlib_import_is_external() -> None:
    files = [
        _fm(
            "com/example/Foo.java",
            "java",
            [_imp("com/example/Foo.java", "List", "java.util.List")],
        )
    ]
    graph = resolve_imports(files)
    assert graph.deps_out == {}
    assert graph.external["com/example/Foo.java"] == ["java.util.List"]


# ---------------------------------------------------------------------
# C/C++


def test_cpp_quoted_include_resolves_by_filename() -> None:
    files = [
        _fm(
            "src/main.cpp",
            "cpp",
            [_imp("src/main.cpp", "local", "local.h")],
        ),
        _fm("include/local.h", "cpp", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out["src/main.cpp"] == ["include/local.h"]


def test_cpp_system_include_stays_external() -> None:
    files = [
        _fm("src/main.cpp", "cpp", [_imp("src/main.cpp", "vector", "vector")])
    ]
    graph = resolve_imports(files)
    assert graph.deps_out == {}
    assert graph.external["src/main.cpp"] == ["vector"]


def test_cpp_ambiguous_same_basename_stays_external() -> None:
    # Two headers named "local.h" in different directories -- skip
    # rather than guess.
    files = [
        _fm(
            "src/main.cpp",
            "cpp",
            [_imp("src/main.cpp", "local", "local.h")],
        ),
        _fm("include/a/local.h", "cpp", []),
        _fm("include/b/local.h", "cpp", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out == {}
    assert graph.external["src/main.cpp"] == ["local.h"]


# ---------------------------------------------------------------------
# Go (deliberately unsupported)


def test_go_import_is_always_external() -> None:
    files = [
        _fm(
            "pkg/foo/foo.go",
            "go",
            [_imp("pkg/foo/foo.go", "bar", "myrepo/pkg/bar")],
        ),
        _fm("pkg/bar/bar.go", "go", []),
    ]
    graph = resolve_imports(files)
    assert graph.deps_out == {}
    assert graph.external["pkg/foo/foo.go"] == ["myrepo/pkg/bar"]


# ---------------------------------------------------------------------
# Cycle detection


def test_find_cycles_detects_two_file_cycle() -> None:
    deps_out = {"x.py": ["y.py"], "y.py": ["x.py"]}
    cycles = find_cycles(deps_out)
    assert cycles == [["x.py", "y.py"]]


def test_find_cycles_detects_three_file_cycle() -> None:
    deps_out = {"a.py": ["b.py"], "b.py": ["c.py"], "c.py": ["a.py"]}
    cycles = find_cycles(deps_out)
    assert cycles == [["a.py", "b.py", "c.py"]]


def test_find_cycles_reports_self_import_separately() -> None:
    deps_out = {"a.py": ["a.py"]}
    cycles = find_cycles(deps_out)
    assert cycles == [["a.py"]]


def test_find_cycles_ignores_acyclic_graph() -> None:
    deps_out = {"a.py": ["b.py"], "b.py": ["c.py"]}
    assert find_cycles(deps_out) == []


def test_find_cycles_sorted_by_size_descending() -> None:
    deps_out = {
        "a.py": ["b.py"],
        "b.py": ["a.py"],
        "x.py": ["y.py"],
        "y.py": ["z.py"],
        "z.py": ["x.py"],
    }
    cycles = find_cycles(deps_out)
    assert [len(c) for c in cycles] == [3, 2]


def test_find_cycles_handles_deep_chain_without_recursion_error() -> None:
    # A straight-line chain 5000 deep, closed into one big cycle --
    # exercises the iterative Tarjan implementation well past Python's
    # default recursion limit (1000).
    n = 5000
    deps_out = {f"f{i}.py": [f"f{i + 1}.py"] for i in range(n)}
    deps_out[f"f{n}.py"] = ["f0.py"]
    cycles = find_cycles(deps_out)
    assert len(cycles) == 1
    assert len(cycles[0]) == n + 1


# ---------------------------------------------------------------------
# End-to-end: real extraction through map_repository()


def test_end_to_end_python_src_layout(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(
        {
            "src/dekko/__init__.py": "",
            "src/dekko/core/__init__.py": "",
            "src/dekko/core/resolver.py": "def resolve():\n    pass\n",
            "tools/script.py": (
                "from dekko.core.resolver import resolve\nresolve()\n"
            ),
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    assert index.module_deps_out["tools/script.py"] == [
        "src/dekko/core/resolver.py"
    ]


def test_end_to_end_js_relative_import(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(
        {
            "src/components/Button.tsx": (
                "export function Button() { return null; }\n"
            ),
            "src/index.ts": (
                'import { Button } from "./components/Button";\n'
                'import React from "react";\n'
            ),
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    assert index.module_deps_out["src/index.ts"] == [
        "src/components/Button.tsx"
    ]
    assert index.module_external["src/index.ts"] == ["react"]


def test_end_to_end_map_json_round_trip(
    make_mapped_repo: RepoFactory, tmp_path: Path
) -> None:
    root = make_mapped_repo(
        {
            "a.py": "from .b import helper\n",
            "b.py": "def helper():\n    pass\n",
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    assert index.module_deps_out["a.py"] == ["b.py"]
    assert index.module_deps_in["b.py"] == ["a.py"]
    assert index.doc_version == mapfile.MAP_DOC_VERSION


def test_end_to_end_deps_cli_no_regen_reads_cached_map(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(
        {
            "a.py": "from .b import helper\n",
            "b.py": "def helper():\n    pass\n",
        }
    )
    assert cli.main(["deps", "--root", str(root), "--no-regen"]) == 0
