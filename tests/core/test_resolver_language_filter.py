"""Every resolution entry point sees only candidates the call can reach.

A call, heritage clause, reference or thrown type is resolved against
same-language candidates (or, failing that, its language family's),
and an ambiguous row lists exactly the 2+ candidates that were
weighed. A call with no reachable candidate is external, never an
ambiguous row naming a symbol in some other language.
"""

from dataclasses import replace
from typing import Any

from dekko.core import languages
from dekko.core.model import (
    CallGraph,
    FileMap,
    Import,
    Param,
    RawCall,
    RawHeritage,
    RawRef,
    RawThrow,
    Symbol,
)
from dekko.core.resolver import resolve


def _sym(
    path: str,
    name: str,
    language: str,
    kind: str = "function",
    qual: str | None = None,
    line: int = 1,
) -> Symbol:
    qual = qual or name
    return Symbol(
        id=f"{path}::{qual}",
        name=name,
        qualname=qual,
        kind=kind,
        path=path,
        language=language,
        start_line=line,
    )


def _call(
    caller: Symbol,
    name: str,
    receiver: str | None = None,
    arg_count: int | None = None,
) -> RawCall:
    return RawCall(
        caller_id=caller.id,
        path=caller.path,
        text=f"{receiver}.{name}" if receiver else name,
        name=name,
        receiver=receiver,
        line=5,
        arg_count=arg_count,
    )


def _module_call(
    path: str, name: str, arg_count: int | None = None
) -> RawCall:
    return RawCall(
        caller_id=None,
        path=path,
        text=name,
        name=name,
        line=2,
        arg_count=arg_count,
    )


def _file(path: str, language: str, *symbols: Symbol, **kw: Any) -> FileMap:
    return FileMap(path, language, symbols=list(symbols), **kw)


def _edges(graph: CallGraph) -> set[tuple[str, str]]:
    return {(e.caller, e.callee) for e in graph.edges}


def test_ambiguous_row_lists_only_same_language_candidates() -> None:
    py_a = _sym("a.py", "helper", "python")
    py_b = _sym("b.py", "helper", "python")
    cc = _sym("c.cc", "helper", "cpp")
    caller = _sym("main.py", "run", "python")
    graph = resolve(
        [
            _file("a.py", "python", py_a),
            _file("b.py", "python", py_b),
            _file("c.cc", "cpp", cc),
            _file(
                "main.py",
                "python",
                caller,
                calls=[_call(caller, "helper", receiver="obj")],
            ),
        ]
    )
    assert graph.ambiguous == [(caller.id, "helper", [py_a.id, py_b.id])]
    assert _edges(graph) == set()


def test_java_and_kotlin_callers_record_their_own_language() -> None:
    java = [
        _sym(f"J{i}.java", "getName", "java", "method", f"J{i}.getName")
        for i in range(2)
    ]
    kotlin = [
        _sym(f"K{i}.kt", "getName", "kotlin", "method", f"K{i}.getName")
        for i in range(2)
    ]
    jcaller = _sym("App.java", "run", "java", "method", "App.run")
    kcaller = _sym("App.kt", "run", "kotlin", "method", "AppKt.run")
    files = [_file(s.path, s.language, s) for s in java + kotlin]
    files.append(
        _file(
            "App.java",
            "java",
            jcaller,
            calls=[_call(jcaller, "getName", receiver="x")],
        )
    )
    files.append(
        _file(
            "App.kt",
            "kotlin",
            kcaller,
            calls=[_call(kcaller, "getName", receiver="x")],
        )
    )
    rows = {caller: cands for caller, _name, cands in resolve(files).ambiguous}
    assert rows[jcaller.id] == [s.id for s in java]
    assert rows[kcaller.id] == [s.id for s in kotlin]


