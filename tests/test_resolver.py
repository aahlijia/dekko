"""End-to-end resolution tests over the language fixtures."""

from pathlib import Path

from dekko.cli import map_repository
from dekko.model import FileMap, Import, Param, RawCall, RawRef, Symbol
from dekko.resolver import resolve

FIXTURES = Path(__file__).parent / "fixtures"


def _fn(
    path: str, name: str, qual: str | None = None, line: int = 1
) -> Symbol:
    qual = qual or name
    return Symbol(
        id=f"{path}::{qual}",
        name=name,
        qualname=qual,
        kind="method" if "." in qual else "function",
        path=path,
        language="python",
        start_line=line,
        end_line=line + 1,
    )


def _edges(root: Path) -> set[tuple[str, str]]:
    files, _ = map_repository(
        root,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    graph = resolve(files)
    return {(e.caller, e.callee) for e in graph.edges}


def test_python_resolution() -> None:
    edges = _edges(FIXTURES / "python")
    assert ("main.py::run", "util.py::helper") in edges
    assert ("main.py::run", "util.py::Config") in edges
    assert ("main.py::run", "util.py::Config.validate") in edges
    assert ("util.py::Config.validate", "util.py::Config.load") in edges
    assert ("main.py::<module>", "main.py::run") in edges


def test_rust_resolution() -> None:
    edges = _edges(FIXTURES / "rust")
    assert ("main.rs::main", "lib.rs::Point.new") in edges
    assert ("main.rs::main", "lib.rs::Point.dist") in edges
    assert ("main.rs::main", "lib.rs::norm") in edges
    assert ("lib.rs::Point.dist", "lib.rs::norm") in edges


def test_common_name_resolves_same_file_not_ambiguous() -> None:
    # ``run`` is defined in many files; a same-file call must still bind
    # to the local definition, not become ambiguous.
    files = [
        FileMap(
            path=f"mod{i}.py",
            language="python",
            symbols=[_fn(f"mod{i}.py", "run")],
        )
        for i in range(20)
    ]
    files.append(
        FileMap(
            path="caller.py",
            language="python",
            symbols=[
                _fn("caller.py", "run"),
                _fn("caller.py", "entry", line=5),
            ],
            calls=[
                RawCall(
                    caller_id="caller.py::entry",
                    path="caller.py",
                    text="run",
                    name="run",
                    line=6,
                )
            ],
        )
    )
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert ("caller.py::entry", "caller.py::run") in edges
    assert ("caller.py::entry", "mod0.py::run") not in edges
    assert graph.ambiguous == []


def test_stdlib_call_resolves_external_despite_name_collision() -> None:
    # ``subprocess.run(...)`` must resolve as external even though this
    # repo (like dekko's own src/) defines ``def run(...)`` in many
    # files: the receiver is bound to a non-repo import, so the bare
    # "run" name lookup must never run and the call must not fall into
    # the unqueryable ambiguous bucket.
    files = [
        FileMap(
            path=f"mod{i}.py",
            language="python",
            symbols=[_fn(f"mod{i}.py", "run")],
        )
        for i in range(3)
    ]
    files.append(
        FileMap(
            path="caller.py",
            language="python",
            symbols=[_fn("caller.py", "entry")],
            calls=[
                RawCall(
                    caller_id="caller.py::entry",
                    path="caller.py",
                    text="subprocess.run",
                    name="run",
                    receiver="subprocess",
                    line=2,
                )
            ],
            imports=[
                Import(
                    path="caller.py",
                    name="subprocess",
                    source="subprocess",
                )
            ],
        )
    )
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    externals = {ext.callee for ext in graph.external}
    assert "subprocess.run" in externals
    assert graph.ambiguous == []
    assert not any(callee.endswith("::run") for _, callee in edges)


def test_self_container_resolves_with_like_named_elsewhere() -> None:
    # ``self.h()`` resolves to the calling class's method even when ``h``
    # exists elsewhere in the repo.
    cls = FileMap(
        path="c.py",
        language="python",
        symbols=[
            _fn("c.py", "C", "C"),
            _fn("c.py", "h", "C.h", line=2),
            _fn("c.py", "m", "C.m", line=4),
        ],
        calls=[
            RawCall(
                caller_id="c.py::C.m",
                path="c.py",
                text="self.h",
                name="h",
                receiver="self",
                line=5,
            )
        ],
    )
    other = FileMap(
        path="other.py", language="python", symbols=[_fn("other.py", "h")]
    )
    graph = resolve([cls, other])
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert ("c.py::C.m", "c.py::C.h") in edges


def test_same_file_step_ignores_receiver_on_unrelated_method_call() -> None:
    # Known limitation (design doc item #6, investigated as part of the
    # claude-buddy Task 3 "callee anomaly" — that case turned out to be
    # a correct callee list, not a resolver bug, but reading the
    # resolution ladder surfaced this real, narrower gap): the
    # same-file step in ``_pick_candidate`` — ``if len(same_file) == 1:
    # return same_file[0]`` — matches purely by bare name in the file,
    # with no check of the call's ``receiver``. A bare ``loadHistory()``
    # call correctly binds to the same-file free function; a
    # *receiver-qualified* call on an unrelated object that happens to
    # share the name (``o.loadHistory()``, ``o`` being some other
    # type — no type inference is available to tell them apart)
    # incorrectly binds to the very same free function via this step.
    # This test documents *today's actual behavior* as a known,
    # intentional limitation rather than a silent gap — it is not
    # asserting desired behavior, and flipping it would trade a false
    # positive here for a false negative on legitimate same-file calls
    # through a local variable of the right type (see design doc item
    # #6's Effort/Risk section).
    fm = FileMap(
        path="c.py",
        language="python",
        symbols=[
            _fn("c.py", "loadHistory", line=1),
            _fn("c.py", "Other", "Other", line=4),
            _fn("c.py", "entry", line=7),
        ],
        calls=[
            RawCall(
                caller_id="c.py::entry",
                path="c.py",
                text="loadHistory",
                name="loadHistory",
                line=8,
            ),
            RawCall(
                caller_id="c.py::entry",
                path="c.py",
                text="o.loadHistory",
                name="loadHistory",
                receiver="o",
                line=9,
            ),
        ],
    )
    graph = resolve([fm])
    edges = {(e.caller, e.callee) for e in graph.edges}
    # The bare call correctly resolves to the same-file function.
    assert ("c.py::entry", "c.py::loadHistory") in edges
    # The receiver-qualified call on an unrelated object also lands on
    # the same edge today — a real false positive, not a fix target of
    # this pass. If this assertion ever starts failing because the
    # same-file step was tightened to check the receiver, update this
    # test to match the new (stricter, correct) behavior.
    assert graph.ambiguous == []


def test_external_calls_recorded() -> None:
    files, _ = map_repository(
        FIXTURES / "rust",
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    graph = resolve(files)
    externals = {ext.callee for ext in graph.external}
    assert any("sqrt" in text for text in externals)
    assert all(ext.caller for ext in graph.external)
    assert all(line > 0 for ext in graph.external for line in ext.lines)


# --- bug #2(b): bare-reference (pass-by-value) resolution -----------------


def test_reference_resolves_into_referenced_not_calls() -> None:
    # A callback passed by reference (not invoked at that site) must
    # resolve into graph.referenced/_in/_out and nowhere near
    # edges/calls_in/calls_out — the two are deliberately kept apart
    # (see resolver.py's module docstring).
    handler = _fn("a.ts", "handleClick")
    wire_up = _fn("b.ts", "wireUp")
    files = [
        FileMap("a.ts", "typescript", symbols=[handler]),
        FileMap(
            "b.ts",
            "typescript",
            symbols=[wire_up],
            refs=[
                RawRef(
                    caller_id=wire_up.id,
                    path="b.ts",
                    name="handleClick",
                    line=5,
                )
            ],
        ),
    ]
    graph = resolve(files)
    assert (wire_up.id, handler.id) in {
        (e.caller, e.callee) for e in graph.referenced
    }
    assert graph.referenced_in[handler.id] == [wire_up.id]
    assert graph.referenced_out[wire_up.id] == [handler.id]
    # Never conflated with the call graph.
    assert graph.edges == []
    assert handler.id not in graph.calls_in
    assert wire_up.id not in graph.calls_out


def test_unresolved_reference_is_silently_dropped() -> None:
    # No candidate in the repo for the referenced name: dropped, not
    # recorded as ambiguous (references have no "ambiguous" concept).
    wire_up = _fn("b.ts", "wireUp")
    files = [
        FileMap(
            "b.ts",
            "typescript",
            symbols=[wire_up],
            refs=[
                RawRef(
                    caller_id=wire_up.id,
                    path="b.ts",
                    name="notDefinedAnywhere",
                    line=3,
                )
            ],
        )
    ]
    graph = resolve(files)
    assert graph.referenced == []
    assert graph.ambiguous == []


# --- aliased-import calls (Work Package C) --------------------------------


def test_aliased_import_call_resolves_to_real_target() -> None:
    # ``import { resolveBug as resolveBugMemory } from "target"`` then
    # calling ``resolveBugMemory(...)`` — the raw callee text is the
    # local alias, which never appears in the by-declared-name index,
    # so this must not fall through to ``external``: it should recover
    # the pre-alias name from the import source and resolve for real.
    real = _fn("target.py", "resolveBug")
    caller = _fn("caller.py", "entry")
    files = [
        FileMap("target.py", "python", symbols=[real]),
        FileMap(
            "caller.py",
            "python",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.py",
                    text="resolveBugMemory",
                    name="resolveBugMemory",
                    line=3,
                )
            ],
            imports=[
                Import(
                    path="caller.py",
                    name="resolveBugMemory",
                    source="target.resolveBug",
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, real.id) in edges
    assert graph.calls_in[real.id] == [caller.id]
    assert graph.external == []
    assert graph.ambiguous == []


def test_aliased_import_to_external_package_stays_external() -> None:
    # The alias happens to share its pre-alias name with an unrelated
    # repo symbol, but the import source names a package outside the
    # repo (no repo file's stem appears in it) — must still resolve to
    # ``external``, never misattributed to the unrelated repo symbol.
    unrelated = _fn("target.py", "resolveBug")
    caller = _fn("caller.py", "entry")
    files = [
        FileMap("target.py", "python", symbols=[unrelated]),
        FileMap(
            "caller.py",
            "python",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.py",
                    text="resolveBugMemory",
                    name="resolveBugMemory",
                    line=3,
                )
            ],
            imports=[
                Import(
                    path="caller.py",
                    name="resolveBugMemory",
                    source="some_npm_package.resolveBug",
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    externals = {ext.callee for ext in graph.external}
    assert (caller.id, unrelated.id) not in edges
    assert "resolveBugMemory" in externals
    assert graph.ambiguous == []


def test_aliased_import_call_ambiguous_when_multiple_candidates() -> None:
    # Two repo-wide symbols share the pre-alias name and the import
    # source doesn't narrow to just one of them (both files have the
    # same stem) — must land in ``ambiguous``, not be guessed.
    real_a = _fn("a/target.py", "resolveBug")
    real_b = _fn("b/target.py", "resolveBug")
    caller = _fn("caller.py", "entry")
    files = [
        FileMap("a/target.py", "python", symbols=[real_a]),
        FileMap("b/target.py", "python", symbols=[real_b]),
        FileMap(
            "caller.py",
            "python",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.py",
                    text="resolveBugMemory",
                    name="resolveBugMemory",
                    line=3,
                )
            ],
            imports=[
                Import(
                    path="caller.py",
                    name="resolveBugMemory",
                    source="target.resolveBug",
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, real_a.id) not in edges
    assert (caller.id, real_b.id) not in edges
    assert len(graph.ambiguous) == 1
    amb_caller, amb_name, amb_cands = graph.ambiguous[0]
    assert amb_caller == caller.id
    assert amb_name == "resolveBugMemory"
    assert set(amb_cands) == {real_a.id, real_b.id}


def test_aliased_import_reference_resolves_to_real_target() -> None:
    # Parity with the call case: a bare-value reference through a
    # local import alias must resolve into referenced/_in/_out, not
    # be silently dropped just because the alias misses the by-name
    # index.
    real = _fn("target.py", "resolveBug")
    wire_up = _fn("caller.ts", "wireUp")
    files = [
        FileMap("target.py", "python", symbols=[real]),
        FileMap(
            "caller.ts",
            "typescript",
            symbols=[wire_up],
            refs=[
                RawRef(
                    caller_id=wire_up.id,
                    path="caller.ts",
                    name="resolveBugMemory",
                    line=4,
                )
            ],
            imports=[
                Import(
                    path="caller.ts",
                    name="resolveBugMemory",
                    source="target/resolveBug",
                )
            ],
        ),
    ]
    graph = resolve(files)
    assert (wire_up.id, real.id) in {
        (e.caller, e.callee) for e in graph.referenced
    }
    assert graph.referenced_in[real.id] == [wire_up.id]
    assert graph.referenced_out[wire_up.id] == [real.id]


def test_cpp_call_disambiguated_via_whole_file_include() -> None:
    # C++ import-hint fix (1.5-remainder, part 1) — reproduces the
    # tensorflow ``rewrite_utils.cc``/``rewrite_utils_test.cc`` gtest
    # pair from investigation-1.5-cpp-gtest-affected.md as a minimal
    # fixture: two repo-wide same-named free functions (no receiver,
    # no self/typed-param/same-file evidence — every earlier ladder
    # step fails), disambiguated only by which header the caller's
    # file ``#include``s. Before the whole-file-include fallback, this
    # always landed in ``ambiguous`` — C++'s ``#include`` has no
    # per-symbol binding, so the ordinary name-keyed import hint could
    # never fire.
    real = _fn("tensorflow/core/data/rewrite_utils.cc", "GetGrapplerItem")
    unrelated = _fn("other/pkg/helpers.cc", "GetGrapplerItem")
    caller = _fn(
        "tensorflow/core/data/rewrite_utils_test.cc",
        "TEST",
        line=86,
    )
    files = [
        FileMap(
            "tensorflow/core/data/rewrite_utils.cc",
            "cpp",
            symbols=[real],
        ),
        FileMap("other/pkg/helpers.cc", "cpp", symbols=[unrelated]),
        FileMap(
            "tensorflow/core/data/rewrite_utils_test.cc",
            "cpp",
            symbols=[caller],
            imports=[
                Import(
                    path="tensorflow/core/data/rewrite_utils_test.cc",
                    name="rewrite_utils",
                    source="tensorflow/core/data/rewrite_utils.h",
                )
            ],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="tensorflow/core/data/rewrite_utils_test.cc",
                    text="GetGrapplerItem",
                    name="GetGrapplerItem",
                    line=90,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, real.id) in edges
    assert (caller.id, unrelated.id) not in edges
    assert graph.ambiguous == []


def test_cpp_call_stays_ambiguous_when_no_include_matches() -> None:
    # Regression guard: the whole-file-include fallback must not
    # over-match — when neither candidate's file is actually included
    # by the caller's file, the call stays genuinely ambiguous rather
    # than guessing.
    a_impl = _fn("a/impl.cc", "Frobnicate")
    b_impl = _fn("b/impl.cc", "Frobnicate")
    caller = _fn("caller.cc", "Run")
    files = [
        FileMap("a/impl.cc", "cpp", symbols=[a_impl]),
        FileMap("b/impl.cc", "cpp", symbols=[b_impl]),
        FileMap(
            "caller.cc",
            "cpp",
            symbols=[caller],
            imports=[
                Import(
                    path="caller.cc",
                    name="unrelated",
                    source="c/unrelated.h",
                )
            ],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.cc",
                    text="Frobnicate",
                    name="Frobnicate",
                    line=5,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, a_impl.id) not in edges
    assert (caller.id, b_impl.id) not in edges
    assert len(graph.ambiguous) == 1


def test_non_cpp_call_unaffected_by_whole_file_include_fallback() -> None:
    # The fallback is gated to ``_WHOLE_FILE_IMPORT_LANGUAGES`` — a
    # Python file with an unrelated import whose source happens to
    # share a path segment with an ambiguous candidate's file must
    # still land in ``ambiguous``, not be swept up by a fallback meant
    # only for C/C++.
    a_impl = _fn("pkg/thing.py", "process")
    b_impl = _fn("other/thing.py", "process")
    caller = _fn("caller.py", "run")
    files = [
        FileMap("pkg/thing.py", "python", symbols=[a_impl]),
        FileMap("other/thing.py", "python", symbols=[b_impl]),
        FileMap(
            "caller.py",
            "python",
            symbols=[caller],
            imports=[
                Import(
                    path="caller.py",
                    name="unrelated_name",
                    source="pkg.thing",
                )
            ],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.py",
                    text="process",
                    name="process",
                    line=3,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, a_impl.id) not in edges
    assert (caller.id, b_impl.id) not in edges
    assert len(graph.ambiguous) == 1


def test_ambiguous_reference_name_is_dropped_not_guessed() -> None:
    # Two same-named repo-wide candidates, no same-file/import hint to
    # disambiguate: dropped rather than guessed (mirrors how an
    # ambiguous call is never guessed into an edge).
    a_handler = _fn("a.ts", "handler")
    b_handler = _fn("b.ts", "handler")
    wire_up = _fn("c.ts", "wireUp")
    files = [
        FileMap("a.ts", "typescript", symbols=[a_handler]),
        FileMap("b.ts", "typescript", symbols=[b_handler]),
        FileMap(
            "c.ts",
            "typescript",
            symbols=[wire_up],
            refs=[
                RawRef(
                    caller_id=wire_up.id,
                    path="c.ts",
                    name="handler",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    assert graph.referenced == []
    assert graph.referenced_in == {}


# --- bug #2: undercounted callers through typed vars/params and ----------
# --- ``new X()`` construction ---------------------------------------------


def test_typed_parameter_call_resolves_to_declared_type_method() -> None:
    # cline's headline bug: a call through a parameter explicitly typed
    # as the target class (``controller: Controller``) must resolve to
    # that class's method — not fall into ``ambiguous`` just because
    # another same-named method exists elsewhere in the repo, and not
    # get guessed via the (unrelated) same-file step either.
    right = _fn("a.ts", "initTask", "Controller.initTask")
    wrong = _fn("b.ts", "initTask", "Other.initTask")
    caller = Symbol(
        id="caller.ts::setup",
        name="setup",
        qualname="setup",
        kind="function",
        path="caller.ts",
        language="typescript",
        params=[Param(name="controller", type="Controller")],
    )
    files = [
        FileMap("a.ts", "typescript", symbols=[right]),
        FileMap("b.ts", "typescript", symbols=[wrong]),
        FileMap(
            "caller.ts",
            "typescript",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.ts",
                    text="controller.initTask",
                    name="initTask",
                    receiver="controller",
                    line=4,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, right.id) in edges
    assert (caller.id, wrong.id) not in edges
    assert graph.ambiguous == []


def test_typed_parameter_match_strips_generic_wrapper() -> None:
    # A declared type dressed in a common wrapper (``Optional[X]``,
    # ``X | undefined``) must still narrow to the bare class name.
    right = _fn("a.ts", "initTask", "Controller.initTask")
    wrong = _fn("b.ts", "initTask", "Other.initTask")
    caller = Symbol(
        id="caller.ts::setup",
        name="setup",
        qualname="setup",
        kind="function",
        path="caller.ts",
        language="typescript",
        params=[Param(name="controller", type="Controller | undefined")],
    )
    files = [
        FileMap("a.ts", "typescript", symbols=[right]),
        FileMap("b.ts", "typescript", symbols=[wrong]),
        FileMap(
            "caller.ts",
            "typescript",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.ts",
                    text="controller.initTask",
                    name="initTask",
                    receiver="controller",
                    line=4,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, right.id) in edges


def test_untyped_parameter_falls_back_to_existing_ladder() -> None:
    # No declared type on the parameter: unchanged behavior — falls
    # through to the same-file step, exactly like before this fix.
    same_file_fn = _fn("caller.ts", "initTask", line=1)
    caller = Symbol(
        id="caller.ts::setup",
        name="setup",
        qualname="setup",
        kind="function",
        path="caller.ts",
        language="typescript",
        params=[Param(name="controller", type=None)],
        start_line=3,
    )
    files = [
        FileMap(
            "caller.ts",
            "typescript",
            symbols=[same_file_fn, caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.ts",
                    text="controller.initTask",
                    name="initTask",
                    receiver="controller",
                    line=4,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, same_file_fn.id) in edges


def test_construction_credits_explicit_constructor() -> None:
    # ``new Controller(...)`` resolves to the class today; it must
    # also credit the class's own explicit constructor method (cline's
    # ``Controller.constructor`` read fan-in 0 despite a real
    # construction call site).
    cls = Symbol(
        id="controller.ts::Controller",
        name="Controller",
        qualname="Controller",
        kind="class",
        path="controller.ts",
        language="typescript",
    )
    ctor = Symbol(
        id="controller.ts::Controller.constructor",
        name="constructor",
        qualname="Controller.constructor",
        kind="method",
        path="controller.ts",
        language="typescript",
    )
    caller = _fn("caller.ts", "setup")
    files = [
        FileMap("controller.ts", "typescript", symbols=[cls, ctor]),
        FileMap(
            "caller.ts",
            "typescript",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.ts",
                    text="new Controller",
                    name="Controller",
                    line=3,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, cls.id) in edges
    assert (caller.id, ctor.id) in edges
    assert graph.calls_in[ctor.id] == [caller.id]


def test_construction_without_explicit_constructor_unchanged() -> None:
    # No explicit constructor/``__init__`` defined: behavior stays
    # exactly as it was before this fix — a single edge to the class.
    cls = _fn("util.py", "Config", "Config")
    caller = _fn("main.py", "run")
    files = [
        FileMap("util.py", "python", symbols=[cls]),
        FileMap(
            "main.py",
            "python",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="main.py",
                    text="Config",
                    name="Config",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert edges == {(caller.id, cls.id)}


def test_python_construction_credits_init() -> None:
    cls = Symbol(
        id="util.py::Config",
        name="Config",
        qualname="Config",
        kind="class",
        path="util.py",
        language="python",
    )
    init = Symbol(
        id="util.py::Config.__init__",
        name="__init__",
        qualname="Config.__init__",
        kind="method",
        path="util.py",
        language="python",
    )
    caller = _fn("main.py", "run")
    files = [
        FileMap("util.py", "python", symbols=[cls, init]),
        FileMap(
            "main.py",
            "python",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="main.py",
                    text="Config",
                    name="Config",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, cls.id) in edges
    assert (caller.id, init.id) in edges


def test_java_constructor_same_name_pair_not_ambiguous() -> None:
    # Java's constructor_declaration shares its bare name with its own
    # class — ``new Foo(...)`` used to land the class + its own
    # constructor in ``ambiguous`` (2 same-named candidates), zeroing
    # out fan-in for both instead of resolving cleanly.
    cls = Symbol(
        id="Foo.java::Foo",
        name="Foo",
        qualname="Foo",
        kind="class",
        path="Foo.java",
        language="java",
    )
    ctor = Symbol(
        id="Foo.java::Foo.Foo",
        name="Foo",
        qualname="Foo.Foo",
        kind="method",
        path="Foo.java",
        language="java",
    )
    caller = _fn("Caller.java", "setup")
    files = [
        FileMap("Foo.java", "java", symbols=[cls, ctor]),
        FileMap(
            "Caller.java",
            "java",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="Caller.java",
                    text="new Foo",
                    name="Foo",
                    line=3,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, cls.id) in edges
    assert (caller.id, ctor.id) in edges
    assert graph.ambiguous == []


def test_real_ambiguity_between_two_unrelated_classes_still_ambiguous() -> (
    None
):
    # Two genuinely unrelated same-named classes (not a class/its-own-
    # constructor pair): must still land in ``ambiguous``, not be
    # guessed via the pair-collapse shortcut.
    a = Symbol(
        id="a.py::Dup",
        name="Dup",
        qualname="Dup",
        kind="class",
        path="a.py",
        language="python",
    )
    b = Symbol(
        id="b.py::Dup",
        name="Dup",
        qualname="Dup",
        kind="class",
        path="b.py",
        language="python",
    )
    caller = _fn("caller.py", "entry")
    files = [
        FileMap("a.py", "python", symbols=[a]),
        FileMap("b.py", "python", symbols=[b]),
        FileMap(
            "caller.py",
            "python",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.py",
                    text="Dup",
                    name="Dup",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert edges == set()
    assert len(graph.ambiguous) == 1


# --- Resolver fan-in noise (investigation-1.2-resolver-fanin.md) ---
#
# cline's ``trim``/``expect``/``describe``/``interface String`` hotspots
# (reported fan-in 1,404/603/676/548) turned out not to be a merge bug —
# ``_pick_candidate`` never guesses among 2+ genuinely ambiguous
# candidates — but a false-positive *single*-candidate resolution: a
# repo defining exactly one symbol sharing a name with a language
# built-in/ambient global had every unrelated built-in/global call site
# silently credited to it. These tests cover the three shapes the
# investigation confirmed live against cline, plus regression guards
# that legitimate same-shape resolutions are unaffected.


def test_receiver_qualified_builtin_method_name_not_guessed() -> None:
    # cline's headline case: exactly one repo-wide ``trim`` symbol; an
    # unrelated call through an untyped local variable
    # (``opts.config.trim()``) is really JS's built-in
    # ``String.prototype.trim()``. Must not be guessed into the repo
    # symbol's fan-in.
    trim_fn = _fn("util.ts", "trim")
    caller = _fn("caller.ts", "run")
    files = [
        FileMap("util.ts", "typescript", symbols=[trim_fn]),
        FileMap(
            "caller.ts",
            "typescript",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.ts",
                    text="opts.config.trim",
                    name="trim",
                    receiver="opts.config",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, trim_fn.id) not in edges
    assert graph.calls_in.get(trim_fn.id, []) == []
    assert len(graph.ambiguous) == 1


def test_bare_call_to_same_file_builtin_named_function_still_resolves() -> (
    None
):
    # Regression guard: the noise guard only applies to
    # receiver-qualified calls and known ambient-global bare names — a
    # genuinely local bare call to a same-file function sharing a
    # built-in method name (dekko's real cline callers: bare
    # ``trim(value)``, no receiver) must still resolve normally.
    trim_fn = _fn("util.ts", "trim")
    caller = _fn("util.ts", "run", line=5)
    files = [
        FileMap(
            "util.ts",
            "typescript",
            symbols=[trim_fn, caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="util.ts",
                    text="trim",
                    name="trim",
                    line=6,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, trim_fn.id) in edges
    assert graph.ambiguous == []


def test_exact_self_receiver_builtin_named_method_still_resolves() -> None:
    # ``this.trim()`` (bare self/this receiver, no further chain) on a
    # class that defines its own ``trim`` method must still resolve via
    # ``_self_container`` — the noise guard only rejects a
    # *multi-segment* chain rooted at self/this, not a direct
    # sibling-method call.
    cls = FileMap(
        path="c.ts",
        language="typescript",
        symbols=[
            _fn("c.ts", "C", "C"),
            _fn("c.ts", "trim", "C.trim", line=2),
            _fn("c.ts", "m", "C.m", line=4),
        ],
        calls=[
            RawCall(
                caller_id="c.ts::C.m",
                path="c.ts",
                text="this.trim",
                name="trim",
                receiver="this",
                line=5,
            )
        ],
    )
    graph = resolve([cls])
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert ("c.ts::C.m", "c.ts::C.trim") in edges


def test_multi_segment_self_chain_builtin_call_not_guessed() -> None:
    # A property chain merely rooted at ``this``
    # (``this.options.value.trim()``) is a value access, not a
    # sibling-method call — must not be exempted from the noise guard
    # just because the chain's first token is ``this``. Confirmed live
    # against cline's ``this.options.authToken?.trim()``.
    trim_fn = _fn("util.ts", "trim")
    cls = _fn("c.ts", "C", "C")
    caller = Symbol(
        id="c.ts::C.m",
        name="m",
        qualname="C.m",
        kind="method",
        path="c.ts",
        language="typescript",
    )
    files = [
        FileMap("util.ts", "typescript", symbols=[trim_fn]),
        FileMap(
            "c.ts",
            "typescript",
            symbols=[cls, caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="c.ts",
                    text="this.options.value.trim",
                    name="trim",
                    receiver="this.options.value",
                    line=6,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, trim_fn.id) not in edges


def test_receiver_qualified_schema_builder_method_not_guessed() -> None:
    # Track H's documented residual gap (investigation-1.2-resolver-
    # fanin.md): a repo's one ``describe`` symbol is an unrelated
    # internal helper; a Zod schema chain call like
    # ``z.string().describe("...")`` must not be guessed into its
    # fan-in just because ``describe`` is otherwise unique repo-wide —
    # confirmed live against cline (fan-in 60, all Zod ``.describe()``
    # calls through untyped schema-builder receivers).
    describe_fn = _fn("definitions.ts", "describe")
    caller = _fn("schema.ts", "run")
    files = [
        FileMap("definitions.ts", "typescript", symbols=[describe_fn]),
        FileMap(
            "schema.ts",
            "typescript",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="schema.ts",
                    text="z.string().describe",
                    name="describe",
                    receiver="z.string()",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, describe_fn.id) not in edges
    assert graph.calls_in.get(describe_fn.id, []) == []
    assert len(graph.ambiguous) == 1


def test_receiver_qualified_rust_std_method_not_guessed() -> None:
    # zed's headline finding, round-09 §2.1 part B: exactly one
    # repo-wide ``then`` symbol (an unrelated CI-tool crate's
    # ``PathContextCondition.then``); a call through an untyped local
    # variable is really Rust std's ``bool::then``. Must not be
    # guessed into the repo symbol's fan-in — same false-positive
    # shape ``_BUILTIN_METHOD_NAMES`` already guards for JS/TS, just
    # never extended to Rust std/prelude method names before.
    then_fn = _fn("ci_tool.rs", "then", "PathContextCondition.then")
    caller = _fn("editor.rs", "new_internal")
    files = [
        FileMap("ci_tool.rs", "rust", symbols=[then_fn]),
        FileMap(
            "editor.rs",
            "rust",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="editor.rs",
                    text="mode.then",
                    name="then",
                    receiver="mode",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, then_fn.id) not in edges
    assert graph.calls_in.get(then_fn.id, []) == []
    assert len(graph.ambiguous) == 1


def test_rust_iter_mut_not_guessed_into_unrelated_repo_symbol() -> None:
    # zed's second part-B example: ``.iter_mut()`` on an untyped
    # local must not resolve to an unrelated repo type's own
    # ``iter_mut`` method just because the name is otherwise unique.
    iter_mut_fn = _fn("atlas.rs", "iter_mut", "AtlasTextureList.iter_mut")
    caller = _fn("editor.rs", "new_internal")
    files = [
        FileMap("atlas.rs", "rust", symbols=[iter_mut_fn]),
        FileMap(
            "editor.rs",
            "rust",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="editor.rs",
                    text="highlights.iter_mut",
                    name="iter_mut",
                    receiver="highlights",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, iter_mut_fn.id) not in edges
    assert graph.calls_in.get(iter_mut_fn.id, []) == []
    assert len(graph.ambiguous) == 1


def test_explicit_type_receiver_resolves_same_file_new_collision() -> None:
    # zed's headline finding, round-09 §2.1 part A:
    # ``BufferDiff::new(...)`` written inside ``BufferDiff``'s own
    # file, which also defines an unrelated type's same-named ``new``
    # method — there's no import to key ``_import_match`` off (same
    # file) and ``same_file`` has 2 same-named candidates, so neither
    # of those steps (nor self/this, nor a typed parameter) can
    # disambiguate it. The receiver being the type's own bare name is
    # stronger evidence than any of those and must resolve
    # unambiguously — previously fell all the way through to
    # ``ambiguous`` with the full repo-wide candidate list.
    buffer_diff_type = Symbol(
        id="buffer_diff.rs::BufferDiff",
        name="BufferDiff",
        qualname="BufferDiff",
        kind="struct",
        path="buffer_diff.rs",
        language="rust",
    )
    buffer_diff_new = _fn("buffer_diff.rs", "new", "BufferDiff.new", line=5)
    other_new = _fn("buffer_diff.rs", "new", "Other.new", line=20)
    caller = _fn(
        "buffer_diff.rs",
        "new_with_base_text",
        "BufferDiff.new_with_base_text",
        line=40,
    )
    files = [
        FileMap(
            "buffer_diff.rs",
            "rust",
            symbols=[buffer_diff_type, buffer_diff_new, other_new, caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="buffer_diff.rs",
                    text="BufferDiff::new",
                    name="new",
                    receiver="BufferDiff",
                    line=41,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, buffer_diff_new.id) in edges
    assert (caller.id, other_new.id) not in edges
    assert graph.ambiguous == []


def test_explicit_type_receiver_no_unique_method_falls_through() -> None:
    # Negative control: the receiver names a real in-repo type, but
    # that type has no candidate matching ``Type.name`` — the new step
    # must decline (return None) rather than guessing, leaving the
    # rest of the ladder (here: real ambiguity) untouched.
    widget_type = Symbol(
        id="widget.rs::Widget",
        name="Widget",
        qualname="Widget",
        kind="struct",
        path="widget.rs",
        language="rust",
    )
    unrelated_a = _fn("a.rs", "build", "Foo.build")
    unrelated_b = _fn("b.rs", "build", "Bar.build")
    caller = _fn("widget.rs", "make", "Widget.make", line=10)
    files = [
        FileMap(
            "widget.rs",
            "rust",
            symbols=[widget_type, caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="widget.rs",
                    text="Widget::build",
                    name="build",
                    receiver="Widget",
                    line=11,
                )
            ],
        ),
        FileMap("a.rs", "rust", symbols=[unrelated_a]),
        FileMap("b.rs", "rust", symbols=[unrelated_b]),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, unrelated_a.id) not in edges
    assert (caller.id, unrelated_b.id) not in edges
    assert len(graph.ambiguous) == 1


def test_bare_call_shadowed_by_external_import_not_guessed() -> None:
    # A file explicitly imports ``expect`` from an external package
    # (e.g. vitest); a bare ``expect(...)`` call there always means the
    # import, never an unrelated same-named repo shim the bare-name
    # index happens to find.
    shim = _fn("test-setup.js", "expect")
    caller = _fn("spec.js", "run")
    files = [
        FileMap("test-setup.js", "javascript", symbols=[shim]),
        FileMap(
            "spec.js",
            "javascript",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="spec.js",
                    text="expect",
                    name="expect",
                    line=3,
                )
            ],
            imports=[
                Import(path="spec.js", name="expect", source="vitest"),
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, shim.id) not in edges
    assert len(graph.ambiguous) == 1


def test_bare_call_to_ambient_global_name_not_guessed_without_import() -> None:
    # Vitest/jest-style ``globals: true`` injects ``describe``/
    # ``expect``/etc. with no explicit import at all — the noise guard
    # must still reject the single-candidate guess from the name alone.
    local = _fn("helpers.ts", "describe")
    caller = _fn("spec.ts", "run")
    files = [
        FileMap("helpers.ts", "typescript", symbols=[local]),
        FileMap(
            "spec.ts",
            "typescript",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="spec.ts",
                    text="describe",
                    name="describe",
                    line=3,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, local.id) not in edges
    assert len(graph.ambiguous) == 1


def test_bare_call_to_global_type_name_not_guessed() -> None:
    # A TS ``declare global { interface String { ... } }`` augmentation
    # is not a real callable definition; a bare ``String(x)`` call
    # elsewhere means the JS global cast/constructor, never that
    # augmentation symbol. Confirmed live against cline: ``interface
    # String`` was credited with 548 calls that were actually
    # ``String(...)`` casts.
    aug = _fn("path.ts", "String", "String")
    caller = _fn("caller.ts", "run")
    files = [
        FileMap("path.ts", "typescript", symbols=[aug]),
        FileMap(
            "caller.ts",
            "typescript",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.ts",
                    text="String",
                    name="String",
                    line=3,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, aug.id) not in edges
    assert len(graph.ambiguous) == 1


def test_reference_resolution_unaffected_by_noise_guard() -> None:
    # ``_pick_candidate``'s ``repo_stems`` gate is only threaded from
    # ``_resolve_call`` — ``resolve_refs()``/``_resolve_ref`` never
    # passes it, so a bare-value *reference* (not a call) to a
    # noise-listed name resolves exactly as it did before this fix; the
    # fan-in bug this pass targets never affected the separate
    # ``referenced``/``referenced_in`` tables.
    local = _fn("helpers.ts", "describe")
    caller = _fn("spec.ts", "run")
    files = [
        FileMap("helpers.ts", "typescript", symbols=[local]),
        FileMap(
            "spec.ts",
            "typescript",
            symbols=[caller],
            refs=[
                RawRef(
                    caller_id=caller.id,
                    path="spec.ts",
                    name="describe",
                    line=3,
                )
            ],
        ),
    ]
    graph = resolve(files)
    ref_edges = {(e.caller, e.callee) for e in graph.referenced}
    assert (caller.id, local.id) in ref_edges
