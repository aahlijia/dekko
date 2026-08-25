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

RUST_FN = {
    "lib.rs": "fn foo() {}\n",
}

PY_AND_RUST = {
    "app.py": "def f():\n    pass\n",
    "lib.rs": "fn foo() {}\n",
}

# round-18 spring-boot finding, reproduced from the real
# `Binder.handleBindError` shape: rethrowing a caught exception
# through a Java 16+ `instanceof` pattern-match binding used to be
# mislabeled `(external) bindException` -- the raw variable name
# standing in for a fabricated external type.
JAVA_INSTANCEOF_RERAISE = {
    "BindException.java": "class BindException extends Exception {\n}\n",
    "Binder.java": (
        "class Binder {\n"
        "    Object handleBindError(Exception error) {\n"
        "        try {\n"
        "            return null;\n"
        "        } catch (Exception ex) {\n"
        "            if (ex instanceof BindException bindException) {\n"
        "                throw bindException;\n"
        "            }\n"
        "            throw new BindException(ex);\n"
        "        }\n"
        "    }\n"
        "}\n"
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


def test_catches_json_includes_note_on_jsts_repo(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(JS_CATCHES)
    code = cli.main(
        ["query", "catches", "SomeError", "--json", "--root", str(root)]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["action"] == "catches"
    assert "note" in doc


def test_catches_caveat_absent_on_non_jsts_repo(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Round-22 item 9 (awesome-go.md §3.1): the JS/TS caveat must not
    # print on a repo with no JS/TS files at all -- it survived a full
    # release cycle unflagged as noise on Go/C++/Python repos.
    root = make_mapped_repo(PY_THROWS)
    code = cli.main(["query", "catches", "ValueError", "--root", str(root)])
    assert code == 0
    result = capsys.readouterr()
    assert "JS/TS catch clauses" not in (result.out + result.err)


def test_catches_json_omits_note_on_non_jsts_repo(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_THROWS)
    code = cli.main(
        ["query", "catches", "ValueError", "--json", "--root", str(root)]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["action"] == "catches"
    assert "note" not in doc


def _catch_all_repo(n: int) -> dict[str, str]:
    return {
        f"mod{i}.py": (
            "def f():\n    try:\n        pass\n    except:\n        pass\n"
        )
        for i in range(n)
    }


def test_catches_truncation_footer_omitted_count_matches_hits(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Regression test: the truncation footer's "N of TOTAL omitted"
    # denominator must equal the real hit count, not hit count + 1 --
    # the summary/header line printed above the rows must not itself
    # be counted as a row by the truncation meter.
    root = make_mapped_repo(_catch_all_repo(5))
    code = cli.main(
        [
            "query",
            "catches",
            "AnythingAtAll",
            "--limit",
            "2",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "5 catch clause(s) match" in out
    assert "3 of 5 omitted" in out


MANY_THROWS = {
    "app.py": (
        "def f():\n"
        "    if a:\n"
        "        raise ValueError('a')\n"
        "    if b:\n"
        "        raise TypeError('b')\n"
        "    if c:\n"
        "        raise KeyError('c')\n"
        "    if d:\n"
        "        raise IndexError('d')\n"
        "    if e:\n"
        "        raise AttributeError('e')\n"
    ),
}


def test_throws_truncation_footer_omitted_count_matches_hits(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Same regression as the catches case above, for `query throws`'
    # own summary-line-in-lines_out truncation-meter bug.
    root = make_mapped_repo(MANY_THROWS)
    code = cli.main(
        ["query", "throws", "f", "--limit", "2", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "5 throw site(s): 0 repo-defined, 5 external" in out
    assert "3 of 5 omitted" in out


def test_throws_unsupported_language_target_is_disclosed_not_generic_empty(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Rust is a permanent throws/catches exclusion (see
    # `languages.exception_handling_supported`) -- the empty-result
    # message must say so, not fall into the generic "(no throws
    # found ...)" text that a genuinely-empty *supported*-language
    # query also produces.
    root = make_mapped_repo(RUST_FN)
    code = cli.main(["query", "throws", "foo", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "not tracked for" in out
    assert "rust" in out
    assert "no throws found" not in out


def test_throws_json_language_supported_false_for_rust_target(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(RUST_FN)
    code = cli.main(["query", "throws", "foo", "--json", "--root", str(root)])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["language_supported"] is False
    assert doc["results"] == []


def test_throws_json_omits_language_supported_when_true(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # A genuinely-empty result on a *supported* language must not
    # carry `language_supported` at all -- locks in the conditional
    # field decision, not just the positive (Rust) case.
    root = make_mapped_repo(PY_THROWS)
    code = cli.main(
        ["query", "throws", "caller", "--json", "--root", str(root)]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert "language_supported" not in doc


def test_catches_excluded_file_count_disclosed_when_repo_has_rust_files(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_AND_RUST)
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
    result = capsys.readouterr()
    assert "1 of 2 mapped files" in result.err
    # Names the actual excluded language present in this repo (rust),
    # not a static "Rust/Go/C" list that could name languages the
    # repo doesn't even contain -- see round-18 claude-buddy finding.
    assert "(rust)" in result.err
    assert "Rust/Go/C" not in result.err


def test_catches_no_language_coverage_note_for_single_supported_language(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Pure-Python repo: excluded count is 0, so the note must be
    # silent -- regression guard against noise on the common case.
    root = make_mapped_repo(PY_THROWS)
    code = cli.main(["query", "catches", "ValueError", "--root", str(root)])
    assert code == 0
    result = capsys.readouterr()
    assert "mapped files" not in result.err


def test_catches_json_language_coverage_field_present_when_nonzero(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_AND_RUST)
    code = cli.main(
        [
            "query",
            "catches",
            "SomeCompletelyUnrelatedType",
            "--json",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["language_coverage"]["excluded_files"] == 1
    assert doc["language_coverage"]["total_files"] == 2


def test_throws_java_instanceof_pattern_reraise_not_labeled_external(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(JAVA_INSTANCEOF_RERAISE)
    code = cli.main(
        [
            "query",
            "throws",
            "handleBindError",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    result = capsys.readouterr()
    assert "bindException" not in result.out
    assert "(external)" not in result.out
    assert "1 throw site(s): 1 repo-defined, 0 external" in result.out
    assert "1 re-raise site(s) omitted" in result.err
