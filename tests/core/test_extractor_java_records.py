"""A Java record's canonical constructor is a symbol.

``record R(int a, int b)`` has a constructor ``R(int, int)`` whether or
not its body writes one. Without a symbol for it, a construction from
another file was judged against the record's own empty parameter list
and dropped as external.
"""

from pathlib import Path

from dekko.core.extractor import extract_file
from dekko.core.languages import spec_for_path
from dekko.core.model import Symbol

_REL = "src/main/java/app/Records.java"


def _symbols(root: Path, text: str) -> dict[str, Symbol]:
    (root / _REL).parent.mkdir(parents=True, exist_ok=True)
    (root / _REL).write_text("package app;\n" + text)
    fm = extract_file(root, _REL, spec_for_path(_REL))
    return {s.id.split("::", 1)[1]: s for s in fm.symbols}


def _params(sym: Symbol) -> list[tuple[str, str | None]]:
    return [(p.name, p.type) for p in sym.params]


def test_a_record_without_a_constructor_gets_one_on_its_header(
    tmp_path: Path,
) -> None:
    syms = _symbols(tmp_path, "public record R(int a, String b) { }\n")
    assert set(syms) == {"R", "R.R"}
    ctor = syms["R.R"]
    assert ctor.kind == "method"
    assert ctor.start_line == 2
    assert _params(ctor) == [("a", "int"), ("b", "String")]
    assert syms["R"].params == []


def test_a_compact_constructor_is_the_canonical_one(tmp_path: Path) -> None:
    syms = _symbols(
        tmp_path,
        "record R(int a, String b) {\n"
        "    R {\n"
        "        check(a);\n"
        "    }\n"
        "    static void check(int a) { }\n"
        "}\n",
    )
    assert [q for q in syms if q.startswith("R.R")] == ["R.R"]
    assert syms["R.R"].start_line == 3
    assert _params(syms["R.R"]) == [("a", "int"), ("b", "String")]

    fm = extract_file(tmp_path, _REL, spec_for_path(_REL))
    callers = {c.caller_id for c in fm.calls if c.name == "check"}
    assert callers == {f"{_REL}::R.R"}


def test_an_explicit_canonical_constructor_replaces_the_header(
    tmp_path: Path,
) -> None:
    syms = _symbols(
        tmp_path,
        "import java.util.List;\n"
        "record R(@Deprecated int a, List<@Deprecated String> b) {\n"
        "    R(int a, List<String>  b) { this.a = a; this.b = b; }\n"
        "}\n"
        "record Arr(int[] a) {\n"
        "    Arr(int a[]) { this.a = a; }\n"
        "}\n",
    )
    assert [q for q in syms if q.startswith("R.")] == ["R.R"]
    assert syms["R.R"].start_line == 4
    assert [q for q in syms if q.startswith("Arr.")] == ["Arr.Arr"]
    assert syms["Arr.Arr"].start_line == 7


def test_an_extra_constructor_precedes_the_canonical_one(
    tmp_path: Path,
) -> None:
    # The declared constructor had ``R.R`` before the header was a
    # symbol; the header takes the next number rather than move it.
    syms = _symbols(
        tmp_path,
        "record R(int a, int b) {\n    R(int a) { this(a, 0); }\n}\n",
    )
    assert _params(syms["R.R"]) == [("a", "int")]
    assert syms["R.R"].start_line == 3
    assert _params(syms["R.R#2"]) == [("a", "int"), ("b", "int")]
    assert syms["R.R#2"].start_line == 2


