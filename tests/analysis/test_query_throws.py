"""``dekko query throws``/``catches``: exception/error-flow tracing.

CLI-level end-to-end tests through ``cli.main`` + a real ``dekko map``
run (``make_mapped_repo``), mirroring ``test_query_heritage.py``'s
pattern. Covers: one-level throws (repo-defined + external + bare
re-raise disclosure), ``--transitive``/``--depth`` with truncation
disclosure, JSON output, catches' exact-match-only v1 scope (a
superclass doesn't match — the documented limitation locked in by a
test), multi-catch matching each listed type, and the JS/TS weak-signal
caveat appearing in the command's own output (not just ``--help``).
"""

import json

import pytest

from dekko.integrations import cli

from conftest import RepoFactory

PY_THROWS = {
    "errors.py": (
        "class ConfigError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "class ConfigErrorSubclass(ConfigError):\n"
        "    pass\n"
    ),
    "app.py": (
        "from errors import ConfigError\n"
        "\n"
        "\n"
        "def load_config():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError:\n"
        "        raise ConfigError('bad')\n"
        "\n"
        "\n"
        "def load_config_multi():\n"
        "    try:\n"
        "        pass\n"
        "    except (ValueError, TypeError):\n"
        "        pass\n"
        "    except:\n"
        "        pass\n"
        "\n"
        "\n"
        "def caller():\n"
        "    load_config()\n"
    ),
}

CHAIN = {
    "chain.py": (
        "class DeepError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def level0():\n"
        "    level1()\n"
        "\n"
        "\n"
        "def level1():\n"
        "    level2()\n"
        "\n"
        "\n"
        "def level2():\n"
        "    raise DeepError('deep')\n"
    ),
}

JS_CATCHES = {
    "app.js": (
        "function handle() {\n    try {\n    } catch (e) {\n    }\n}\n"
    ),
}


def test_throws_one_level_repo_defined(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_THROWS)
    code = cli.main(["query", "throws", "load_config", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "class ConfigError" in out
    assert "1 throw site(s): 1 repo-defined, 0 external" in out


def test_throws_one_level_does_not_include_callees(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # `caller()` calls `load_config()` but has no throw of its own --
    # one-level `throws` must not walk into it.
    root = make_mapped_repo(PY_THROWS)
    code = cli.main(["query", "throws", "caller", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "no throws found" in out


def test_throws_transitive_finds_callee_throw(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_THROWS)
    code = cli.main(
        [
            "query",
            "throws",
            "caller",
            "--transitive",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "class ConfigError" in out


def test_throws_bare_reraise_disclosed(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "a.py": (
            "def f():\n"
            "    try:\n"
            "        pass\n"
            "    except ValueError:\n"
            "        raise\n"
        )
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "throws", "f", "--root", str(root)])
    assert code == 0
    result = capsys.readouterr()
    assert "re-raise site(s) omitted" in result.err


def test_throws_transitive_depth_cap_disclosed(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CHAIN)
    code = cli.main(
        [
            "query",
            "throws",
            "level0",
            "--transitive",
            "--depth",
            "1",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    result = capsys.readouterr()
    assert "reached the transitive depth cap (1)" in result.err
    # depth 1 from level0 only reaches level1, not level2's raise.
    assert "no throws found" in result.out


def test_throws_wider_depth_finds_deep_raise(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CHAIN)
    code = cli.main(
        [
            "query",
            "throws",
            "level0",
            "--transitive",
            "--depth",
            "3",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "class DeepError" in out


def test_throws_json_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_THROWS)
    code = cli.main(
        ["query", "throws", "load_config", "--json", "--root", str(root)]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["action"] == "throws"
    assert doc["repo_defined"] == 1
    assert doc["external"] == 0
    assert doc["results"][0]["id"] == "errors.py::ConfigError"


def test_catches_exact_match(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_THROWS)
    code = cli.main(["query", "catches", "ValueError", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "app.py:7" in out  # `except ValueError:` in load_config


def test_catches_superclass_does_not_match(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # v1's documented limitation: catching `ConfigError` (the
    # superclass) must NOT be reported as a match for a query for its
    # subclass `ConfigErrorSubclass` -- no supertype-aware matching.
    files = {
        "errors.py": (
            "class ConfigError(Exception):\n"
            "    pass\n"
            "\n"
            "\n"
            "class ConfigErrorSubclass(ConfigError):\n"
            "    pass\n"
        ),
        "app.py": (
            "from errors import ConfigError\n"
            "\n"
            "\n"
            "def handle():\n"
            "    try:\n"
            "        pass\n"
            "    except ConfigError:\n"
            "        pass\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(
        ["query", "catches", "ConfigErrorSubclass", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "no catch clauses would handle" in out


def test_catches_multi_catch_matches_each_type(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_THROWS)
    for needle in ("ValueError", "TypeError"):
        code = cli.main(["query", "catches", needle, "--root", str(root)])
        assert code == 0
        out = capsys.readouterr().out
        assert "app.py:14" in out  # the `except (ValueError, TypeError):`


def test_catches_bare_catch_all_always_matches(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_THROWS)
    code = cli.main(
        [
            "query",
            "catches",
            "SomeCompletelyUnrelatedType",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    # The bare `except:` in load_config_multi always matches.
    assert "catch-all" in out


def test_catches_js_weak_signal_caveat_in_output(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(JS_CATCHES)
    code = cli.main(["query", "catches", "SomeError", "--root", str(root)])
    assert code == 0
    result = capsys.readouterr()
    assert "JS/TS catch clauses are almost never type-annotated" in (
        result.out + result.err
    )


def test_catches_json_includes_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_THROWS)
    code = cli.main(
        ["query", "catches", "ValueError", "--json", "--root", str(root)]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["action"] == "catches"
    assert "note" in doc