def test_import_alias_ignores_other_language_namesakes() -> None:
    # ``_module_matches`` accepts any ``main.*`` file for the source
    # ``./gen.mjs/main``, so two Rust ``main``s used to join the alias
    # list and made a one-target import ambiguous.
    js_main = _sym("scripts/gen.mjs", "main", "javascript")
    rs_a = _sym("crates/a/src/main.rs", "main", "rust")
    rs_b = _sym("crates/b/src/main.rs", "main", "rust")
    caller = _sym("scripts/build.mjs", "run", "javascript")
    graph = resolve(
        [
            _file("scripts/gen.mjs", "javascript", js_main),
            _file("crates/a/src/main.rs", "rust", rs_a),
            _file("crates/b/src/main.rs", "rust", rs_b),
            _file(
                "scripts/build.mjs",
                "javascript",
                caller,
                calls=[_call(caller, "gen", arg_count=0)],
                imports=[
                    Import(
                        path="scripts/build.mjs",
                        name="gen",
                        source="./gen.mjs/main",
                    )
                ],
            ),
        ]
    )
    assert _edges(graph) == {(caller.id, js_main.id)}
    assert graph.ambiguous == []


def test_reference_alias_ignores_other_language_namesakes() -> None:
    js_main = _sym("scripts/gen.mjs", "main", "javascript")
    rs_main = _sym("crates/a/src/main.rs", "main", "rust")
    caller = _sym("scripts/build.mjs", "run", "javascript")
    graph = resolve(
        [
            _file("scripts/gen.mjs", "javascript", js_main),
            _file("crates/a/src/main.rs", "rust", rs_main),
            _file(
                "scripts/build.mjs",
                "javascript",
                caller,
                refs=[
                    RawRef(
                        caller_id=caller.id,
                        path="scripts/build.mjs",
                        name="gen",
                        line=3,
                    )
                ],
                imports=[
                    Import(
                        path="scripts/build.mjs",
                        name="gen",
                        source="./gen.mjs/main",
                    )
                ],
            ),
        ]
    )
    assert {(e.caller, e.callee) for e in graph.referenced} == {
        (caller.id, js_main.id)
    }


def test_tier2_shell_call_never_reaches_another_language() -> None:
    assert languages.spec_for_path("ci/build.sh") is None
    py_exit = _sym("ops/control_flow_ops.py", "exit", "python")
    graph = resolve(
        [
            _file("ops/control_flow_ops.py", "python", py_exit),
            _file(
                "ci/build.sh",
                "bash",
                calls=[_module_call("ci/build.sh", "exit")],
            ),
        ]
    )
    assert _edges(graph) == set()
    assert graph.ambiguous == []
    assert [ext.callee for ext in graph.external] == ["exit"]


def test_tier2_shell_call_still_reaches_a_shell_function() -> None:
    tfrun = _sym("ci/utils.sh", "tfrun", "bash")
    graph = resolve(
        [
            _file("ci/utils.sh", "bash", tfrun),
            _file(
                "ci/any.sh", "bash", calls=[_module_call("ci/any.sh", "tfrun")]
            ),
        ]
    )
    assert _edges(graph) == {("ci/any.sh::<module>", tfrun.id)}


def test_gradle_dsl_word_does_not_reach_a_java_method() -> None:
    # ``id "java"`` in a plugins block: one argument, like the setter.
    java_id = replace(
        _sym(
            "src/KafkaProperties.java",
            "id",
            "java",
            "method",
            "IsolationLevel.id",
        ),
        params=[Param(name="id", type="byte")],
    )
    graph = resolve(
        [
            _file("src/KafkaProperties.java", "java", java_id),
            _file(
                "build.gradle",
                "groovy",
                calls=[_module_call("build.gradle", "id", arg_count=1)],
            ),
        ]
    )
    assert _edges(graph) == set()
    assert graph.ambiguous == []


def test_swift_call_still_reaches_a_c_function() -> None:
    c_fn = replace(
        _sym("lite/c/c_api.cc", "TfLiteTensorByteSize", "cpp"),
        params=[Param(name="tensor", type="const TfLiteTensor*")],
    )
    swift = _sym("lite/swift/Tensor.swift", "byteCount", "swift", "method")
    graph = resolve(
        [
            _file("lite/c/c_api.cc", "cpp", c_fn),
            _file(
                "lite/swift/Tensor.swift",
                "swift",
                swift,
                calls=[_call(swift, "TfLiteTensorByteSize", arg_count=1)],
            ),
        ]
    )
    assert _edges(graph) == {(swift.id, c_fn.id)}


