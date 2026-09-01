"""``dekko query throws``/``catches``: exception/error-flow tracing.

CLI-level end-to-end tests through ``cli.main`` + a real ``dekko map``
run (``make_mapped_repo``), mirroring ``test_query_heritage.py``'s
pattern. Covers: one-level throws (repo-defined + external + bare
re-raise disclosure), ``--transitive``/``--depth`` with truncation
disclosure, JSON output, catches' exact-match-only v1 scope (a
superclass doesn't match — the documented limitation locked in by a
test), multi-catch matching each listed type, and the JS/TS weak-signal
caveat appearing in the command's own output (not just ``--help``).

Also covers plan 28's ``--lang`` filter and default exact-before-
catch-all sort: the CLI-level tests exercise real multi-language
(Java/JS) map data through the full ``cli.main`` -> ``query.run`` ->
``_dispatch`` -> ``_run_catches``/``_run_throws`` path; the
``--transitive`` cross-language-BFS case is exercised directly against
a hand-built ``MapIndex`` instead, since real cross-language call-graph
resolution isn't something the extractor produces (there's no way to
get a genuine repo to reproduce it) — this is still exactly what the
plan's "small fixture index" test-plan bullets describe.
"""

import json

import pytest

from dekko.analysis import query
from dekko.core.model import ExternalCall, Symbol
from dekko.core.resolver import MODULE_CALLER_SUFFIX
from dekko.integrations import cli
from dekko.render.mapfile import MapIndex

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


# ---------------------------------------------------------------------
# Plan 28: ``--lang`` filter + default exact-before-catch-all sort.

JAVA_AND_JS_CATCHES = {
    "App.java": (
        "class App {\n"
        "    void handle() {\n"
        "        try {\n"
        "        } catch (ConfigError e) {\n"
        "        }\n"
        "    }\n"
        "}\n"
    ),
    "widget.js": (
        "function handle() {\n    try {\n    } catch (e) {\n    }\n}\n"
    ),
}

SORT_FIXTURE = {
    # Paths deliberately alphabetize the catch-all ahead of the exact
    # match, so a passing test proves Fix B's sort key (not path
    # lexical order) decides row order.
    "aaa_widget.js": (
        "function handle() {\n    try {\n    } catch (e) {\n    }\n}\n"
    ),
    "zzz_app.py": (
        "class ConfigError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def handle():\n"
        "    try:\n"
        "        pass\n"
        "    except ConfigError:\n"
        "        pass\n"
    ),
}


