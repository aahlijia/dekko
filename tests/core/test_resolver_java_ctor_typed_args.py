"""Java constructor overloads narrowed by a named argument's type.

``new Instantiator<>(parent, loaderClassName)`` against a
``(ClassLoader, Class<?>)`` and a ``(ClassLoader, String)`` overload
is a tie by count, and neither argument is a literal. Java decides it
by the arguments' static types, and for a plain name that type is
written at its declaration: a parameter, a local, a field. dekko reads
it from there and rules out what a literal of that type would.
"""

from pathlib import Path

import pytest

from dekko.core.extractor import extract_file
from dekko.core.languages import spec_for_path
from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

_USE = "app/Use.java"


def _graph(root: Path, sources: dict[str, str]) -> CallGraph:
    for rel, text in sources.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    files, _ = map_repository(
        root,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
    )
    return resolve(files)


def _class(name: str, *ctors: str) -> str:
    body = "".join(f"    public {name}({c}) {{ }}\n" for c in ctors)
    return f"package app;\npublic class {name} {{\n{body}}}\n"


def _ctor(name: str, n: int) -> str:
    suffix = f"#{n}" if n > 1 else ""
    return f"app/{name}.java::{name}.{name}{suffix}"


def _picks(
    tmp_path: Path,
    target: str,
    use_body: str,
    caller: str = "Use.m",
) -> tuple[set[str], dict[str, list[str]]]:
    """The constructors ``caller`` picks for ``target`` and its
    ambiguous rows, with ``use_body`` as class ``Use``'s body."""
    graph = _graph(
        tmp_path,
        {
            "app/Use.java": (
                "package app;\nimport java.io.File;\n"
                f"class Use {{\n{use_body}}}\n"
            ),
        },
    )
    caller_id = f"{_USE}::{caller}"
    cls = f"app/{target}.java::{target}"
    picked = {
        e.callee
        for e in graph.edges
        if e.caller == caller_id and e.callee.startswith(cls + ".")
    }
    ambiguous = {
        n: sorted(ids) for c, n, ids in graph.ambiguous if c == caller_id
    }
    return picked, ambiguous


def _write(tmp_path: Path, rel: str, text: str) -> None:
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(text)


_STRING_OR_FILE = ("String path", "File file")


def test_the_instantiator_tie_is_settled_by_the_parameter_type(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "app/Instantiator.java",
        "package app;\n"
        "record Instantiator<T>(ClassLoader parent, Class<?> type) {\n"
        "    Instantiator(ClassLoader parent, String name) {\n"
        "        this(parent, (Class<?>) null);\n"
        "    }\n"
        "}\n",
    )
    body = (
        "    Object m(ClassLoader parent, String name) {\n"
        "        return new Instantiator<>(parent, name);\n"
        "    }\n"
        "    Object k(ClassLoader parent, Class<?> type) {\n"
        "        return new Instantiator<>(parent, type);\n"
        "    }\n"
    )
    picked, ambiguous = _picks(tmp_path, "Instantiator", body)
    # The declared constructor is ``Instantiator.Instantiator``, the
    # canonical one ``#2``.
    assert picked == {_ctor("Instantiator", 1)}
    assert ambiguous == {}
    picked, ambiguous = _picks(tmp_path, "Instantiator", body, caller="Use.k")
    assert picked == {_ctor("Instantiator", 2)}
    assert ambiguous == {}


@pytest.mark.parametrize(
    ("use_body", "want"),
    [
        # A local before the construction.
        (
            "    Object m() {\n"
            '        File file = new File("x");\n'
            "        return new Target(file);\n"
            "    }\n",
            2,
        ),
        # A field through ``this.``, and bare.
        (
            '    String path = "x";\n'
            "    Object m() { return new Target(this.path); }\n",
            1,
        ),
        (
            '    String path = "x";\n'
            "    Object m() { return new Target(path); }\n",
            1,
        ),
        # A static final constant.
        (
            '    static final String PATH = "x";\n'
            "    Object m() { return new Target(PATH); }\n",
            1,
        ),
        # Enhanced for, catch, try-with-resources, a typed lambda.
        (
            "    void m(java.util.List<File> files) {\n"
            "        for (File f : files) { new Target(f); }\n"
            "    }\n",
            2,
        ),
        (
            "    void m() {\n"
            "        try (java.io.Reader r = null) { }\n"
            "        catch (Exception e) { String s = null; new Target(s); }\n"
            "    }\n",
            1,
        ),
        (
            "    void m() {\n"
            "        java.util.function.Function<File, Object> f =\n"
            "            (File file) -> new Target(file);\n"
            "    }\n",
            2,
        ),
        # A local in an earlier ``case`` group is in scope in a later
        # one, and wins over a field of another type.
        (
            "    File s;\n"
            "    void m(int k) {\n"
            "        switch (k) {\n"
            '            case 1: String s = "x"; break;\n'
            "            case 2: s = null; new Target(s);\n"
            "        }\n"
            "    }\n",
            1,
        ),
    ],
)
def test_a_declared_name_picks_its_overload(
    tmp_path: Path,
    use_body: str,
    want: int,
) -> None:
    _write(tmp_path, "app/Target.java", _class("Target", *_STRING_OR_FILE))
    picked, ambiguous = _picks(tmp_path, "Target", use_body)
    assert picked == {_ctor("Target", want)}
    assert ambiguous == {}