def test_a_compact_constructor_is_numbered_after_declared_ones(
    tmp_path: Path,
) -> None:
    text = (
        "record R(int a, String b) {\n"
        "    R {\n"
        "        check(a);\n"
        "    }\n"
        "    R(int a) { this(a, null); }\n"
        "    R(String b) { this(0, b); }\n"
        "    static void check(int a) { }\n"
        "}\n"
    )
    syms = _symbols(tmp_path, text)
    assert {q for q in syms if q.startswith("R.R")} == {
        "R.R",
        "R.R#2",
        "R.R#3",
    }
    assert (syms["R.R"].start_line, _params(syms["R.R"])) == (
        6,
        [("a", "int")],
    )
    assert (syms["R.R#2"].start_line, _params(syms["R.R#2"])) == (
        7,
        [("b", "String")],
    )
    compact = syms["R.R#3"]
    assert compact.start_line == 3
    assert _params(compact) == [("a", "int"), ("b", "String")]

    fm = extract_file(tmp_path, _REL, spec_for_path(_REL))
    callers = {c.caller_id for c in fm.calls if c.name == "check"}
    assert callers == {f"{_REL}::R.R#3"}


def test_a_declared_constructor_before_a_compact_one_keeps_r_r(
    tmp_path: Path,
) -> None:
    syms = _symbols(
        tmp_path,
        "record Host(String name, int port) {\n"
        "    Host(String name) { this(name, 80); }\n"
        "    Host { }\n"
        "}\n",
    )
    assert syms["Host.Host"].start_line == 3
    assert _params(syms["Host.Host"]) == [("name", "String")]
    assert syms["Host.Host#2"].start_line == 4


def test_an_explicit_canonical_keeps_source_order(tmp_path: Path) -> None:
    # A full canonical constructor was always a declared symbol, so
    # nothing about its number changed.
    syms = _symbols(
        tmp_path,
        "record R(int a, int b) {\n"
        "    R(int a) { this(a, 0); }\n"
        "    R(int a, int b) { this.a = a; this.b = b; }\n"
        "    R() { this(0, 0); }\n"
        "}\n",
    )
    lines = {q: s.start_line for q, s in syms.items() if q.startswith("R.R")}
    assert lines == {"R.R": 3, "R.R#2": 4, "R.R#3": 5}


def test_canonical_constructors_of_two_records_keep_their_slots(
    tmp_path: Path,
) -> None:
    # Deferring the numbering must not reorder the symbol list:
    # callers are attributed by walking it in source order.
    fm_text = (
        "record A(int a) {\n"
        "    A() { this(0); }\n"
        "}\n"
        "class Mid { void m() { } }\n"
        "record B(int b) {\n"
        "    B { }\n"
        "    B(String s) { this(1); }\n"
        "}\n"
    )
    (tmp_path / _REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / _REL).write_text("package app;\n" + fm_text)
    fm = extract_file(tmp_path, _REL, spec_for_path(_REL))
    order = [s.id.split("::", 1)[1] for s in fm.symbols]
    assert order == [
        "A",
        "A.A#2",
        "A.A",
        "Mid",
        "Mid.m",
        "B",
        "B.B#2",
        "B.B",
    ]


def test_generic_varargs_and_empty_records(tmp_path: Path) -> None:
    syms = _symbols(
        tmp_path,
        "record Pair<A, B>(A a, B b) { }\n"
        "record Parts(String... parts) { }\n"
        "record Empty() { }\n",
    )
    assert _params(syms["Pair.Pair"]) == [("a", "A"), ("b", "B")]
    (parts,) = syms["Parts.Parts"].params
    assert (parts.name, parts.type, parts.variadic) == (
        "parts",
        "String...",
        True,
    )
    assert syms["Empty.Empty"].params == []


def test_the_canonical_constructor_has_the_records_access(
    tmp_path: Path,
) -> None:
    syms = _symbols(
        tmp_path,
        "public record Open(int a) {\n"
        "    public record Inner(int b) { }\n"
        "}\n"
        "record Pkg(int a) { }\n"
        "public class Holder {\n"
        "    private record Hidden(int z) { }\n"
        "    private static class Shut {\n"
        "        public record Within(int w) { }\n"
        "    }\n"
        "}\n",
    )
    assert syms["Open.Open"].visibility is None
    assert syms["Open.Inner.Inner"].visibility is None
    assert syms["Pkg.Pkg"].visibility == "package"
    assert syms["Holder.Hidden.Hidden"].visibility == "private"
    assert syms["Holder.Shut.Within.Within"].visibility == "private"
