"""Java constructor overloads narrowed by literal arguments.

A construction's argument count leaves a choice wherever a varargs
overload also fits, and the count then says nothing. A literal
argument does: Java has no user-defined conversions, so a class
literal is never a ``ResourceLoader`` and a string never a
``boolean``.
"""

from pathlib import Path

import pytest

from dekko.core.model import CallGraph
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository

_BUILD = "app/Builder.java::Builder.build"


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


def _builder(body: str) -> str:
    return (
        "package app;\n"
        "public class Builder {\n"
        f"    Object build(Object a, Object b) {{ {body} }}\n"
        "}\n"
    )


def _picked(
    tmp_path: Path,
    name: str,
    ctors: tuple[str, ...],
    construction: str,
) -> tuple[set[str], dict[str, list[str]]]:
    graph = _graph(
        tmp_path,
        {
            f"app/{name}.java": _class(name, *ctors),
            "app/Builder.java": _builder(f"return {construction};"),
        },
    )
    cls = f"app/{name}.java::{name}"
    picked = {
        e.callee for e in graph.edges if e.caller == _BUILD and e.callee != cls
    }
    ambiguous = {n: ids for c, n, ids in graph.ambiguous if c == _BUILD}
    return picked, ambiguous


def _ctor(name: str, n: int) -> str:
    suffix = f"#{n}" if n > 1 else ""
    return f"app/{name}.java::{name}.{name}{suffix}"


_SPRING = ("Class<?>... sources", "ResourceLoader loader, Class<?>... sources")
_REST = (
    "RestTemplateBuilder builder, UriTemplateHandler handler",
    "String user, String password, HttpClientOption... options",
)
_SERVLET = (
    "T servlet, boolean alwaysMap, String... urls",
    "T servlet, String... urls",
)
_AUDIT = (
    "Instant at, String principal, String type, Map<String, Object> data",
    "String principal, String type, String... data",
)
_ERROR_PAGE = (
    "String path",
    "HttpStatus status, String path",
    "Class<? extends Throwable> exception, String path",
)


@pytest.mark.parametrize(
    ("name", "ctors", "construction", "want"),
    [
        # The count's exact tier picked the second overload: two
        # arguments, two declared parameters. A class literal is never
        # a ResourceLoader.
        (
            "SpringApplication",
            _SPRING,
            "new SpringApplication(A.class, B.class)",
            1,
        ),
        (
            "TestRestTemplate",
            _REST,
            'new TestRestTemplate("user", "password")',
            2,
        ),
        # A boolean is never a String, and a string never a boolean.
        (
            "ServletRegistrationBean",
            _SERVLET,
            "new ServletRegistrationBean<>(a, false)",
            1,
        ),
        (
            "ServletRegistrationBean",
            _SERVLET,
            'new ServletRegistrationBean<>(a, "/a", "/b")',
            2,
        ),
        (
            "AuditEvent",
            _AUDIT,
            'new AuditEvent("phil", "UNKNOWN", "a=b", "c=d")',
            2,
        ),
        ("ErrorPage", _ERROR_PAGE, 'new ErrorPage(Oops.class, "/500")', 3),
        # A type variable takes anything.
        ("Box", ("T value", "Integer count"), 'new Box<>("x")', 1),
        ("Holder", ("String s", "Foo f"), "new Holder(new Foo())", 2),
        ("Task", ("String name", "Runnable r"), "new Task(() -> run())", 2),
    ],
)
def test_literal_argument_picks_the_overload_it_can_be(
    tmp_path: Path,
    name: str,
    ctors: tuple[str, ...],
    construction: str,
    want: int,
) -> None:
    picked, ambiguous = _picked(tmp_path, name, ctors, construction)
    assert picked == {_ctor(name, want)}
    assert ambiguous == {}


def test_identifiers_only_keep_the_count_pick(tmp_path: Path) -> None:
    picked, ambiguous = _picked(
        tmp_path,
        "SpringApplication",
        _SPRING,
        "new SpringApplication(a, b)",
    )
    assert picked == {_ctor("SpringApplication", 2)}
    assert ambiguous == {}


def test_every_overload_ruled_out_keeps_the_count_choice(
    tmp_path: Path,
) -> None:
    # Neither HttpStatus nor Class can be an int: the evidence is not
    # trusted over the count, so the row stays as the count left it.
    picked, ambiguous = _picked(
        tmp_path,
        "ErrorPage",
        _ERROR_PAGE,
        'new ErrorPage(1, "/x")',
    )
    assert picked == set()
    assert ambiguous == {
        "ErrorPage": [_ctor("ErrorPage", 2), _ctor("ErrorPage", 3)],
    }


def test_null_may_be_the_whole_varargs_array(tmp_path: Path) -> None:
    # ``new Ints(null)`` passes null as the ``int[]`` itself, which
    # Java allows; it rules nothing out here.
    picked, ambiguous = _picked(
        tmp_path,
        "Ints",
        ("int... values", "String name"),
        "new Ints(null)",
    )
    assert picked == set()
    assert ambiguous == {"Ints": [_ctor("Ints", 1), _ctor("Ints", 2)]}


def test_one_overload_left_by_the_count_is_not_second_guessed(
    tmp_path: Path,
) -> None:
    # One fitting overload is the count's answer; a literal that
    # doesn't fit it isn't evidence for a constructor the count rules
    # out.
    picked, _ = _picked(
        tmp_path,
        "Pair",
        ("Integer a, Integer b", "String s"),
        'new Pair("x", "y")',
    )
    assert picked == {_ctor("Pair", 1)}