def test_a_parameter_shadows_a_field_of_another_type(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app/Outcome.java",
        _class(
            "Outcome",
            "boolean match, String message",
            "boolean match, Msg message",
        ),
    )
    _write(tmp_path, "app/Msg.java", "package app;\nclass Msg { }\n")
    body = (
        "    Msg message;\n"
        "    Object m(String message) { return new Outcome(true, message); }\n"
        "    Object f() { return new Outcome(true, message); }\n"
    )
    picked, ambiguous = _picks(tmp_path, "Outcome", body)
    assert picked == {_ctor("Outcome", 1)}
    assert ambiguous == {}
    picked, ambiguous = _picks(tmp_path, "Outcome", body, caller="Use.f")
    assert picked == {_ctor("Outcome", 2)}
    assert ambiguous == {}


def test_a_local_declared_after_the_construction_does_not_count(
    tmp_path: Path,
) -> None:
    # The field is what ``path`` means at the construction.
    _write(tmp_path, "app/Target.java", _class("Target", *_STRING_OR_FILE))
    picked, _ = _picks(
        tmp_path,
        "Target",
        "    File path;\n"
        "    Object m() {\n"
        "        Object t = new Target(path);\n"
        '        String path = "x";\n'
        "        return t;\n"
        "    }\n",
    )
    assert picked == {_ctor("Target", 2)}


def test_object_rules_out_only_closed_types(tmp_path: Path) -> None:
    _write(tmp_path, "app/A.java", _class("A", "String s", "Object o"))
    _write(
        tmp_path,
        "app/B.java",
        _class("B", "java.util.Collection<?> c", "Object o"),
    )
    body = (
        "    Object m(Object o) { return new A(o); }\n"
        "    Object k(Object o) { return new B(o); }\n"
    )
    picked, ambiguous = _picks(tmp_path, "A", body)
    assert picked == {_ctor("A", 2)}
    assert ambiguous == {}
    picked, ambiguous = _picks(tmp_path, "B", body, caller="Use.k")
    assert picked == set()
    assert ambiguous == {"B": [_ctor("B", 1), _ctor("B", 2)]}


def test_every_overload_ruled_out_keeps_the_count_tie(tmp_path: Path) -> None:
    _write(tmp_path, "app/C.java", _class("C", "String s", "int n"))
    _write(tmp_path, "app/Foo.java", "package app;\nclass Foo { }\n")
    picked, ambiguous = _picks(
        tmp_path,
        "C",
        "    Object m(Foo foo) { return new C(foo); }\n",
    )
    assert picked == set()
    assert ambiguous == {"C": [_ctor("C", 1), _ctor("C", 2)]}


_REL = "src/main/java/app/K.java"


def _kinds(tmp_path: Path, body: str) -> tuple[str, ...] | None:
    """``arg_kinds`` of the one ``new X(..)`` in ``body``."""
    _write(
        tmp_path,
        _REL,
        f"package app;\nclass K<T> extends Base {{\n{body}}}\n",
    )
    fm = extract_file(tmp_path, _REL, spec_for_path(_REL))
    (call,) = [c for c in fm.calls if c.name == "X"]
    return call.arg_kinds


@pytest.mark.parametrize(
    "body",
    [
        '    void m() { var x = "s"; new X(x); }\n',
        "    void m(String[] x) { new X(x); }\n",
        "    void m(String... x) { new X(x); }\n",
        "    void m() { String x[] = null; new X(x); }\n",
        "    void m(T x) { new X(x); }\n",
        "    void m(long x) { new X(x); }\n",
        "    void m(Double x) { new X(x); }\n",
        "    void m() { run(x -> new X(x)); }\n",
        "    void m() { run((x, y) -> new X(x)); }\n",
        "    void m() {\n"
        "        try { } catch (IllegalStateException | Error x) {\n"
        "            new X(x);\n"
        "        }\n"
        "    }\n",
        # An inherited field: not in this file.
        "    void m() { new X(inherited); }\n",
        # A pattern variable anywhere in the function, even outside the
        # lambda the construction sits in.
        "    String x;\n"
        "    void m(Object o) {\n"
        "        if (o instanceof String x) { }\n"
        "        run(() -> new X(x));\n"
        "    }\n",
        "    String x;\n"
        "    void m(Object o) {\n"
        "        if (o instanceof P(String x)) { }\n"
        "        new X(x);\n"
        "    }\n",
        # A call result or a field chain has no type.
        "    void m() { new X(make()); }\n",
        "    K other;\n    String x;\n    void m() { new X(this.other.x); }\n",
    ],
)
def test_names_without_a_usable_type_stay_unknown(
    tmp_path: Path,
    body: str,
) -> None:
    assert _kinds(tmp_path, body) is None


@pytest.mark.parametrize(
    ("body", "want"),
    [
        ("    void m(String x) { new X(x); }\n", ("string",)),
        ("    void m(Class<? extends K> x) { new X(x); }\n", ("class",)),
        ("    void m(Boolean x) { new X(x); }\n", ("bool",)),
        ("    void m(int x) { new X(x); }\n", ("int",)),
        ("    void m(Character x) { new X(x); }\n", ("char",)),
        ("    void m(java.io.@Ann File x) { new X(x); }\n", ("type:File",)),
        (
            "    void m(java.util.List<String> x) { new X(x); }\n",
            ("type:List",),
        ),
        (
            '    void m(String a) { new X(a, "b", 3); }\n',
            ("string", "string", "int"),
        ),
        # A compact constructor's names are its record's components.
        (
            "    record R(String x) { R { new X(x); } }\n",
            ("string",),
        ),
        # Inside a record's body, a component is a field.
        ("    record R(File x) { void m() { new X(x); } }\n", ("type:File",)),
    ],
)
def test_declared_types_give_kinds(
    tmp_path: Path,
    body: str,
    want: tuple[str, ...],
) -> None:
    assert _kinds(tmp_path, body) == want
