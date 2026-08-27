"""End-to-end resolution tests over the language fixtures."""

import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures import TimeoutError as PoolTimeoutError
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

from dekko.core import resolver as resolver_mod
from dekko.repo_ops import map_repository
from dekko.core.model import (
    FileMap,
    Import,
    Param,
    RawCall,
    RawCatch,
    RawRef,
    RawThrow,
    Symbol,
)
from dekko.core.resolver import (
    resolve,
    resolve_catches,
    resolve_refs,
    resolve_throws,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _fn(
    path: str,
    name: str,
    qual: str | None = None,
    line: int = 1,
    language: str = "python",
) -> Symbol:
    qual = qual or name
    return Symbol(
        id=f"{path}::{qual}",
        name=name,
        qualname=qual,
        kind="method" if "." in qual else "function",
        path=path,
        language=language,
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
    handler = _fn("a.ts", "handleClick", language="typescript")
    wire_up = _fn("b.ts", "wireUp", language="typescript")
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
    real = _fn(
        "tensorflow/core/data/rewrite_utils.cc",
        "GetGrapplerItem",
        language="cpp",
    )
    unrelated = _fn("other/pkg/helpers.cc", "GetGrapplerItem", language="cpp")
    caller = _fn(
        "tensorflow/core/data/rewrite_utils_test.cc",
        "TEST",
        line=86,
        language="cpp",
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
    a_impl = _fn("a/impl.cc", "Frobnicate", language="cpp")
    b_impl = _fn("b/impl.cc", "Frobnicate", language="cpp")
    caller = _fn("caller.cc", "Run", language="cpp")
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
    right = _fn(
        "a.ts", "initTask", "Controller.initTask", language="typescript"
    )
    wrong = _fn("b.ts", "initTask", "Other.initTask", language="typescript")
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
    right = _fn(
        "a.ts", "initTask", "Controller.initTask", language="typescript"
    )
    wrong = _fn("b.ts", "initTask", "Other.initTask", language="typescript")
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
    # Round 22 cline.md §3.1: a noise-suppressed call with exactly one
    # real candidate used to land in ``ambiguous`` (indistinguishable
    # from a genuine 2+-candidate collision) — it must now route to
    # ``external`` instead, via the ``_NOISE`` sentinel.
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "opts.config.trim" in externals


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
    # Round 22 cline.md §3.1: noise-suppressed, single-candidate calls
    # now route to ``external`` via the ``_NOISE`` sentinel, not
    # ``ambiguous``.
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "z.string().describe" in externals


def test_receiver_qualified_commander_builder_method_not_guessed() -> None:
    # Master report #5 (round 11, cline): a top-level
    # ``const description = ...`` binding in an unrelated script file
    # (`publish-npm.ts`-shaped) was credited with fan-in 14, all really
    # Commander.js ``.description("...")`` builder calls on local
    # ``Command``/``program`` instances scattered across the CLI.
    # ``description`` must not be guessed into that binding's fan-in
    # just because it's otherwise unique repo-wide, same as Zod's
    # ``describe`` above.
    description_binding = _fn("script/publish-npm.ts", "description")
    caller_a = _fn("main.ts", "runCli")
    caller_b = _fn("commands/build.ts", "registerBuild")
    files = [
        FileMap(
            "script/publish-npm.ts",
            "typescript",
            symbols=[description_binding],
        ),
        FileMap(
            "main.ts",
            "typescript",
            symbols=[caller_a],
            calls=[
                RawCall(
                    caller_id=caller_a.id,
                    path="main.ts",
                    text="program.description",
                    name="description",
                    receiver="program",
                    line=3,
                )
            ],
        ),
        FileMap(
            "commands/build.ts",
            "typescript",
            symbols=[caller_b],
            calls=[
                RawCall(
                    caller_id=caller_b.id,
                    path="commands/build.ts",
                    text="cmd.description",
                    name="description",
                    receiver="cmd",
                    line=5,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller_a.id, description_binding.id) not in edges
    assert (caller_b.id, description_binding.id) not in edges
    assert graph.calls_in.get(description_binding.id, []) == []
    # Round 22 cline.md §3.1: noise-suppressed calls now route to
    # ``external``, not ``ambiguous``.
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "program.description" in externals
    assert "cmd.description" in externals


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
    # Round 22 cline.md §3.1: noise-suppressed calls now route to
    # ``external``, not ``ambiguous``.
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "mode.then" in externals


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
    # Round 22 cline.md §3.1: noise-suppressed calls now route to
    # ``external``, not ``ambiguous``.
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "highlights.iter_mut" in externals


def test_receiver_qualified_js_now_not_guessed_into_closure_local() -> None:
    # Round 23 cline.md §2.1: a closure-local
    # ``const now = () => Date.now()``-shaped repo symbol named
    # ``now`` must not absorb every unrelated ``Date.now()``/
    # ``performance.now()`` call in the repo just because it's the
    # only repo-wide ``now`` symbol (404 misattributed sites live).
    now_fn = _fn("timer.ts", "now")
    caller = _fn("caller.ts", "run")
    files = [
        FileMap("timer.ts", "typescript", symbols=[now_fn]),
        FileMap(
            "caller.ts",
            "typescript",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.ts",
                    text="Date.now",
                    name="now",
                    receiver="Date",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, now_fn.id) not in edges
    assert graph.calls_in.get(now_fn.id, []) == []
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "Date.now" in externals


def test_receiver_qualified_js_has_not_guessed_into_repo_symbol() -> None:
    # Round 23 cline.md §2.1: ``Map.prototype.has``/
    # ``Set.prototype.has`` calls through an untyped local must not be
    # guessed into an unrelated repo-defined ``has`` just because the
    # name is otherwise unique repo-wide (352 -> 436 misattributed
    # sites vs. 0 credible in the live repro).
    has_fn = _fn("registry.ts", "has")
    caller = _fn("caller.ts", "run")
    files = [
        FileMap("registry.ts", "typescript", symbols=[has_fn]),
        FileMap(
            "caller.ts",
            "typescript",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.ts",
                    text="seen.has",
                    name="has",
                    receiver="seen",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, has_fn.id) not in edges
    assert graph.calls_in.get(has_fn.id, []) == []
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "seen.has" in externals


def test_receiver_qualified_java_assertj_istrue_not_guessed() -> None:
    # Round 23 spring-boot.md §2.1: a single real
    # ``ResolvedDockerHost.isTrue`` caller had its fan-in inflated to
    # 1,103 by unrelated AssertJ ``assertThat(x).isTrue()`` assertion
    # chain calls elsewhere in the test suite -- ~1,100x inflation from
    # a single denylist gap (no Java-idiom denylist existed at all
    # before round 23).
    is_true_fn = _fn(
        "ResolvedDockerHost.java",
        "isTrue",
        "ResolvedDockerHost.isTrue",
        language="java",
    )
    caller = _fn(
        "SomeTest.java",
        "verify",
        language="java",
    )
    files = [
        FileMap(
            "ResolvedDockerHost.java",
            "java",
            symbols=[is_true_fn],
        ),
        FileMap(
            "SomeTest.java",
            "java",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="SomeTest.java",
                    text="assertThat(result).isTrue",
                    name="isTrue",
                    receiver="assertThat(result)",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, is_true_fn.id) not in edges
    assert graph.calls_in.get(is_true_fn.id, []) == []
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "assertThat(result).isTrue" in externals


def test_receiver_qualified_generic_builder_build_not_guessed() -> None:
    # Round 23 spring-boot.md §2.2: a repo-defined ``Builder.build``
    # read 43 real callers plus 1,131 additional ambiguous-but-
    # uncounted sites from unrelated builder types elsewhere in the
    # codebase -- a receiver-qualified ``.build()`` on an untyped local
    # must not be guessed into a same-named repo builder just because
    # the bare name happens to be otherwise unique.
    build_fn = _fn(
        "RequestBuilder.java",
        "build",
        "RequestBuilder.build",
        language="java",
    )
    caller = _fn("Other.java", "run", language="java")
    files = [
        FileMap("RequestBuilder.java", "java", symbols=[build_fn]),
        FileMap(
            "Other.java",
            "java",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="Other.java",
                    text="someOtherBuilder.build",
                    name="build",
                    receiver="someOtherBuilder",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, build_fn.id) not in edges
    assert graph.calls_in.get(build_fn.id, []) == []
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "someOtherBuilder.build" in externals


def test_bare_call_to_same_file_builder_named_function_still_resolves() -> (
    None
):
    # Regression guard mirroring the existing ``trim`` regression test:
    # the noise guard only applies to receiver-qualified calls and
    # known ambient-global bare names -- a genuinely local bare call to
    # a same-file function sharing a name now in
    # ``_BUILDER_METHOD_NAMES`` (``build``/``of``/``from``/``with``)
    # must still resolve normally.
    build_fn = _fn("util.py", "build")
    caller = _fn("util.py", "run", line=5)
    files = [
        FileMap(
            "util.py",
            "python",
            symbols=[build_fn, caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="util.py",
                    text="build",
                    name="build",
                    line=6,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, build_fn.id) in edges
    assert graph.ambiguous == []


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
    buffer_diff_new = _fn(
        "buffer_diff.rs",
        "new",
        "BufferDiff.new",
        line=5,
        language="rust",
    )
    other_new = _fn(
        "buffer_diff.rs", "new", "Other.new", line=20, language="rust"
    )
    caller = _fn(
        "buffer_diff.rs",
        "new_with_base_text",
        "BufferDiff.new_with_base_text",
        line=40,
        language="rust",
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
    # Deliberately not named ``build``/``has``/``now``/etc. — those are
    # now denylisted noise-guard names (round 23) and a
    # receiver-qualified call to one of them is suppressed before this
    # step's own candidate count is even reached; this test's fixture
    # needs a name outside every denylist so it still exercises the
    # *receiver-type-match negative-control* path it was written for.
    unrelated_a = _fn("a.rs", "render", "Foo.render")
    unrelated_b = _fn("b.rs", "render", "Bar.render")
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
                    text="Widget::render",
                    name="render",
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
    # Round 22 cline.md §3.1: noise-suppressed calls now route to
    # ``external``, not ``ambiguous``.
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "expect" in externals


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
    # Round 22 cline.md §3.1: noise-suppressed calls now route to
    # ``external``, not ``ambiguous``.
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "describe" in externals


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
    # Round 22 cline.md §3.1: noise-suppressed calls now route to
    # ``external``, not ``ambiguous``.
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "String" in externals


def test_noise_guard_wins_over_ambiguous_with_two_candidates() -> None:
    # Round 22 cline.md §3.1: noise detection (``_is_noise_call``) runs
    # *before* the candidate-count branch in ``_pick_candidate``'s
    # ladder — a receiver-qualified builtin-method-shaped call must
    # still route to ``external`` via the ``_NOISE`` sentinel even
    # when the repo happens to have 2+ unrelated same-named candidates
    # (not just the single-candidate shape every other noise-guard
    # test here covers), since noise detection never even looks at
    # candidate count.
    trim_a = _fn("util_a.ts", "trim", "A.trim")
    trim_b = _fn("util_b.ts", "trim", "B.trim")
    caller = _fn("caller.ts", "run")
    files = [
        FileMap("util_a.ts", "typescript", symbols=[trim_a]),
        FileMap("util_b.ts", "typescript", symbols=[trim_b]),
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
    assert (caller.id, trim_a.id) not in edges
    assert (caller.id, trim_b.id) not in edges
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert "opts.config.trim" in externals


def test_receiver_qualified_get_resolve_create_not_guessed() -> None:
    # Part B of round 22 cline.md §3.1: ``_BUILTIN_METHOD_NAMES`` was
    # missing several very common JS/TS names that hit this same fast
    # path once every stronger heuristic fails -- ``get`` averaged
    # 32.0 candidates in cline's own report, almost certainly
    # ``Map.get()``/``Promise.resolve()``/``Object.create()`` noise,
    # not real repo-symbol collisions.
    get_fn = _fn("registry.ts", "get")
    resolve_fn = _fn("registry.ts", "resolve", line=2)
    create_fn = _fn("registry.ts", "create", line=3)
    caller = _fn("caller.ts", "run")
    files = [
        FileMap(
            "registry.ts",
            "typescript",
            symbols=[get_fn, resolve_fn, create_fn],
        ),
        FileMap(
            "caller.ts",
            "typescript",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.ts",
                    text="cache.get",
                    name="get",
                    receiver="cache",
                    line=2,
                ),
                RawCall(
                    caller_id=caller.id,
                    path="caller.ts",
                    text="Promise.resolve",
                    name="resolve",
                    receiver="Promise",
                    line=3,
                ),
                RawCall(
                    caller_id=caller.id,
                    path="caller.ts",
                    text="Object.create",
                    name="create",
                    receiver="Object",
                    line=4,
                ),
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, get_fn.id) not in edges
    assert (caller.id, resolve_fn.id) not in edges
    assert (caller.id, create_fn.id) not in edges
    assert graph.ambiguous == []
    externals = {ext.callee for ext in graph.external}
    assert {"cache.get", "Promise.resolve", "Object.create"} <= externals


def test_reference_resolution_unaffected_by_noise_guard() -> None:
    # ``_pick_candidate``'s ``repo_stems`` gate is only threaded from
    # ``_resolve_call`` — ``resolve_refs()``/``_resolve_ref`` never
    # passes it, so a bare-value *reference* (not a call) to a
    # noise-listed name resolves exactly as it did before this fix; the
    # fan-in bug this pass targets never affected the separate
    # ``referenced``/``referenced_in`` tables.
    local = _fn("helpers.ts", "describe", language="typescript")
    caller = _fn("spec.ts", "run", language="typescript")
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


# 1.4: resolve()/resolve_refs() gained a process-pool parallelization
# split (``_resolve_all``/``_chunk_files``) for large repos. These
# tests force the parallel path (by monkeypatching the item-count
# threshold down to 0) on modest, hand-built fixtures rather than a
# huge repo, and assert byte-identical output against the sequential
# (``workers=1``) path — the merge step must not reorder, drop, or
# double-count any edge/ambiguous/external/reference entry regardless
# of which worker resolved which file.


def _multi_file_call_fixture() -> list[FileMap]:
    """20 files, each defining a same-named ``run`` plus a caller that
    invokes a mix of same-file, cross-file, and ambiguous same-named
    targets — enough shape to actually exercise chunk boundaries."""
    files = [
        FileMap(
            path=f"mod{i}.py",
            language="python",
            symbols=[_fn(f"mod{i}.py", "run"), _fn(f"mod{i}.py", "helper")],
            calls=[
                RawCall(
                    caller_id=f"mod{i}.py::run",
                    path=f"mod{i}.py",
                    text="run",
                    name="run",
                    line=2,
                ),
                RawCall(
                    caller_id=f"mod{i}.py::run",
                    path=f"mod{i}.py",
                    text="helper",
                    name="helper",
                    line=3,
                ),
            ],
        )
        for i in range(20)
    ]
    return files


def _multi_file_ref_fixture() -> list[FileMap]:
    """20 files, each with a value-reference to a bare, repo-wide
    same-named binding (ambiguous by construction) — the reference
    analog of ``_multi_file_call_fixture``."""
    files = [
        FileMap(
            path=f"mod{i}.py",
            language="python",
            symbols=[_fn(f"mod{i}.py", "target"), _fn(f"mod{i}.py", "entry")],
            refs=[
                RawRef(
                    caller_id=f"mod{i}.py::entry",
                    path=f"mod{i}.py",
                    name="target",
                    line=2,
                )
            ],
        )
        for i in range(20)
    ]
    return files


def _graph_shape(graph: object) -> tuple:
    return (
        sorted((e.caller, e.callee, tuple(e.lines)) for e in graph.edges),
        sorted(graph.ambiguous),
        sorted((e.caller, e.callee, tuple(e.lines)) for e in graph.external),
        sorted(graph.calls_in.items()),
        sorted(graph.calls_out.items()),
        sorted((e.caller, e.callee, tuple(e.lines)) for e in graph.referenced),
        sorted(graph.referenced_in.items()),
        sorted(graph.referenced_out.items()),
    )


def test_chunk_files_splits_evenly() -> None:
    files = _multi_file_call_fixture()
    chunks = resolver_mod._chunk_files(files, 4)
    assert len(chunks) == 4
    assert sum(len(c) for c in chunks) == len(files)
    # No file dropped or duplicated across chunks.
    assert sorted(fm.path for c in chunks for fm in c) == sorted(
        fm.path for fm in files
    )


def test_chunk_files_workers_le_1_is_a_single_chunk() -> None:
    files = _multi_file_call_fixture()
    assert resolver_mod._chunk_files(files, 1) == [files]
    assert resolver_mod._chunk_files(files, 0) == [files]


def test_chunk_files_never_makes_more_chunks_than_files() -> None:
    files = _multi_file_call_fixture()[:3]
    chunks = resolver_mod._chunk_files(files, 8)
    assert len(chunks) == 3


def test_resolve_parallel_matches_sequential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod, "_RESOLVE_PARALLEL_MIN_ITEMS", 0)
    files = _multi_file_call_fixture()

    sequential = resolve(files, workers=1)
    parallel = resolve(files, workers=4)

    assert _graph_shape(sequential) == _graph_shape(parallel)
    # Sanity: the parallel run actually did real work, not a no-op.
    assert sequential.edges


def test_resolve_all_oversubscribes_chunk_count_beyond_worker_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 17: ``_resolve_all``'s ``_run(w)`` closure must build
    ``w * _RESOLVE_CHUNK_OVERSUBSCRIPTION`` chunks, not just ``w`` --
    more chunks than workers is the whole point of the fix (an idle
    worker can pick up the next queued chunk instead of sitting idle
    once its one assigned chunk finishes). Byte-identical-output tests
    alone wouldn't catch a silent revert to one-chunk-per-worker, since
    output is correct either way -- this asserts the chunk *count*
    the pool actually sees, by spying on ``_chunk_files``."""
    monkeypatch.setattr(resolver_mod, "_RESOLVE_PARALLEL_MIN_ITEMS", 0)
    files = _multi_file_call_fixture()  # 20 files, plenty to chunk finely

    real_chunk_files = resolver_mod._chunk_files
    seen_n: list[int] = []

    def spy_chunk_files(files: list, n: int) -> list:
        seen_n.append(n)
        return real_chunk_files(files, n)

    monkeypatch.setattr(resolver_mod, "_chunk_files", spy_chunk_files)

    workers = 3
    resolve(files, workers=workers)

    assert seen_n  # _chunk_files was actually invoked
    expected = workers * resolver_mod._RESOLVE_CHUNK_OVERSUBSCRIPTION
    assert all(n == expected for n in seen_n)
    assert expected > workers  # the actual oversubscription property


def test_resolve_refs_parallel_matches_sequential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod, "_RESOLVE_PARALLEL_MIN_ITEMS", 0)
    files = _multi_file_ref_fixture()

    seq_edges, seq_in, seq_out = resolve_refs(files, workers=1)
    par_edges, par_in, par_out = resolve_refs(files, workers=4)

    def shape(edges: list, rin: dict, rout: dict) -> tuple:
        return (
            sorted((e.caller, e.callee, tuple(e.lines)) for e in edges),
            sorted(rin.items()),
            sorted(rout.items()),
        )

    assert shape(seq_edges, seq_in, seq_out) == shape(
        par_edges, par_in, par_out
    )
    assert seq_edges  # sanity: real work happened


def _multi_file_throws_fixture() -> list[FileMap]:
    """20 files, each defining a same-named ``Err`` class plus a
    ``load`` function that raises it (same-file resolution) — the
    throw-resolution analog of ``_multi_file_call_fixture``, enough
    shape to actually exercise chunk boundaries."""
    files = []
    for i in range(20):
        err = Symbol(
            id=f"mod{i}.py::Err",
            name="Err",
            qualname="Err",
            kind="class",
            path=f"mod{i}.py",
            language="python",
        )
        fn = Symbol(
            id=f"mod{i}.py::load",
            name="load",
            qualname="load",
            kind="function",
            path=f"mod{i}.py",
            language="python",
        )
        files.append(
            FileMap(
                path=f"mod{i}.py",
                language="python",
                symbols=[err, fn],
                throws=[
                    RawThrow(
                        caller_id=f"mod{i}.py::load",
                        path=f"mod{i}.py",
                        text="Err",
                        name="Err",
                        line=2,
                    )
                ],
            )
        )
    return files


def _multi_file_catches_fixture() -> list[FileMap]:
    """20 files, each with an except clause naming a same-file ``Err``
    class — the catch-resolution analog of ``_multi_file_throws_fixture``.
    """
    files = []
    for i in range(20):
        err = Symbol(
            id=f"mod{i}.py::Err",
            name="Err",
            qualname="Err",
            kind="class",
            path=f"mod{i}.py",
            language="python",
        )
        fn = Symbol(
            id=f"mod{i}.py::run",
            name="run",
            qualname="run",
            kind="function",
            path=f"mod{i}.py",
            language="python",
        )
        files.append(
            FileMap(
                path=f"mod{i}.py",
                language="python",
                symbols=[err, fn],
                catches=[
                    RawCatch(
                        caller_id=f"mod{i}.py::run",
                        path=f"mod{i}.py",
                        types=["Err"],
                        bare=False,
                        line=2,
                    )
                ],
            )
        )
    return files


def test_resolve_throws_parallel_matches_sequential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod, "_RESOLVE_PARALLEL_MIN_ITEMS", 0)
    files = _multi_file_throws_fixture()

    seq = resolve_throws(files, workers=1)
    par = resolve_throws(files, workers=4)

    def shape(result: tuple) -> tuple:
        edges, out, ambiguous, external, bare = result
        return (
            sorted((e.caller, e.type, tuple(e.lines)) for e in edges),
            sorted(out.items()),
            sorted(ambiguous),
            sorted((e.caller, e.callee, tuple(e.lines)) for e in external),
            sorted(bare),
        )

    assert shape(seq) == shape(par)
    assert seq[0]  # sanity: real work happened


def test_resolve_catches_parallel_matches_sequential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod, "_RESOLVE_PARALLEL_MIN_ITEMS", 0)
    files = _multi_file_catches_fixture()

    seq = resolve_catches(files, workers=1)
    par = resolve_catches(files, workers=4)

    def shape(sites: list) -> list:
        return [
            (
                s.caller,
                s.path,
                tuple(s.type_names),
                tuple(sorted(s.repo_types.items())),
                s.bare,
                s.line,
            )
            for s in sites
        ]

    assert shape(seq) == shape(par)
    assert seq  # sanity: real work happened


def test_resolve_below_threshold_stays_sequential_by_default() -> None:
    """The default ``workers=1`` (every caller except ``cli.py``'s
    ``run_map``) must behave exactly as before this change — no pool,
    no chunking, single-pass resolution."""
    files = _multi_file_call_fixture()
    graph = resolve(files)
    assert graph.edges  # unchanged baseline behavior, still resolves


# 1.5 (round 17): a BrokenProcessPool on the parallel path gets one
# bounded retry at reduced parallelism (``run_pooled_with_retry``)
# before propagating — see
# .features/plans/round17/round17-mcp-process-pool-concurrent-load-plan.md.
# No existing test simulated a broken pool before this; ``_FlakyPool``
# replaces ``ProcessPoolExecutor`` with a fake that raises
# ``BrokenProcessPool`` on ``__enter__`` for its first ``fail_times``
# constructions, then falls back to a real ``ThreadPoolExecutor`` (same
# submit/map/future.result() interface, but in-process — fast,
# deterministic, and avoids pickling module-level resolver functions
# across a real subprocess boundary just to prove the retry wiring).


def _flaky_pool_factory(fail_times: int) -> type:
    """Build a ``ProcessPoolExecutor``-shaped fake that raises
    ``BrokenProcessPool`` on construction for its first ``fail_times``
    constructions (counted across every instance built from this one
    factory call), then delegates to ``ThreadPoolExecutor``.

    Round 22: every call site now owns its pool via
    ``pool = ProcessPoolExecutor(...)`` / ``try``/``finally:
    pool.shutdown(wait=False)`` instead of ``with ProcessPoolExecutor(
    ...) as pool:`` (see ``_run_pool_bounded``'s docstring for why) --
    so the failure trigger point moves from ``__enter__`` to
    ``__init__``, and ``submit``/``shutdown`` delegate straight to the
    real ``ThreadPoolExecutor`` instead of relying on context-manager
    protocol.

    Accepts (and forwards) ``initializer``/``initargs`` the same way
    ``ProcessPoolExecutor`` does (round 17: ``_resolve_all`` and its
    siblings now construct their real pool with these) --
    ``ThreadPoolExecutor`` supports both natively, and since threads
    share process memory, running the initializer per-thread still
    populates the same module-level ``_worker_*`` globals the real
    worker wrapper functions read from, just redundantly rather than
    once-per-process -- harmless for a same-value re-assignment in a
    test fake."""
    state = {"calls": 0}

    class _FlakyPool:
        def __init__(
            self,
            max_workers: int | None = None,
            initializer: object = None,
            initargs: tuple = (),
        ) -> None:
            state["calls"] += 1
            if state["calls"] <= fail_times:
                raise BrokenProcessPool("simulated: process pool broken")
            self._real = ThreadPoolExecutor(
                max_workers=max_workers,
                initializer=initializer,
                initargs=initargs,
            )

        def submit(self, fn: object, *args: object) -> object:
            return self._real.submit(fn, *args)

        def shutdown(
            self, wait: bool = True, *, cancel_futures: bool = False
        ) -> None:
            self._real.shutdown(wait=wait, cancel_futures=cancel_futures)

    return _FlakyPool


@pytest.fixture(autouse=True)
def _no_real_pool_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 23 §15 added a fixed backoff (``_POOL_RETRY_DELAY_S``)
    before ``run_pooled_with_retry``'s bounded retry fires. Every
    ``BrokenProcessPool``-retry test in this module (and the
    ``_FlakyPool``-based tests further below) deliberately triggers
    that retry path -- without this, each would pay a real multi-
    second sleep, several times over across the suite. Autouse and
    file-scoped since ``time.sleep`` has exactly one call site in
    ``resolver.py`` (the retry backoff itself); the dedicated backoff
    test below re-patches ``time.sleep`` itself to assert it fires
    with the expected delay, superseding this no-op within that one
    test.
    """
    monkeypatch.setattr(resolver_mod.time, "sleep", lambda _seconds: None)


def test_run_pooled_with_retry_retries_once_then_succeeds() -> None:
    calls: list[int] = []

    def run(w: int) -> str:
        calls.append(w)
        if len(calls) == 1:
            raise BrokenProcessPool("simulated: process pool broken")
        return f"ok-{w}"

    result = resolver_mod.run_pooled_with_retry(run, workers=8, what="test")

    assert result == "ok-2"  # retry_workers = min(8, _POOL_RETRY_WORKERS=2)
    assert calls == [8, 2]


def test_run_pooled_with_retry_caps_retry_at_original_workers() -> None:
    """A retry never requests *more* workers than the first attempt —
    ``min(workers, _POOL_RETRY_WORKERS)``, not always exactly 2."""
    calls: list[int] = []

    def run(w: int) -> str:
        calls.append(w)
        if len(calls) == 1:
            raise BrokenProcessPool("simulated: process pool broken")
        return "ok"

    resolver_mod.run_pooled_with_retry(run, workers=1, what="test")
    assert calls == [1, 1]


def test_run_pooled_with_retry_propagates_after_second_failure() -> None:
    calls: list[int] = []

    def run(w: int) -> str:
        calls.append(w)
        raise BrokenProcessPool("simulated: process pool broken")

    with pytest.raises(BrokenProcessPool):
        resolver_mod.run_pooled_with_retry(run, workers=8, what="test")
    assert calls == [8, 2]  # exactly one retry, no loop


def test_run_pooled_with_retry_sleeps_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 23 §15: an immediate retry can land in the exact same
    transient window (CPU contention, or a `uv tool install
    --reinstall` shim relink race) that caused the first
    ``BrokenProcessPool``. Confirms the backoff actually fires, with
    the expected delay, between the two ``run()`` invocations -- and
    that it fires *before* the retry attempt, not after (an
    after-the-fact sleep would be a no-op for this bug)."""
    events: list[str] = []

    def fake_sleep(seconds: float) -> None:
        assert seconds == resolver_mod._POOL_RETRY_DELAY_S
        events.append("sleep")

    monkeypatch.setattr(resolver_mod.time, "sleep", fake_sleep)

    calls: list[int] = []

    def run(w: int) -> str:
        calls.append(w)
        events.append(f"run-{w}")
        if len(calls) == 1:
            raise BrokenProcessPool("simulated: process pool broken")
        return "ok"

    result = resolver_mod.run_pooled_with_retry(run, workers=8, what="test")

    assert result == "ok"
    assert events == ["run-8", "sleep", "run-2"]


def test_run_pooled_with_retry_prints_disclosure_note_on_retry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def run(w: int) -> str:
        if w == 8:
            raise BrokenProcessPool("simulated: process pool broken")
        return "ok"

    resolver_mod.run_pooled_with_retry(run, workers=8, what="call resolution")

    err = capsys.readouterr().err
    assert "note:" in err
    assert "call resolution" in err
    assert "reduced parallelism" in err


# Round 21 Track A: cline reproduced a spawned worker resolving a
# completely different Python interpreter than its own parent process
# (the system Anaconda install instead of the parent's `uv
# tool`-managed venv), hanging 6+ minutes at 0% CPU before a manual
# kill revealed the mismatch. ``run_pooled_with_retry`` now pins
# ``multiprocessing``'s spawn executable to ``sys.executable`` before
# every pool attempt, and turns a stalled future (one that never
# returns within ``POOL_RESULT_TIMEOUT_S``) into a clear
# ``PoolStalledError`` instead of hanging indefinitely.


def test_run_pooled_with_retry_pins_interpreter_before_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        resolver_mod.multiprocessing,
        "set_executable",
        lambda exe: calls.append(exe),
    )

    resolver_mod.run_pooled_with_retry(lambda w: "ok", workers=4, what="test")

    assert calls == [sys.executable]


def test_run_pooled_with_retry_pins_interpreter_before_retry_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        resolver_mod.multiprocessing,
        "set_executable",
        lambda exe: calls.append(exe),
    )
    attempts: list[int] = []

    def run(w: int) -> str:
        attempts.append(w)
        if len(attempts) == 1:
            raise BrokenProcessPool("simulated: process pool broken")
        return "ok"

    resolver_mod.run_pooled_with_retry(run, workers=8, what="test")

    # Pinned once before the first attempt and again before the retry
    # -- a fresh pool is constructed each time, so each needs its own
    # pin.
    assert calls == [sys.executable, sys.executable]


def test_run_pooled_with_retry_raises_pool_stalled_error_on_timeout() -> None:
    def run(w: int) -> str:
        raise PoolTimeoutError("simulated: worker never returned")

    with pytest.raises(resolver_mod.PoolStalledError, match="test"):
        resolver_mod.run_pooled_with_retry(run, workers=8, what="test")


def test_run_pooled_with_retry_timeout_is_not_retried() -> None:
    """Unlike ``BrokenProcessPool``, a stalled future gets no bounded
    retry -- a worker that never returns at reduced parallelism is no
    more likely to un-wedge than at full parallelism, so surfacing the
    clear error immediately beats waiting twice as long for the same
    outcome."""
    calls: list[int] = []

    def run(w: int) -> str:
        calls.append(w)
        raise PoolTimeoutError("simulated: worker never returned")

    with pytest.raises(resolver_mod.PoolStalledError):
        resolver_mod.run_pooled_with_retry(run, workers=8, what="test")

    assert calls == [8]


def test_run_pooled_with_retry_stalled_error_message_is_actionable() -> None:
    def run(w: int) -> str:
        raise PoolTimeoutError("simulated: worker never returned")

    with pytest.raises(resolver_mod.PoolStalledError) as exc_info:
        resolver_mod.run_pooled_with_retry(
            run, workers=8, what="file extraction"
        )

    message = str(exc_info.value)
    assert "file extraction" in message
    assert str(resolver_mod.POOL_RESULT_TIMEOUT_S) in message
    assert "--jobs 1" in message


def test_resolve_parallel_raises_pool_stalled_error_on_stalled_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a worker future that never completes surfaces as
    ``PoolStalledError`` from a real ``resolve()`` call, not an
    indefinite hang."""

    class _StalledFuture:
        def result(self, timeout: float | None = None) -> object:
            raise PoolTimeoutError("simulated: worker never returned")

    class _StalledPool:
        def __init__(
            self,
            max_workers: int | None = None,
            initializer: object = None,
            initargs: tuple = (),
        ) -> None:
            if initializer is not None:
                initializer(*initargs)
            # ``_run_pool_bounded`` reads the private ``_processes``
            # attribute (dict of pid -> Process) to force-kill any
            # still-wedged worker after a timeout -- empty here since
            # this fake never launches a real subprocess.
            self._processes: dict = {}

        def __enter__(self) -> "_StalledPool":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def submit(self, fn: object, *args: object) -> _StalledFuture:
            return _StalledFuture()

        def shutdown(
            self, wait: bool = True, *, cancel_futures: bool = False
        ) -> None:
            pass

    monkeypatch.setattr(resolver_mod, "_RESOLVE_PARALLEL_MIN_ITEMS", 0)
    monkeypatch.setattr(resolver_mod, "ProcessPoolExecutor", _StalledPool)
    files = _multi_file_call_fixture()

    with pytest.raises(resolver_mod.PoolStalledError):
        resolve(files, workers=4)


# Round 22 claude-code.md §1: the tests above all mock the pool away
# (``_StalledPool``/``_flaky_pool_factory``), so none of them ever let
# a timeout unwind through a *real* ``ProcessPoolExecutor``'s
# context-manager exit -- which is exactly the gap that let the bug
# ship. ``ProcessPoolExecutor.__exit__`` unconditionally calls
# ``shutdown(wait=True)``, which blocks until every worker the pool
# ever launched terminates; a genuinely wedged worker never does, so
# the 600s ``POOL_RESULT_TIMEOUT_S`` bound was reached but then masked
# by an unbounded wait immediately afterward. These two tests exercise
# ``_run_pool_bounded`` (the fix: pool owned via ``try``/``finally``
# instead of ``with``, timeout tears the pool down with
# ``wait=False`` and force-kills any still-alive worker) against a
# real pool and a worker that never returns, asserting the *timing*
# guarantee -- bounded wall-clock return, and no leaked live worker
# processes -- not just the exception type.


def _sleep_worker(seconds: float) -> str:
    """Module-level (spawn-picklable) worker that sleeps far longer
    than the test-scale timeout below it, standing in for a wedged
    worker process that never completes on its own."""
    time.sleep(seconds)
    return "done"


def test_run_pool_bounded_returns_promptly_on_real_pool_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod, "POOL_RESULT_TIMEOUT_S", 0.3)
    pool = ProcessPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_sleep_worker, 30.0)
        start = time.monotonic()
        with pytest.raises(PoolTimeoutError):
            resolver_mod._run_pool_bounded(pool, [future])
        elapsed = time.monotonic() - start
        # Well under the 30s the worker is actually sleeping for --
        # before the fix, this would have blocked for the full 30s
        # (or longer) inside ``__exit__``'s ``shutdown(wait=True)``.
        assert elapsed < 5.0
    finally:
        pool.shutdown(wait=False)


def test_run_pool_bounded_kills_wedged_worker_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod, "POOL_RESULT_TIMEOUT_S", 0.3)
    pool = ProcessPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_sleep_worker, 30.0)
        # Snapshot the worker process handles before calling, since
        # ``_run_pool_bounded`` (via ``pool.shutdown()``) clears
        # ``pool._processes`` to ``None`` as part of its own teardown.
        while not pool._processes:
            time.sleep(0.01)
        procs = list(pool._processes.values())
        assert procs, "expected at least one worker process to check"

        with pytest.raises(PoolTimeoutError):
            resolver_mod._run_pool_bounded(pool, [future])

        deadline = time.monotonic() + 5.0
        while any(p.is_alive() for p in procs) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not any(p.is_alive() for p in procs)
    finally:
        pool.shutdown(wait=False)


def test_resolve_parallel_retries_once_on_broken_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod, "_RESOLVE_PARALLEL_MIN_ITEMS", 0)
    monkeypatch.setattr(
        resolver_mod, "ProcessPoolExecutor", _flaky_pool_factory(1)
    )
    files = _multi_file_call_fixture()

    graph = resolve(files, workers=4)

    assert graph.edges  # succeeded despite the first pool breaking


def test_resolve_parallel_propagates_when_retry_also_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod, "_RESOLVE_PARALLEL_MIN_ITEMS", 0)
    monkeypatch.setattr(
        resolver_mod, "ProcessPoolExecutor", _flaky_pool_factory(99)
    )
    files = _multi_file_call_fixture()

    with pytest.raises(BrokenProcessPool):
        resolve(files, workers=4)


def test_resolve_refs_parallel_retries_once_on_broken_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod, "_RESOLVE_PARALLEL_MIN_ITEMS", 0)
    monkeypatch.setattr(
        resolver_mod, "ProcessPoolExecutor", _flaky_pool_factory(1)
    )
    files = _multi_file_ref_fixture()

    edges, _referenced_in, _referenced_out = resolve_refs(files, workers=4)

    assert edges  # succeeded despite the first pool breaking


def test_resolve_throws_parallel_retries_once_on_broken_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod, "_RESOLVE_PARALLEL_MIN_ITEMS", 0)
    monkeypatch.setattr(
        resolver_mod, "ProcessPoolExecutor", _flaky_pool_factory(1)
    )
    files = _multi_file_throws_fixture()

    throw_edges, *_rest = resolve_throws(files, workers=4)

    assert throw_edges  # succeeded despite the first pool breaking


def test_resolve_catches_parallel_retries_once_on_broken_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_mod, "_RESOLVE_PARALLEL_MIN_ITEMS", 0)
    monkeypatch.setattr(
        resolver_mod, "ProcessPoolExecutor", _flaky_pool_factory(1)
    )
    files = _multi_file_catches_fixture()

    sites = resolve_catches(files, workers=4)

    assert sites  # succeeded despite the first pool breaking


# --- Bare-call vs. unrelated method collision (round-12 §3.2) ---


def test_bare_call_resolves_to_free_function_over_unrelated_method() -> None:
    """Round-12 master report §3.2: a bare (receiverless) call to a
    same-package Go free function (``Generate``) used to misresolve
    as ambiguous against an unrelated *method* sharing the same bare
    name in a different package (``(g *IDGenerator) Generate(...)``),
    causing ``dekko affected``/``workset`` to report zero impacted
    tests for a change a same-package unit test directly covered. A
    bare call can never syntactically reach a method, so the free
    function must win."""
    free_fn = Symbol(
        id="pkg/slug/generator.go::Generate",
        name="Generate",
        qualname="Generate",
        kind="function",
        path="pkg/slug/generator.go",
        language="go",
    )
    method = Symbol(
        id="pkg/markdown/id.go::IDGenerator.Generate",
        name="Generate",
        qualname="IDGenerator.Generate",
        kind="method",
        path="pkg/markdown/id.go",
        language="go",
    )
    caller = _fn("pkg/slug/generator_test.go", "TestGenerate")
    files = [
        FileMap("pkg/slug/generator.go", "go", symbols=[free_fn]),
        FileMap("pkg/markdown/id.go", "go", symbols=[method]),
        FileMap(
            "pkg/slug/generator_test.go",
            "go",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="pkg/slug/generator_test.go",
                    text="Generate",
                    name="Generate",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, free_fn.id) in edges
    assert graph.ambiguous == []


def test_bare_call_stays_ambiguous_among_multiple_non_methods() -> None:
    """Regression guard: the bare-call/non-method fix must only kick
    in when it narrows the field to exactly one candidate. Two
    genuinely unrelated free functions sharing a bare name must still
    land in ``ambiguous`` — this isn't a general "prefer functions"
    rule, only a "methods are impossible for a bare call" one."""
    a = _fn("a.py", "helper")
    b = _fn("b.py", "helper")
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
                    text="helper",
                    name="helper",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert edges == set()
    assert len(graph.ambiguous) == 1


def test_receiver_qualified_call_unaffected_by_non_method_fallback() -> None:
    """Regression guard: a call *with* a receiver must never reach the
    bare-call fallback -- it's gated on ``call.receiver`` being falsy.
    A receiver-qualified call to an ambiguous method-vs-function pair
    should stay ambiguous rather than being silently steered to the
    function just because the fallback exists."""
    free_fn = Symbol(
        id="a.py::helper",
        name="helper",
        qualname="helper",
        kind="function",
        path="a.py",
        language="python",
    )
    method = Symbol(
        id="b.py::Thing.helper",
        name="helper",
        qualname="Thing.helper",
        kind="method",
        path="b.py",
        language="python",
    )
    caller = _fn("caller.py", "entry")
    files = [
        FileMap("a.py", "python", symbols=[free_fn]),
        FileMap("b.py", "python", symbols=[method]),
        FileMap(
            "caller.py",
            "python",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="caller.py",
                    text="obj.helper",
                    name="helper",
                    receiver="obj",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert edges == set()
    assert len(graph.ambiguous) == 1


# --- Language-aware candidate pre-filter (round-21 §Track D) --------------


def test_language_filtered_drops_candidates_in_a_different_language() -> None:
    """``_language_filtered`` (used as ``_pick_candidate``'s first
    ladder step) narrows a repo-wide candidate list to the call site's
    own language before anything else runs."""
    py_candidate = Symbol(
        id="a.py::Thing",
        name="Thing",
        qualname="Thing",
        kind="class",
        path="a.py",
        language="python",
    )
    cpp_candidate = Symbol(
        id="a.cc::Thing",
        name="Thing",
        qualname="Thing",
        kind="function",
        path="a.cc",
        language="cpp",
    )
    call = RawCall(
        caller_id=None, path="caller.cc", text="Thing", name="Thing", line=1
    )
    filtered = resolver_mod._language_filtered(
        call, [py_candidate, cpp_candidate]
    )
    assert filtered == [cpp_candidate]


def test_language_filtered_falls_through_within_c_cpp_family() -> None:
    """Round 21 residual fix (`.features/fixes/resolver-vendored-
    exclusion-false-match.md`): when no same-language candidate
    exists, the fallback narrows to the call site's language *family*
    rather than the full unfiltered list -- but a legitimate same-
    family case (a C header declaring something a C++ file uses) must
    still resolve exactly as before, since c/cpp is one of the two
    families the resolver already treats as one interoperating unit
    (mirroring ``_WHOLE_FILE_IMPORT_LANGUAGES``/``_IMPORT_RESOLVERS``).
    """
    c_header_candidate = Symbol(
        id="a.h::Thing",
        name="Thing",
        qualname="Thing",
        kind="function",
        path="a.h",
        language="c",
    )
    call = RawCall(
        caller_id=None, path="caller.cc", text="Thing", name="Thing", line=1
    )
    filtered = resolver_mod._language_filtered(call, [c_header_candidate])
    assert filtered == [c_header_candidate]


def test_language_filtered_falls_through_within_js_family() -> None:
    """Same shape as the c/cpp family test above, for the
    javascript/typescript/tsx family -- no direct test coverage
    existed for this family before this fix."""
    ts_candidate = Symbol(
        id="a.ts::helper",
        name="helper",
        qualname="helper",
        kind="function",
        path="a.ts",
        language="typescript",
    )
    call = RawCall(
        caller_id=None,
        path="caller.tsx",
        text="helper",
        name="helper",
        line=1,
    )
    filtered = resolver_mod._language_filtered(call, [ts_candidate])
    assert filtered == [ts_candidate]


def test_language_filtered_returns_empty_across_unrelated_families() -> None:
    """The literal tensorflow shape: a C++ call site with only a
    same-bare-name Python candidate (no same-language, no same-family
    candidate exists at all) must narrow to an empty list, not fall
    back to the full unfiltered candidates -- python and cpp share no
    family, so there is no legitimate precedent for python answering
    a cpp call the way a C header answers a C++ call."""
    python_candidate = Symbol(
        id="a.py::InvalidArgumentError",
        name="InvalidArgumentError",
        qualname="InvalidArgumentError",
        kind="class",
        path="a.py",
        language="python",
    )
    call = RawCall(
        caller_id=None,
        path="caller.cc",
        text="InvalidArgumentError",
        name="InvalidArgumentError",
        line=1,
    )
    filtered = resolver_mod._language_filtered(call, [python_candidate])
    assert filtered == []


def test_language_filtered_unchanged_for_unrecognized_call_site_path() -> None:
    """The call site's own language must be determinable to filter at
    all -- an unrecognized extension leaves ``candidates`` untouched
    rather than dropping everything."""
    candidate = Symbol(
        id="a.py::Thing",
        name="Thing",
        qualname="Thing",
        kind="class",
        path="a.py",
        language="python",
    )
    call = RawCall(
        caller_id=None,
        path="caller.unknownext",
        text="Thing",
        name="Thing",
        line=1,
    )
    filtered = resolver_mod._language_filtered(call, [candidate])
    assert filtered == [candidate]


def test_module_matches_bare_node_builtin_specifier_denylisted() -> None:
    # Round 22 claude-buddy.md §2.1: a bare (non-relative) JS/TS import
    # source naming a Node core module must never match a same-named
    # repo file, regardless of stem collision -- confirmed against the
    # actual extractor encoding (``"path/join"`` for a named import of
    # ``join`` from ``"path"``, per ``extractor._imports_js``).
    assert not resolver_mod._module_matches("path/join", "server/path.ts")
    # Default and namespace imports get the same "/name" suffix.
    assert not resolver_mod._module_matches(
        "path/defaultPath", "server/path.ts"
    )
    assert not resolver_mod._module_matches("path/path", "server/path.ts")
    # Side-effect import (no local binding) keeps the bare source.
    assert not resolver_mod._module_matches("path", "server/path.ts")


def test_module_matches_relative_import_still_matches() -> None:
    # A genuine relative import must be unaffected by the Node-builtin
    # denylist -- only the bare specifier shape is denylisted.
    assert resolver_mod._module_matches("./path/resolvePath", "server/path.ts")
    assert resolver_mod._module_matches("./path", "server/path.ts")


def test_module_matches_node_builtin_denylist_is_js_ts_only() -> None:
    # The denylist is gated on the candidate file's own extension -- a
    # Python file legitimately named ``path.py`` must not be affected
    # by a JS-only denylist just because some unrelated JS/TS file
    # elsewhere imports Node's real ``path`` module.
    assert resolver_mod._module_matches("path", "server/path.py")


def test_rust_crate_hint_matches_crate_root_re_export() -> None:
    # Round 22 zed.md §3.2: a trait declared in one file
    # (``crates/gpui/src/element.rs``) but only reachable elsewhere
    # via its crate-root re-export (``use gpui::Render;``) must match
    # through the crate's root directory, not the declaring file's
    # own stem. Round 23 Fix B: ``crate_roots`` values are now lists
    # (every matching root, not just one) -- see
    # ``test_rust_crate_hint_matches_multiple_roots_for_same_name``
    # below for the collision-aware behavior this enables.
    crate_roots = {"gpui": ["crates/gpui/src"]}
    assert resolver_mod._rust_crate_hint_matches(
        "gpui::Render", "crates/gpui/src/element.rs", crate_roots
    )
    # A candidate outside that crate's root, or a hint naming a
    # different/unknown crate, must not match.
    assert not resolver_mod._rust_crate_hint_matches(
        "gpui::Render", "crates/other/src/lib.rs", crate_roots
    )
    assert not resolver_mod._rust_crate_hint_matches(
        "unknown_crate::Render", "crates/gpui/src/element.rs", crate_roots
    )


def test_rust_crate_hint_matches_only_rust_candidates() -> None:
    # A crate-name/candidate-path coincidence must never leak into a
    # non-Rust candidate.
    crate_roots = {"gpui": ["crates/gpui/src"]}
    assert not resolver_mod._rust_crate_hint_matches(
        "gpui::Render", "crates/gpui/src/element.py", crate_roots
    )


def test_rust_crate_hint_matches_multiple_roots_for_same_name() -> None:
    # Round 23 Fix B (``.features/plans/round23/
    # 09-subtypes-ambiguous-resolution-rate.md``): when two directories
    # both convention-match the same crate name (the real
    # ``crates/gpui`` plus zed's own
    # ``tooling/lints/test_fixture/gpui`` synthetic fixture), a
    # candidate under *either* registered root must match -- previously
    # ``_rust_crate_roots_index``'s single-root-per-name dict silently
    # dropped whichever root lost the last-write-wins race, so a
    # candidate under the dropped root never matched at all, and (live
    # measurement against zed showed) which root won was a genuine
    # 50/50 coin flip per process hash seed.
    crate_roots = {
        "gpui": [
            "crates/gpui/src",
            "tooling/lints/test_fixture/gpui/src",
        ]
    }
    assert resolver_mod._rust_crate_hint_matches(
        "gpui::Render", "crates/gpui/src/element.rs", crate_roots
    )
    assert resolver_mod._rust_crate_hint_matches(
        "gpui::Render",
        "tooling/lints/test_fixture/gpui/src/lib.rs",
        crate_roots,
    )
    assert not resolver_mod._rust_crate_hint_matches(
        "gpui::Render", "crates/unrelated/src/lib.rs", crate_roots
    )


def test_rust_crate_roots_index_all_keeps_every_matching_root() -> None:
    # Round 23 Fix B: ``_rust_crate_roots_index_all`` must retain every
    # directory matching a given crate name, not just the last one
    # encountered -- the collision-aware sibling of
    # ``_rust_crate_roots_index``, which intentionally keeps its
    # existing single-winner behavior (see that function's own
    # docstring) since ``resolve_imports()``'s Rust ``use``-resolution
    # path needs exactly one directory to resolve against.
    paths = frozenset(
        {
            "crates/gpui/src/lib.rs",
            "crates/gpui/src/element.rs",
            "tooling/lints/test_fixture/gpui/src/lib.rs",
            "crates/other/src/lib.rs",
        }
    )
    roots = resolver_mod._rust_crate_roots_index_all(paths)
    assert sorted(roots["gpui"]) == sorted(
        [
            "crates/gpui/src",
            "tooling/lints/test_fixture/gpui/src",
        ]
    )
    assert roots["other"] == ["crates/other/src"]


def test_rust_crate_roots_index_all_deduplicates_same_directory() -> None:
    # A crate with both ``lib.rs`` and a binary ``main.rs`` in the same
    # ``src/`` directory must register that directory once, not twice.
    paths = frozenset(
        {
            "crates/app/src/lib.rs",
            "crates/app/src/main.rs",
        }
    )
    roots = resolver_mod._rust_crate_roots_index_all(paths)
    assert roots["app"] == ["crates/app/src"]


def test_import_match_uses_receiver_as_rust_crate_hint_fallback() -> None:
    """Round 23 Fix A (``.features/plans/round23/
    09-subtypes-ambiguous-resolution-rate.md``): a fully-qualified
    ``impl gpui::Render for X`` heritage clause has ``name="Render"``,
    ``receiver="gpui"``, and (by construction of this fixture) no
    ``file_imports`` entry for either name -- the ordinary
    ``hints``-building step in ``_import_match`` finds nothing to loop
    over at all. Fix A's fallback tries ``call.receiver`` itself as a
    bare crate-name hint once the ``hints`` loop comes up empty,
    resolving via ``crate_roots`` directly. Isolated unit test, before
    Fix B's multi-root collision handling is layered on -- exactly one
    matching root here.
    """
    render_candidate = Symbol(
        id="crates/gpui/src/element.rs::Render",
        name="Render",
        qualname="Render",
        kind="trait",
        path="crates/gpui/src/element.rs",
        language="rust",
    )
    unrelated_candidate = Symbol(
        id="crates/other/src/lib.rs::Render",
        name="Render",
        qualname="Render",
        kind="trait",
        path="crates/other/src/lib.rs",
        language="rust",
    )
    call = RawCall(
        caller_id=None,
        path="crates/editor/src/editor.rs",
        text="gpui::Render",
        name="Render",
        receiver="gpui",
        line=1,
    )
    result = resolver_mod._import_match(
        call,
        [render_candidate, unrelated_candidate],
        file_imports={},
        raw_imports=None,
        crate_roots={"gpui": ["crates/gpui/src"]},
    )
    assert result is render_candidate


def test_pick_candidate_returns_none_when_language_filtered_empty() -> None:
    """End-to-end ``_pick_candidate`` pin for the tensorflow shape: a
    C++ call site with only a same-bare-name Python candidate must
    return ``None`` (defer to ambiguous), not the wrong-language
    ``Symbol`` -- this exercises every downstream ladder step
    (receiver-type, typed-param, same-file, import-hint, noise-guard,
    single-candidate fast path, and the last-resort steps) against an
    empty ``candidates`` list, pinning down that each one tolerates it
    rather than assuming they do."""
    python_candidate = Symbol(
        id="a.py::InvalidArgumentError",
        name="InvalidArgumentError",
        qualname="InvalidArgumentError",
        kind="class",
        path="a.py",
        language="python",
    )
    call = RawCall(
        caller_id=None,
        path="caller.cc",
        text="InvalidArgumentError",
        name="InvalidArgumentError",
        line=1,
    )
    result = resolver_mod._pick_candidate(
        call,
        [python_candidate],
        same_file=[],
        file_imports={},
        caller=None,
        by_name_path={},
        index={},
        repo_stems=set(),
    )
    assert result is None


def test_resolve_call_records_cross_family_miss_as_ambiguous() -> None:
    """Full ``_resolve_call``/``resolve()`` integration test for the
    residual tensorflow gap (`.features/fixes/resolver-vendored-
    exclusion-false-match.md`): the real C++ target lives outside the
    map entirely (simulating a vendored/excluded directory), leaving
    only a same-bare-name, unrelated-language Python class as the sole
    candidate. This must land in ``graph.ambiguous`` -- not silently
    resolve to the Python symbol as an edge -- so
    ``query.py``'s existing ambiguous-call disclosure surfaces it
    instead of reporting a confidently wrong fan-in."""
    python_class = Symbol(
        id="tensorflow/python/framework/errors_impl.py::InvalidArgumentError",
        name="InvalidArgumentError",
        qualname="InvalidArgumentError",
        kind="class",
        path="tensorflow/python/framework/errors_impl.py",
        language="python",
    )
    caller = Symbol(
        id="tensorflow/core/kernels/foo.cc::Compute",
        name="Compute",
        qualname="Compute",
        kind="method",
        path="tensorflow/core/kernels/foo.cc",
        language="cpp",
    )
    files = [
        FileMap(
            "tensorflow/python/framework/errors_impl.py",
            "python",
            symbols=[python_class],
        ),
        FileMap(
            "tensorflow/core/kernels/foo.cc",
            "cpp",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="tensorflow/core/kernels/foo.cc",
                    text="errors::InvalidArgumentError",
                    name="InvalidArgumentError",
                    line=5,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, python_class.id) not in edges
    assert edges == set()
    assert len(graph.ambiguous) == 1
    ambiguous_caller, ambiguous_name, ambiguous_cands = graph.ambiguous[0]
    assert ambiguous_caller == caller.id
    assert ambiguous_name == "InvalidArgumentError"
    assert ambiguous_cands == [python_class.id]


def test_cross_language_bare_call_no_longer_resolves_to_wrong_symbol() -> None:
    """Round 21 (tensorflow.md §5, Issue 6, Track D): a C++ namespace-
    qualified factory call (``errors::InvalidArgumentError(...)``,
    extracted with no receiver -- C++ namespace qualifiers are not
    treated as a receiver the way a ``.``/``->`` method call is) used
    to be able to resolve to a same-bare-name Python class defined
    completely unrelated elsewhere in the repo. The real mechanism:
    the actual C++ target is itself method-kind, so
    ``_bare_call_non_method_match``'s "drop every method-kind
    candidate" step used to drop it too, leaving the Python class as
    the *sole* surviving non-method candidate -- a confidently wrong
    single-candidate resolution, not even flagged ambiguous.

    ``_pick_candidate`` now drops the Python candidate before any
    ladder step runs at all (it's a different language than the C++
    call site), so the call resolves directly to the real C++ target
    via the earlier single-candidate fast path, never reaching -- or
    needing -- the non-method fallback."""
    python_class = Symbol(
        id="tensorflow/python/framework/errors_impl.py::InvalidArgumentError",
        name="InvalidArgumentError",
        qualname="InvalidArgumentError",
        kind="class",
        path="tensorflow/python/framework/errors_impl.py",
        language="python",
    )
    cpp_factory = Symbol(
        id="tensorflow/core/platform/errors.cc::errors.InvalidArgumentError",
        name="InvalidArgumentError",
        qualname="errors.InvalidArgumentError",
        kind="method",
        path="tensorflow/core/platform/errors.cc",
        language="cpp",
    )
    caller = Symbol(
        id="tensorflow/core/kernels/foo.cc::Compute",
        name="Compute",
        qualname="Compute",
        kind="method",
        path="tensorflow/core/kernels/foo.cc",
        language="cpp",
    )
    files = [
        FileMap(
            "tensorflow/python/framework/errors_impl.py",
            "python",
            symbols=[python_class],
        ),
        FileMap(
            "tensorflow/core/platform/errors.cc",
            "cpp",
            symbols=[cpp_factory],
        ),
        FileMap(
            "tensorflow/core/kernels/foo.cc",
            "cpp",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="tensorflow/core/kernels/foo.cc",
                    text="errors::InvalidArgumentError",
                    name="InvalidArgumentError",
                    line=5,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, cpp_factory.id) in edges
    assert (caller.id, python_class.id) not in edges
    assert graph.ambiguous == []


# --- Go cross-package qualified-call resolution (round-13 §1) -------------


def test_go_qualified_call_resolves_to_correct_subpackage() -> None:
    """Round-13 master report §1: a qualified call through an imported
    first-party Go subpackage selector (``slug.Generate(...)``) used
    to be dropped entirely -- ``_repo_stem`` compared the import
    source against the *file's own* stem (``"generator"``), which
    never appears in a Go import path, instead of the package
    directory name (``"slug"``), which does. Reproduces the exact
    awesome-go shape: two same-named-symbol packages, a caller
    importing only one, calling it via selector -- the call must
    resolve to that package's ``Generate``, not fall through to
    ``external`` or land in ``ambiguous``."""
    slug_generate = Symbol(
        id="pkg/slug/generator.go::Generate",
        name="Generate",
        qualname="Generate",
        kind="function",
        path="pkg/slug/generator.go",
        language="go",
    )
    markdown_generate = Symbol(
        id="pkg/markdown/id.go::Generate",
        name="Generate",
        qualname="Generate",
        kind="function",
        path="pkg/markdown/id.go",
        language="go",
    )
    caller = _fn("cmd/main.go", "main")
    files = [
        FileMap("pkg/slug/generator.go", "go", symbols=[slug_generate]),
        FileMap("pkg/markdown/id.go", "go", symbols=[markdown_generate]),
        FileMap(
            "cmd/main.go",
            "go",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="cmd/main.go",
                    text="slug.Generate",
                    name="Generate",
                    receiver="slug",
                    line=3,
                )
            ],
            imports=[
                Import(
                    path="cmd/main.go",
                    name="slug",
                    source="github.com/avelino/awesome-go/pkg/slug",
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, slug_generate.id) in edges
    assert (caller.id, markdown_generate.id) not in edges
    assert graph.ambiguous == []
    assert not any(ext.callee.startswith("slug.") for ext in graph.external)


def test_go_same_package_split_across_files_unaffected_by_stem_fix() -> None:
    """Regression guard: a single Go package split across two files
    (``pkg/a/one.go`` calling into ``pkg/a/two.go``) has no import
    statement for its own package, so it never reaches
    ``_import_match``/``_repo_stem`` at all -- resolution goes through
    the same-file/bare-name ladder instead. Confirms the directory-
    name stem change doesn't accidentally change that path's
    outcome."""
    helper = Symbol(
        id="pkg/a/two.go::Helper",
        name="Helper",
        qualname="Helper",
        kind="function",
        path="pkg/a/two.go",
        language="go",
    )
    caller = _fn("pkg/a/one.go", "Entry")
    files = [
        FileMap("pkg/a/two.go", "go", symbols=[helper]),
        FileMap(
            "pkg/a/one.go",
            "go",
            symbols=[caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="pkg/a/one.go",
                    text="Helper",
                    name="Helper",
                    line=2,
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, helper.id) in edges
    assert graph.ambiguous == []


def test_go_method_call_to_same_named_imported_function_not_dropped() -> None:
    """Round-14 report awesome-go.md §1.1: a method whose bare name
    equals the free function it calls in another package
    (``IDGenerator.Generate`` calling ``slug.Generate(...)``) used to
    resolve to itself via the same-file step -- the sole ``Generate``
    symbol in ``convert.go`` was the caller, so ``len(same_file) == 1``
    returned the caller's own symbol, and ``_add_edge``'s self-
    recursion filter silently discarded the "edge." The call must
    resolve to ``pkg/slug``'s ``Generate``, not to itself, not to
    ``ambiguous``, and not to ``external``."""
    slug_generate = Symbol(
        id="pkg/slug/generator.go::Generate",
        name="Generate",
        qualname="Generate",
        kind="function",
        path="pkg/slug/generator.go",
        language="go",
    )
    id_generator_type = Symbol(
        id="pkg/markdown/convert.go::IDGenerator",
        name="IDGenerator",
        qualname="IDGenerator",
        kind="struct",
        path="pkg/markdown/convert.go",
        language="go",
    )
    caller = Symbol(
        id="pkg/markdown/convert.go::IDGenerator.Generate",
        name="Generate",
        qualname="IDGenerator.Generate",
        kind="method",
        path="pkg/markdown/convert.go",
        language="go",
    )
    files = [
        FileMap("pkg/slug/generator.go", "go", symbols=[slug_generate]),
        FileMap(
            "pkg/markdown/convert.go",
            "go",
            symbols=[id_generator_type, caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="pkg/markdown/convert.go",
                    text="slug.Generate",
                    name="Generate",
                    receiver="slug",
                    line=46,
                )
            ],
            imports=[
                Import(
                    path="pkg/markdown/convert.go",
                    name="slug",
                    source="github.com/avelino/awesome-go/pkg/slug",
                )
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, slug_generate.id) in edges
    assert (caller.id, caller.id) not in edges
    assert graph.ambiguous == []
    assert not any(ext.callee.startswith("slug.") for ext in graph.external)


def test_python_method_call_to_same_named_imported_function_not_dropped() -> (
    None
):
    """Not Go-specific, per the round-14 design doc's root-cause
    finding: the identical bare-name collision shape in Python --
    ``Processor.process`` calling an imported, unrelated module's
    ``process`` function -- must resolve to the import, not silently
    vanish via the same-file self-collision the fix above targets."""
    helper_process = _fn("helper.py", "process")
    processor_type = _fn("main.py", "Processor", "Processor", line=1)
    caller = _fn("main.py", "process", "Processor.process", line=2)
    files = [
        FileMap("helper.py", "python", symbols=[helper_process]),
        FileMap(
            "main.py",
            "python",
            symbols=[processor_type, caller],
            calls=[
                RawCall(
                    caller_id=caller.id,
                    path="main.py",
                    text="helper.process",
                    name="process",
                    receiver="helper",
                    line=3,
                )
            ],
            imports=[
                Import(path="main.py", name="helper", source="helper"),
            ],
        ),
    ]
    graph = resolve(files)
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert (caller.id, helper_process.id) in edges
    assert (caller.id, caller.id) not in edges
    assert graph.ambiguous == []


def test_bare_and_self_recursive_calls_still_produce_no_edge() -> None:
    """Regression guard for the same-file self-exclusion fix above:
    genuine recursion -- a bare call to one's own name, and a
    self-qualified call to one's own method -- must still resolve to
    no edge (self-recursion is deliberately never recorded), exactly
    as before this change. The bare call now falls through the
    (skipped) same-file step and lands on the later
    ``len(candidates) == 1`` fast path, which still resolves to the
    caller itself and is still dropped by ``_add_edge``; the
    self-qualified call is caught earlier still, by
    ``_container_match``, which resolves it back to the same method
    and is dropped the same way."""
    recurse = Symbol(
        id="c.py::recurse",
        name="recurse",
        qualname="recurse",
        kind="function",
        path="c.py",
        language="python",
    )
    cls = Symbol(
        id="c.py::C",
        name="C",
        qualname="C",
        kind="class",
        path="c.py",
        language="python",
    )
    method = Symbol(
        id="c.py::C.walk",
        name="walk",
        qualname="C.walk",
        kind="method",
        path="c.py",
        language="python",
    )
    fm = FileMap(
        path="c.py",
        language="python",
        symbols=[recurse, cls, method],
        calls=[
            RawCall(
                caller_id=recurse.id,
                path="c.py",
                text="recurse",
                name="recurse",
                line=2,
            ),
            RawCall(
                caller_id=method.id,
                path="c.py",
                text="self.walk",
                name="walk",
                receiver="self",
                line=6,
            ),
        ],
    )
    graph = resolve([fm])
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert not edges
    assert graph.ambiguous == []


def test_same_file_two_candidates_including_caller_unaffected() -> None:
    """Regression guard: when ``same_file`` already has 2+ candidates
    (including the caller), the same-file step already skips its
    single-candidate fast path today, before this fix's new self-
    exclusion branch even runs -- the branch is nested inside the
    existing ``len(same_file) == 1`` check, so it structurally cannot
    fire here. Pinned explicitly since this is the case most likely to
    be miscoded if a future edit tries to "simplify" the fix into a
    list-filter instead of the single-candidate identity check.

    Deliberately not named ``build`` (a round-23 noise-guard denylist
    name, see ``_BUILDER_METHOD_NAMES``) — a receiver-qualified call
    to a denylisted name is suppressed to the ``_NOISE`` sentinel
    before ``_pick_candidate`` ever reaches the ambiguous fallback this
    test is pinning, which is a different code path than what this
    test exists to guard.
    """
    caller = Symbol(
        id="c.py::Widget.render",
        name="render",
        qualname="Widget.render",
        kind="method",
        path="c.py",
        language="python",
    )
    other = Symbol(
        id="c.py::render",
        name="render",
        qualname="render",
        kind="function",
        path="c.py",
        language="python",
    )
    fm = FileMap(
        path="c.py",
        language="python",
        symbols=[caller, other],
        calls=[
            RawCall(
                caller_id=caller.id,
                path="c.py",
                text="thing.render",
                name="render",
                receiver="thing",
                line=3,
            )
        ],
    )
    graph = resolve([fm])
    edges = {(e.caller, e.callee) for e in graph.edges}
    assert not edges
    assert graph.ambiguous == [(caller.id, "render", [caller.id, other.id])]
