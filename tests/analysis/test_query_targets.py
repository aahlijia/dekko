"""How a target string is read: a leading ``./``, an id's ``#N`` after a
single colon, and ``--lang`` narrowing the candidates."""

import pytest

from dekko.analysis import query
from dekko.integrations import cli
from dekko.render import mapfile

from conftest import RepoFactory

TWO_FILES = {
    "src/a.py": (
        "def helper(x: int) -> int:\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "def entry() -> None:\n"
        "    helper(1)\n"
    ),
}

OVERLOADED = {
    "Foo.java": (
        "class Foo {\n"
        "    void run(int x) {\n"
        "    }\n"
        "\n"
        "    void run(String x) {\n"
        "    }\n"
        "}\n"
    ),
}

MAINS = {
    "app/a.py": "def main() -> None:\n    pass\n",
    "cmd/main.go": "package main\n\nfunc main() {\n}\n",
}


@pytest.mark.parametrize(
    "argv",
    [
        ["outline", "./src/a.py"],
        ["query", "file", "./src/a.py"],
        ["query", "callers", "./src/a.py:helper"],
        ["query", "callers", "./src/a.py::helper"],
        ["query", "callers", "././src/a.py:helper"],
    ],
)
def test_a_leading_dot_slash_names_the_same_file(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture,
    argv: list[str],
) -> None:
    root = make_mapped_repo(TWO_FILES)
    assert cli.main([*argv, "--root", str(root)]) == 0
    assert "no mapped file" not in capsys.readouterr().err


def test_a_dot_slash_id_resolves_to_its_symbol(
    make_mapped_repo: RepoFactory,
) -> None:
    index = mapfile.load_map(make_mapped_repo(TWO_FILES))
    assert index is not None
    sym, _ = query.resolve_target(index, "./src/a.py::helper")
    assert sym is not None
    assert sym.id == "src/a.py::helper"


def test_an_overload_suffix_works_after_a_single_colon(
    make_mapped_repo: RepoFactory,
) -> None:
    index = mapfile.load_map(make_mapped_repo(OVERLOADED))
    assert index is not None
    by_id, _ = query.resolve_target(index, "Foo.java::Foo.run#2")
    assert by_id is not None

    sym, _ = query.resolve_target(index, "Foo.java:Foo.run#2")
    assert sym is not None
    assert sym.id == by_id.id


def test_a_wrong_overload_suffix_is_still_not_found(
    make_mapped_repo: RepoFactory,
) -> None:
    index = mapfile.load_map(make_mapped_repo(OVERLOADED))
    assert index is not None
    sym, candidates = query.resolve_target(index, "Foo.java:Foo.run#9")
    assert sym is None
    assert candidates == []


def test_lang_narrows_an_ambiguous_name_to_one(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(MAINS)
    assert cli.main(["query", "symbol", "main", "--root", str(root)]) == 4
    capsys.readouterr()

    code = cli.main(
        ["query", "symbol", "main", "--lang", "go", "--root", str(root)]
    )
    assert code == 0
    assert "cmd/main.go" in capsys.readouterr().out


def test_lang_filters_the_ambiguity_list(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {**MAINS, "tools/b.py": "def main() -> None:\n    pass\n"}
    )
    code = cli.main(
        ["query", "callers", "main", "--lang", "python", "--root", str(root)]
    )
    assert code == 4
    err = capsys.readouterr().err
    assert "app/a.py" in err
    assert "tools/b.py" in err
    assert "cmd/main.go" not in err


def test_lang_with_no_candidate_in_that_language_says_so(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(MAINS)
    code = cli.main(
        ["query", "symbol", "main", "--lang", "rust", "--root", str(root)]
    )
    assert code == 3
    err = capsys.readouterr().err
    assert "no rust symbol matches 'main'" in err
    assert "2 in other languages" in err


def test_lang_accepts_every_mapped_language(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(MAINS)
    for lang in ("c", "go", "starlark", "lua"):
        code = cli.main(
            ["query", "symbol", "main", "--lang", lang, "--root", str(root)]
        )
        assert code != 2, lang
    capsys.readouterr()


def test_lang_rejects_an_unknown_name(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(MAINS)
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "query",
                "symbol",
                "main",
                "--lang",
                "klingon",
                "--root",
                str(root),
            ]
        )
    assert exc.value.code == 2
    assert "unknown language 'klingon'" in capsys.readouterr().err


def test_lang_on_an_action_that_ignores_it_says_so(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    cli.main(
        ["query", "file", "src/a.py", "--lang", "go", "--root", str(root)]
    )
    assert "--lang doesn't apply to 'file'" in capsys.readouterr().err