def test_swift_call_never_reaches_a_cpp_method() -> None:
    # ``Bundle.module.path(forResource:ofType:)`` is Foundation's; the
    # only in-repo ``path`` is a C++ test fixture's method.
    cpp_path = replace(
        _sym(
            "core/save_dataset_op_test.cc",
            "path",
            "cpp",
            "method",
            "SaveDatasetV2Params.path",
        ),
        params=[Param(name="p", type="string")],
    )
    swift = _sym("lite/swift/Tests/ModelTests.swift", "setUp", "swift")
    graph = resolve(
        [
            _file("core/save_dataset_op_test.cc", "cpp", cpp_path),
            _file(
                swift.path,
                "swift",
                swift,
                calls=[_call(swift, "path", receiver="module", arg_count=2)],
            ),
        ]
    )
    assert _edges(graph) == set()
    assert graph.ambiguous == []


def test_swift_receiver_call_never_reaches_a_c_function() -> None:
    # ``buffer.deallocate()`` is the pointer's own method; a C function
    # is imported as a global and called bare.
    c_fn = _sym("core/gpu/gpu_device.cc", "deallocate", "cpp")
    swift = _sym("lite/swift/Interpreter.swift", "name", "swift")
    graph = resolve(
        [
            _file("core/gpu/gpu_device.cc", "cpp", c_fn),
            _file(
                swift.path,
                "swift",
                swift,
                calls=[
                    _call(swift, "deallocate", receiver="buffer", arg_count=0)
                ],
            ),
        ]
    )
    assert _edges(graph) == set()


def test_swift_unnamed_receiver_call_never_reaches_a_c_function() -> None:
    # ``UnsafeMutablePointer<CChar>.allocate(capacity:)``: the Tier-2
    # extractor keeps only ``.allocate`` as the text, with no receiver.
    c_fn = _sym("core/gpu/gpu_device.cc", "allocate", "cpp")
    swift = _sym("lite/swift/Interpreter.swift", "name", "swift")
    call = RawCall(
        caller_id=swift.id,
        path=swift.path,
        text=".allocate",
        name="allocate",
        line=3,
    )
    graph = resolve(
        [
            _file("core/gpu/gpu_device.cc", "cpp", c_fn),
            _file(swift.path, "swift", swift, calls=[call]),
        ]
    )
    assert _edges(graph) == set()


def test_throw_does_not_resolve_to_another_languages_type() -> None:
    java_error = _sym("src/Error.java", "Error", "java", "class")
    handler = _sym("static/livereload.js", "handle", "javascript")
    graph = resolve(
        [
            _file("src/Error.java", "java", java_error),
            _file(
                "static/livereload.js",
                "javascript",
                handler,
                throws=[
                    RawThrow(
                        caller_id=handler.id,
                        path="static/livereload.js",
                        text="Error",
                        name="Error",
                        line=3,
                    )
                ],
            ),
        ]
    )
    assert graph.throws == []
    assert graph.throws_ambiguous == []


def test_every_ambiguous_row_is_two_plus_reachable_candidates() -> None:
    names = ("update", "Base", "Failure")
    defs = [
        ("x.py", "python"),
        ("y.py", "python"),
        ("x.cc", "cpp"),
        ("X.java", "java"),
        ("X.kt", "kotlin"),
        ("x.ts", "typescript"),
        ("x.sh", "bash"),
    ]
    files = []
    for path, lang in defs:
        syms = [
            _sym(path, n, lang, "class" if n != "update" else "function")
            for n in names
        ]
        files.append(_file(path, lang, *syms))
    for path, lang in defs:
        caller = _sym(f"use_{path}", "go", lang)
        files.append(
            _file(
                caller.path,
                lang,
                caller,
                calls=[_call(caller, "update", receiver="o")],
                heritage=[
                    RawHeritage(
                        subtype_id=caller.id,
                        path=caller.path,
                        text="Base",
                        name="Base",
                        receiver=None,
                        relation="extends",
                        line=1,
                    )
                ],
                throws=[
                    RawThrow(
                        caller_id=caller.id,
                        path=caller.path,
                        text="Failure",
                        name="Failure",
                        line=6,
                    )
                ],
            )
        )
    graph = resolve(files)
    lang_of = {s.id: s.language for f in files for s in f.symbols}
    rows = graph.ambiguous + graph.heritage_ambiguous + graph.throws_ambiguous
    assert rows
    for caller, _name, cands in rows:
        caller_lang = lang_of[caller]
        assert len(cands) >= 2, (caller, cands)
        assert {lang_of[c] for c in cands} == {caller_lang}, (caller, cands)
