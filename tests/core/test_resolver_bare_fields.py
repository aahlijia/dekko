"""A Java, Kotlin or C++ field used without ``this``.

``repository.findAll()`` inside a method of a type with a
``repository`` field is a call on that field, typed through its
declared type the way ``this.repository.findAll()`` is. A parameter or
a local of the same name shadows the field, and every other one-segment
receiver resolves as it did before.
"""

import textwrap
from pathlib import Path

import pytest

from dekko.core import resolver
from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository


def _graph(root: Path, sources: dict[str, str]) -> CallGraph:
    for rel, text in sources.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text))
    files, _ = map_repository(
        root,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    return resolve(files, root=root)


def _callees(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.edges if e.caller == caller}


def _external(graph: CallGraph, caller: str) -> set[str]:
    return {e.callee for e in graph.external if e.caller == caller}


def _ambiguous(graph: CallGraph, caller: str) -> dict[str, set[str]]:
    return {name: set(ids) for c, name, ids in graph.ambiguous if c == caller}


def _outcome(graph: CallGraph, caller: str) -> tuple:
    return (
        _callees(graph, caller),
        _external(graph, caller),
        _ambiguous(graph, caller),
    )


def _without_bare_fields(
    monkeypatch: pytest.MonkeyPatch, root: Path, sources: dict[str, str]
) -> CallGraph:
    """The graph with the bare-field case switched off: what every
    receiver that isn't an unbound field must still resolve to."""
    with monkeypatch.context() as m:
        m.setattr(
            resolver,
            "_walk_bare_field",
            lambda *a: (resolver._WALK_UNKNOWN, 1),
            raising=False,
        )
        return _graph(root, sources)


_JAVA_REPOS = {
    "Repo.java": "class Repo { Helper helper; void findAll() {} }\n",
    "Other.java": "class Other { void findAll() {} }\n",
    "Helper.java": "class Helper { void go() {} }\n",
    "Spare.java": "class Spare { void go() {} }\n",
}


def test_java_bare_field_resolves_through_its_type(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **_JAVA_REPOS,
            "Svc.java": (
                "class Svc {\n"
                "  private Repo repository;\n"
                "  void run() { repository.findAll(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "Svc.java::Svc.run") == {"Repo.java::Repo.findAll"}


def test_java_bare_field_chain(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **_JAVA_REPOS,
            "Svc.java": (
                "class Svc {\n"
                "  private Repo repository;\n"
                "  void run() { repository.helper.go(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "Svc.java::Svc.run") == {"Helper.java::Helper.go"}


def test_java_bare_field_of_a_foreign_type_goes_external(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            "Bag.java": "class Bag { void add(Object o) {} }\n",
            "Bag2.java": "class Bag2 { void add(Object o) {} }\n",
            "Svc.java": (
                "import java.util.List;\n"
                "class Svc {\n"
                "  private List<Object> items;\n"
                "  void run(Object x) { items.add(x); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "Svc.java::Svc.run") == set()
    assert _ambiguous(g, "Svc.java::Svc.run") == {}
    assert "items.add" in _external(g, "Svc.java::Svc.run")


def test_java_inherited_bare_field(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **_JAVA_REPOS,
            "Base.java": "class Base { protected Repo repository; }\n",
            "Svc.java": (
                "class Svc extends Base {\n"
                "  void run() { repository.findAll(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "Svc.java::Svc.run") == {"Repo.java::Repo.findAll"}


def test_java_local_shadowing_a_field_is_not_walked(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **_JAVA_REPOS,
            "Svc.java": (
                "class Svc {\n"
                "  private Repo repository;\n"
                "  void run() {\n"
                "    Other repository = new Other();\n"
                "    repository.findAll();\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    assert "Repo.java::Repo.findAll" not in _callees(g, "Svc.java::Svc.run")


def test_lambda_param_shadowing_a_field_is_not_walked(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **_JAVA_REPOS,
            "Svc.java": (
                "class Svc {\n"
                "  private Repo repository;\n"
                "  void run(java.util.List<Other> items) {\n"
                "    items.forEach(repository -> repository.findAll());\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    assert "Repo.java::Repo.findAll" not in _callees(g, "Svc.java::Svc.run")


def test_java_param_shadowing_a_field_uses_the_param_type(
    tmp_path: Path,
) -> None:
    g = _graph(
        tmp_path,
        {
            **_JAVA_REPOS,
            "Svc.java": (
                "class Svc {\n"
                "  private Repo repository;\n"
                "  void run(Other repository) { repository.findAll(); }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "Svc.java::Svc.run") == {"Other.java::Other.findAll"}


_INNER = {
    **_JAVA_REPOS,
    "Outer.java": (
        "class Outer {\n"
        "  private Repo repository;\n"
        "  class Inner {\n"
        "    void run() { repository.findAll(); }\n"
        "  }\n"
        "}\n"
    ),
}


def test_inner_class_outer_field_stays_on_the_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = "Outer.java::Outer.Inner.run"
    before = _outcome(
        _without_bare_fields(monkeypatch, tmp_path / "a", _INNER), caller
    )
    after = _outcome(_graph(tmp_path / "b", _INNER), caller)
    assert after == before


def test_kotlin_constructor_val_field(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            "Repo.kt": "class Repo { fun findAll() {} }\n",
            "Other.kt": "class Other { fun findAll() {} }\n",
            "Svc.kt": (
                "class Svc(private val repo: Repo) {\n"
                "  fun run() { repo.findAll() }\n"
                "  fun shadow() {\n"
                "    val repo = Other()\n"
                "    repo.findAll()\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "Svc.kt::Svc.run") == {"Repo.kt::Repo.findAll"}
    assert "Repo.kt::Repo.findAll" not in _callees(g, "Svc.kt::Svc.shadow")


_CPP = {
    "repo.h": "class Repo {\n public:\n  void Go() {}\n};\n",
    "other.h": "class Other {\n public:\n  void Go() {}\n};\n",
}


def test_cpp_bare_pointer_and_smart_pointer_fields(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        {
            **_CPP,
            "svc.h": (
                '#include "repo.h"\n'
                "#include <memory>\n"
                "class Svc {\n"
                "  Repo* delegate_;\n"
                "  std::unique_ptr<Repo> owned_;\n"
                "  void Run();\n"
                "};\n"
            ),
            "svc.cc": (
                '#include "svc.h"\n'
                "void Svc::Run() {\n"
                "  delegate_->Go();\n"
                "  owned_->Go();\n"
                "}\n"
            ),
        },
    )
    assert _callees(g, "svc.cc::Svc.Run") == {"repo.h::Repo.Go"}


_CPP_FREE = {
    **_CPP,
    "svc.cc": (
        '#include "repo.h"\n'
        "class Svc {\n"
        "  Repo* delegate_;\n"
        "};\n"
        "Other* delegate_;\n"
        "void Free() { delegate_->Go(); }\n"
    ),
}


def test_cpp_free_function_bare_name_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = "svc.cc::Free"
    before = _outcome(
        _without_bare_fields(monkeypatch, tmp_path / "a", _CPP_FREE), caller
    )
    after = _outcome(_graph(tmp_path / "b", _CPP_FREE), caller)
    assert after == before


_TS_BARE = {
    "repo.ts": "export class Repo { find() {} }\n",
    "other.ts": "export class Other { find() {} }\n",
    "svc.ts": (
        "import { Repo } from './repo'\n"
        "import { Other } from './other'\n"
        "const repo = new Other()\n"
        "export class Svc {\n"
        "  repo: Repo\n"
        "  run() { repo.find() }\n"
        "}\n"
    ),
}


def test_ts_bare_name_is_never_a_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = "svc.ts::Svc.run"
    before = _outcome(
        _without_bare_fields(monkeypatch, tmp_path / "a", _TS_BARE), caller
    )
    after = _outcome(_graph(tmp_path / "b", _TS_BARE), caller)
    assert after == before
    assert "repo.ts::Repo.find" not in after[0]


_DEPTH_ZERO = {
    **_JAVA_REPOS,
    "Svc.java": (
        "class Svc {\n"
        "  private Other p;\n"
        "  void run(Repo p) { p.findAll(); Repo.create(); }\n"
        "}\n"
    ),
}


def test_depth_zero_receivers_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = "Svc.java::Svc.run"
    before = _outcome(
        _without_bare_fields(monkeypatch, tmp_path / "a", _DEPTH_ZERO),
        caller,
    )
    after = _outcome(_graph(tmp_path / "b", _DEPTH_ZERO), caller)
    assert after == before
