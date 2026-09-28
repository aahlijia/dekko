"""Kotlin extraction: declarations, constructors, calls, imports.

Kotlin is a Tier-1 language on the ``tree-sitter-kotlin`` grammar.
Its constructors are named after their class (``Foo.Foo``), the same
shape as Java's, so the resolver's constructor pick applies to both.
"""

from pathlib import Path

import pytest

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.core.model import FileMap, Symbol

_SAMPLE = """\
package a.b

import x.y.Z as Zed
import x.y.W

class Foo(val p: Int, q: String = "d") : Base(p), Iface {

    constructor(s: String) : this(s.length)

    fun `does a thing`(vararg xs: Int, flag: Boolean = false): Int {
        return baz(xs.size)
    }

    companion object {
        fun create() = Foo(1)
    }
}

fun String.ext(n: Int): String = this

fun use() {
    runApplication<App>(*args) {
        setBannerMode(OFF)
    }
    "s".ext(1)
    a.b.Util.go(2, 3)
}
"""


def _extract(tmp_path: Path, source: str, name: str = "Sample.kt") -> FileMap:
    spec = languages.spec_for_path(name)
    assert spec is not None
    assert spec.name == "kotlin"
    (tmp_path / name).write_text(source)
    fm = extract_file(tmp_path, name, spec)
    assert fm.error is None
    return fm


def _by_id(fm: FileMap) -> dict[str, Symbol]:
    return {s.id: s for s in fm.symbols}


def _params(sym: Symbol) -> list[tuple[str, str | None, bool, bool]]:
    return [(p.name, p.type, p.has_default, p.variadic) for p in sym.params]


def test_kotlin_is_tier1_for_kt_and_kts() -> None:
    for name in ("A.kt", "build.gradle.kts"):
        spec = languages.spec_for_path(name)
        assert spec is not None
        assert spec.name == "kotlin"
    assert languages.tier2_grammar_for_path("A.kt") is None


def test_declarations_and_constructors(tmp_path: Path) -> None:
    syms = _by_id(_extract(tmp_path, _SAMPLE))
    assert [(s.qualname, s.kind, s.start_line) for s in syms.values()] == [
        ("Foo", "class", 6),
        ("Foo.Foo", "method", 6),
        ("Foo.Foo", "method", 8),
        ("Foo.does a thing", "method", 10),
        ("Foo.create", "method", 15),
        ("ext", "function", 19),
        ("use", "function", 21),
    ]
    assert "Sample.kt::Foo.Foo#2" in syms


def test_parameters_defaults_and_vararg(tmp_path: Path) -> None:
    syms = _by_id(_extract(tmp_path, _SAMPLE))
    assert _params(syms["Sample.kt::Foo.Foo"]) == [
        ("p", "Int", False, False),
        ("q", "String", True, False),
    ]
    assert _params(syms["Sample.kt::Foo.Foo#2"]) == [
        ("s", "String", False, False),
    ]
    thing = syms["Sample.kt::Foo.does a thing"]
    assert _params(thing) == [
        ("vararg xs", "Int", False, True),
        ("flag", "Boolean", True, False),
    ]
    assert thing.returns == "Int"
    assert syms["Sample.kt::ext"].returns == "String"