def test_catches_lang_filter_excludes_other_language(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(JAVA_AND_JS_CATCHES)
    code = cli.main(
        [
            "query",
            "catches",
            "ConfigError",
            "--lang",
            "java",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "App.java" in out
    assert "widget.js" not in out


def test_catches_lang_filter_json_fields(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(JAVA_AND_JS_CATCHES)
    code = cli.main(
        [
            "query",
            "catches",
            "ConfigError",
            "--lang",
            "java",
            "--json",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["lang_filter"] == "java"
    assert doc["lang_filtered_out"] == 1
    assert doc["exact_matches"] == 1
    assert doc["catch_all_matches"] == 0


def test_catches_lang_filter_note_disclosed(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(JAVA_AND_JS_CATCHES)
    code = cli.main(
        [
            "query",
            "catches",
            "ConfigError",
            "--lang",
            "java",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "--lang java filter applied" in err
    assert "1 catch clause(s)" in err
    assert "1 javascript" in err


def test_catches_default_sort_exact_before_catch_all(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SORT_FIXTURE)
    code = cli.main(["query", "catches", "ConfigError", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    exact_idx = out.index("zzz_app.py")
    catch_all_idx = out.index("aaa_widget.js")
    assert exact_idx < catch_all_idx


def test_query_lang_rejects_unsupported_language(
    make_mapped_repo: RepoFactory,
) -> None:
    # 'rust' is a real language name but permanently excluded from
    # throws/catches (exception_handling_supported() == False) -- it
    # must never be a valid --lang choice, since accepting it would
    # silently produce an always-empty result instead of a clear error.
    root = make_mapped_repo(PY_THROWS)
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "query",
                "catches",
                "ValueError",
                "--lang",
                "rust",
                "--root",
                str(root),
            ]
        )
    assert exc.value.code == 2


def test_caller_language_resolved_symbol_id() -> None:
    index = MapIndex(root_label="test")
    sym = Symbol(
        id="a.py::f",
        name="f",
        qualname="f",
        kind="function",
        path="a.py",
        language="python",
    )
    index.symbols_by_id[sym.id] = sym
    assert query._caller_language(index, sym.id) == "python"


def test_caller_language_module_pseudo_id() -> None:
    index = MapIndex(root_label="test")
    index.languages_by_path["b.js"] = "javascript"
    module_id = f"b.js{MODULE_CALLER_SUFFIX}"
    assert query._caller_language(index, module_id) == "javascript"


def _make_transitive_throws_index() -> tuple[MapIndex, Symbol]:
    """A hand-built ``MapIndex`` for the ``--lang`` transitive-filter
    tests: ``entry`` (Python) calls ``helper`` (an unresolved,
    JavaScript-recorded caller id with no owning ``Symbol``, the same
    "module-level call site" shape ``throws``/``catches`` already
    model elsewhere). ``entry`` directly raises a Python-defined type;
    the BFS through ``helper`` additionally reaches a JS-defined raised
    type and a JS external call -- real cross-language call-graph
    resolution isn't something the extractor produces from source, so
    this is built by hand rather than through ``make_mapped_repo``.
    """
    index = MapIndex(root_label="test")
    entry = Symbol(
        id="app.py::entry",
        name="entry",
        qualname="entry",
        kind="function",
        path="app.py",
        language="python",
    )
    py_error = Symbol(
        id="errors.py::PyError",
        name="PyError",
        qualname="PyError",
        kind="class",
        path="errors.py",
        language="python",
    )
    js_error = Symbol(
        id="errors.js::JsError",
        name="JsError",
        qualname="JsError",
        kind="class",
        path="errors.js",
        language="javascript",
    )
    helper_id = "helper.js::helper"
    index.symbols_by_id = {
        entry.id: entry,
        py_error.id: py_error,
        js_error.id: js_error,
    }
    index.languages_by_path = {
        "app.py": "python",
        "errors.py": "python",
        "errors.js": "javascript",
        "helper.js": "javascript",
    }
    index.calls_out = {entry.id: [helper_id]}
    index.throws_out = {
        entry.id: [py_error.id],
        helper_id: [js_error.id],
    }
    index.throws_lines = {
        (entry.id, py_error.id): [5],
        (helper_id, js_error.id): [9],
    }
    index.throws_external_out = {
        helper_id: [
            ExternalCall(caller=helper_id, callee="fetch", lines=[12])
        ],
    }
    return index, entry


def test_throws_gather_transitive_lang_filter_excludes_other_language() -> (
    None
):
    index, entry = _make_transitive_throws_index()
    resolved, external, _bare, _ambig, _trunc, filtered_out = (
        query._throws_gather(index, entry, True, 5, "python")
    )
    assert [s.id for s, _d, _lines in resolved] == ["errors.py::PyError"]
    assert external == []
    assert filtered_out == 2


def test_throws_gather_transitive_no_lang_filter_returns_all() -> None:
    index, entry = _make_transitive_throws_index()
    resolved, external, _bare, _ambig, _trunc, filtered_out = (
        query._throws_gather(index, entry, True, 5, None)
    )
    assert filtered_out == 0
    assert len(resolved) == 2
    assert len(external) == 1


def test_run_throws_transitive_lang_filter_disclosed(
    capsys: pytest.CaptureFixture,
) -> None:
    index, entry = _make_transitive_throws_index()
    code, _meter = query._run_throws(
        index, entry, True, 5, False, 50, None, "python"
    )
    assert code == 0
    result = capsys.readouterr()
    assert "PyError" in result.out
    assert "JsError" not in result.out
    assert "--lang python filter applied" in result.err
    assert "2 throw site(s)" in result.err


def test_run_throws_transitive_lang_filter_json_fields(
    capsys: pytest.CaptureFixture,
) -> None:
    index, entry = _make_transitive_throws_index()
    code, _meter = query._run_throws(
        index, entry, True, 5, True, 50, None, "python"
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["lang_filter"] == "python"
    assert doc["lang_filtered_out"] == 2
    assert doc["repo_defined"] == 1
    assert doc["external"] == 0


def test_run_throws_one_level_lang_mismatch_note(
    capsys: pytest.CaptureFixture,
) -> None:
    index, entry = _make_transitive_throws_index()
    code, _meter = query._run_throws(
        index, entry, False, 5, False, 50, None, "java"
    )
    assert code == 0
    result = capsys.readouterr()
    assert (
        "target's own language (python) doesn't match --lang java"
        in result.err
    )
    assert "no throws found" in result.out