def test_calls_names_receivers_and_arg_counts(tmp_path: Path) -> None:
    fm = _extract(tmp_path, _SAMPLE)
    calls = [(c.name, c.receiver, c.arg_count, c.caller_id) for c in fm.calls]
    assert calls == [
        ("baz", None, 1, "Sample.kt::Foo.does a thing"),
        ("Foo", None, 1, "Sample.kt::Foo.create"),
        # A spread makes the count unknowable; recorded once, not
        # again for the trailing-lambda wrapper around it.
        ("runApplication", None, None, "Sample.kt::use"),
        ("setBannerMode", None, 1, "Sample.kt::use"),
        ("ext", '""', 1, "Sample.kt::use"),
        ("go", "a.b.Util", 2, "Sample.kt::use"),
    ]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("run(1) { it }", ("run", None, 2)),
        ("listOf(1).map { it + 1 }", ("map", "listOf()", 1)),
        ("apply { }", ("apply", None, 1)),
        ("f(a = 1, b = 2)", ("f", None, 2)),
        ("this.go()", ("go", "this", 0)),
        ("this@Outer.go()", ("go", "this", 0)),
        ("super.go(1)", ("go", "super", 1)),
        ("obj?.go(1)", ("go", "obj", 1)),
    ],
)
def test_call_shapes(
    tmp_path: Path,
    body: str,
    expected: tuple[str, str | None, int | None],
) -> None:
    fm = _extract(tmp_path, f"fun t() {{\n    {body}\n}}\n")
    assert (fm.calls[0].name, fm.calls[0].receiver, fm.calls[0].arg_count) == (
        expected
    )
    assert len([c for c in fm.calls if c.name == expected[0]]) == 1


def test_invoked_call_result_has_no_name(tmp_path: Path) -> None:
    fm = _extract(tmp_path, "fun t() {\n    f()()\n}\n")
    assert [c.name for c in fm.calls] == ["f"]


def test_imports_with_alias(tmp_path: Path) -> None:
    fm = _extract(tmp_path, _SAMPLE)
    assert [(i.name, i.source) for i in fm.imports] == [
        ("Zed", "x.y.Z"),
        ("W", "x.y.W"),
    ]


def test_heritage_superclass_and_interfaces(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        _SAMPLE
        + "\ninterface Child : Shape\n\nclass Impl : Runner by other\n",
    )
    assert [(h.name, h.relation, h.subtype_id) for h in fm.heritage] == [
        ("Base", "extends", "Sample.kt::Foo"),
        ("Iface", "implements", "Sample.kt::Foo"),
        ("Shape", "extends", "Sample.kt::Child"),
        ("Runner", "implements", "Sample.kt::Impl"),
    ]


_KINDS = """\
enum class Color {
    RED, GREEN;

    fun f() {}
}

sealed interface Shape

fun interface Runner {
    fun run()
}

object Single {
    fun one() = 1
}

data class D(val a: Int)
"""


def test_type_kinds_and_object_members(tmp_path: Path) -> None:
    fm = _extract(tmp_path, _KINDS)
    assert [(s.qualname, s.kind) for s in fm.symbols] == [
        ("Color", "enum"),
        ("Color.f", "method"),
        ("Shape", "interface"),
        ("Runner", "interface"),
        ("Runner.run", "method"),
        ("Single", "class"),
        ("Single.one", "method"),
        ("D", "class"),
        ("D.D", "method"),
    ]


def test_local_function_is_not_a_member(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "class C {\n    fun outer() {\n        fun local() {}\n"
        "        local()\n    }\n}\n",
    )
    assert [(s.qualname, s.kind) for s in fm.symbols] == [
        ("C", "class"),
        ("C.outer", "method"),
        ("local", "function"),
    ]


def test_annotation_and_visibility_flags(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "class T {\n    @Test\n    fun a() {}\n\n"
        "    private fun b() {}\n\n    internal fun c() {}\n\n"
        "    fun d() {}\n}\n",
    )
    flags = {s.name: (s.decorated, s.exported) for s in fm.symbols}
    assert flags["a"] == (True, True)
    assert flags["b"] == (False, False)
    assert flags["c"] == (False, False)
    assert flags["d"] == (False, True)


def test_system_getenv_is_an_env_read(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path, 'fun t() {\n    val h = System.getenv("HOME")\n}\n'
    )
    assert [(r.call, r.key) for r in fm.env_reads] == [
        ("System.getenv", "HOME"),
    ]


def test_object_expression_constructs_its_superclass(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "fun f() {\n    val x = object : a.Base<T>(1, 2) {\n"
        "        fun g() {}\n    }\n    val y = object : Iface {}\n}\n",
    )
    assert [(c.name, c.receiver, c.arg_count) for c in fm.calls] == [
        ("Base", "a", 2),
    ]
