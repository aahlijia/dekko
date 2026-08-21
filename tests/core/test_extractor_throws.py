"""Extraction tests for raise/throw sites and except/catch clauses.

Python/Java/C++/JS/TS (the design doc's scoped-pilot languages): a
repo-defined type raised/caught, a stdlib type raised/caught (still
extracted the same way — resolution, not extraction, is what buckets
it "external"), a bare re-raise, a catch-all, and a multi-catch.
Java additionally covers the ``throws``-clause declared-contract
signal as a source distinct from throw-site scanning. Rust/Go/C are
covered separately (test_no_throw_query_for_rust_go_c) confirming the
permanent, not-yet-implemented-looking, exclusion.
"""

from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.core.model import RawCatch, RawThrow


def _throws_by_caller(items: list[RawThrow]) -> dict[str, list[RawThrow]]:
    out: dict[str, list[RawThrow]] = {}
    for t in items:
        out.setdefault(t.caller_id or "", []).append(t)
    return out


def _catches_by_caller(items: list[RawCatch]) -> dict[str, list[RawCatch]]:
    out: dict[str, list[RawCatch]] = {}
    for c in items:
        out.setdefault(c.caller_id or "", []).append(c)
    return out


# ---------------------------------------------------------------------
# Python


def test_python_raise_repo_and_external(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "class ConfigError(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def load():\n"
        "    raise ConfigError('bad')\n"
        "\n"
        "\n"
        "def parse():\n"
        "    raise ValueError('bad')\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    assert fm.error is None
    by_caller = _throws_by_caller(fm.throws)

    load = by_caller["a.py::load"]
    assert len(load) == 1
    assert load[0].name == "ConfigError"
    assert load[0].text == "ConfigError('bad')"

    parse = by_caller["a.py::parse"]
    assert len(parse) == 1
    assert parse[0].name == "ValueError"


def test_python_bare_reraise(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError:\n"
        "        raise\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    by_caller = _throws_by_caller(fm.throws)
    reraise = by_caller["a.py::f"]
    assert len(reraise) == 1
    assert reraise[0].text is None
    assert reraise[0].name is None


def test_python_catch_all_and_multi_catch(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as e:\n"
        "        pass\n"
        "    except (TypeError, KeyError):\n"
        "        pass\n"
        "    except:\n"
        "        pass\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    by_caller = _catches_by_caller(fm.catches)
    clauses = by_caller["a.py::f"]
    assert len(clauses) == 3

    single, multi, bare = clauses
    assert single.types == ["ValueError"]
    assert single.bare is False
    assert multi.types == ["TypeError", "KeyError"]
    assert multi.bare is False
    assert bare.types == []
    assert bare.bare is True


def test_python_bound_reraise_folds_into_bare(tmp_path: Path) -> None:
    # `raise e` re-raising the `except ... as e:` bound variable is the
    # same "re-raise this exact error" idiom as a bare `raise` — it
    # must fold into the same bucket, not be extracted as a fake
    # external type named "e" (T1 fix).
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except OSError as e:\n"
        "        raise e\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    reraise = _throws_by_caller(fm.throws)["a.py::f"]
    assert len(reraise) == 1
    assert reraise[0].text is None
    assert reraise[0].name is None


def test_python_raise_different_type_in_except_block_unaffected(
    tmp_path: Path,
) -> None:
    # Raising a *different*, real, constructed type inside a handler
    # that also binds `e` must not be mistaken for a re-raise of `e`.
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except OSError as e:\n"
        "        raise ValueError('x')\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    thrown = _throws_by_caller(fm.throws)["a.py::f"]
    assert len(thrown) == 1
    assert thrown[0].name == "ValueError"


def test_python_raise_other_bare_name_in_except_block_unaffected(
    tmp_path: Path,
) -> None:
    # A bare reference to a *different* name than the bound variable
    # is still a real (external) type name, not folded into bare
    # re-raise — confirms name equality is checked, not just "any bare
    # identifier inside an except block".
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except OSError as e:\n"
        "        raise SomeOtherError\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    thrown = _throws_by_caller(fm.throws)["a.py::f"]
    assert len(thrown) == 1
    assert thrown[0].name == "SomeOtherError"


def test_python_bare_class_reference_raise_with_no_except_unaffected(
    tmp_path: Path,
) -> None:
    # A bare class-reference raise at module/function scope with no
    # enclosing except clause at all — the upward walk must return
    # None cleanly rather than crash or mis-tag.
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text("def f():\n    raise SomeError\n")
    fm = extract_file(tmp_path, "a.py", spec)
    thrown = _throws_by_caller(fm.throws)["a.py::f"]
    assert len(thrown) == 1
    assert thrown[0].name == "SomeError"


def test_python_nested_closure_reraise_folds_into_bare(
    tmp_path: Path,
) -> None:
    # `raise e` inside a nested `def` defined inside an except block,
    # referencing the outer `e` via closure. Python's `LanguageSpec`
    # sets no `function_boundary_types` (matching Python's existing
    # lack of that concept elsewhere in the codebase), so the upward
    # walk does not stop at the nested `def` and still finds the
    # outer `except ... as e:` binding — the closure genuinely
    # re-raises the same bound exception object, so folding this into
    # bare re-raise is correct, not a mis-tag. Documented explicitly
    # (deviation from the original design doc, which expected this
    # case to fall through to an "external type named e" — see the
    # design doc's T1 section for the reconciliation note) so a future
    # change doesn't silently alter this behavior.
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except OSError as e:\n"
        "        def inner():\n"
        "            raise e\n"
        "        inner()\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    thrown = _throws_by_caller(fm.throws)["a.py::inner"]
    assert len(thrown) == 1
    assert thrown[0].text is None
    assert thrown[0].name is None


def test_python_raise_from_ignores_cause(tmp_path: Path) -> None:
    # `raise X from Y` — the `from` clause's cause must never be
    # mistaken for the raised expression itself (RawThrow.name should
    # be the raised type, not the cause).
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "def f(err):\n    raise ValueError('bad') from err\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    assert len(fm.throws) == 1
    assert fm.throws[0].name == "ValueError"


# ---------------------------------------------------------------------
# Java


def test_java_throw_and_throws_clause(tmp_path: Path) -> None:
    spec = languages.spec_for_path("Sample.java")
    assert spec is not None
    (tmp_path / "Sample.java").write_text(
        "class Sample {\n"
        "    void load() throws IOException, SQLException {\n"
        '        throw new ConfigError("bad");\n'
        "    }\n"
        "    void propagateOnly() throws IOException {\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "Sample.java", spec)
    assert fm.error is None
    by_caller = _throws_by_caller(fm.throws)

    load = by_caller["Sample.java::Sample.load"]
    names = {t.name for t in load}
    assert names == {"ConfigError", "IOException", "SQLException"}

    # A method with a declared `throws` clause but no throw statement
    # of its own still surfaces the checked-exception signal — this is
    # the design doc's own called-out "common real pattern".
    propagate = by_caller["Sample.java::Sample.propagateOnly"]
    assert len(propagate) == 1
    assert propagate[0].name == "IOException"


def test_java_multi_catch(tmp_path: Path) -> None:
    spec = languages.spec_for_path("Sample.java")
    assert spec is not None
    (tmp_path / "Sample.java").write_text(
        "class Sample {\n"
        "    void handle() {\n"
        "        try {\n"
        "        } catch (ConfigError e) {\n"
        "        } catch (IOException | SQLException e) {\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "Sample.java", spec)
    by_caller = _catches_by_caller(fm.catches)
    clauses = by_caller["Sample.java::Sample.handle"]
    assert len(clauses) == 2
    single, multi = clauses
    assert single.types == ["ConfigError"]
    assert single.bare is False
    assert multi.types == ["IOException", "SQLException"]
    # Java has no catch-all syntax at all.
    assert multi.bare is False


def test_java_catch_param_reraise_folds_into_bare(tmp_path: Path) -> None:
    # `catch (Exception ex) { throw ex; }` — the direct Java analog of
    # C++/JS's bound-catch-param reraise. Before this fix, Java had no
    # branch in `_nearest_catch_binding` at all, so this was mislabeled
    # as an "external" throw of a fake type literally named `ex`.
    spec = languages.spec_for_path("Sample.java")
    assert spec is not None
    (tmp_path / "Sample.java").write_text(
        "class Sample {\n"
        "    void handle() {\n"
        "        try {\n"
        "        } catch (Exception ex) {\n"
        "            throw ex;\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "Sample.java", spec)
    assert fm.error is None
    reraise = _throws_by_caller(fm.throws)["Sample.java::Sample.handle"]
    assert len(reraise) == 1
    assert reraise[0].text is None
    assert reraise[0].name is None


def test_java_instanceof_pattern_bound_reraise_folds_into_bare(
    tmp_path: Path,
) -> None:
    # round-18 spring-boot finding, reproduced from
    # `Binder.handleBindError`: rethrowing a caught exception through a
    # Java 16+ `instanceof` pattern-match binding (`if (ex instanceof
    # BindException bindException) { throw bindException; }`) was
    # mislabeled `(external) bindException` — the raw variable
    # identifier standing in for a fabricated external type name, even
    # though it's neither external nor truly unresolvable: the bound
    # type is sitting right there in the pattern match one line up.
    spec = languages.spec_for_path("Sample.java")
    assert spec is not None
    (tmp_path / "Sample.java").write_text(
        "class Sample {\n"
        "    void handle() {\n"
        "        try {\n"
        "        } catch (Exception ex) {\n"
        "            if (ex instanceof BindException bindException) {\n"
        "                throw bindException;\n"
        "            }\n"
        "            throw new BindException(ex);\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "Sample.java", spec)
    assert fm.error is None
    sites = _throws_by_caller(fm.throws)["Sample.java::Sample.handle"]
    assert len(sites) == 2
    bare = [t for t in sites if t.text is None]
    named = [t for t in sites if t.text is not None]
    assert len(bare) == 1
    assert len(named) == 1
    assert named[0].name == "BindException"


def test_java_instanceof_pattern_in_else_branch_not_mistaken_for_reraise(
    tmp_path: Path,
) -> None:
    # The pattern-bound variable's scope is the `if`'s own consequence
    # block only -- a bare-identifier throw of the same name from the
    # `else` branch (real Java would reject this at compile time as
    # out of scope, but dekko doesn't type-check) must not be folded
    # into bare re-raise on the strength of a name match alone; it
    # should still resolve as a named (if unresolvable) throw target.
    spec = languages.spec_for_path("Sample.java")
    assert spec is not None
    (tmp_path / "Sample.java").write_text(
        "class Sample {\n"
        "    void handle() {\n"
        "        try {\n"
        "        } catch (Exception ex) {\n"
        "            if (ex instanceof BindException bindException) {\n"
        "            } else {\n"
        "                throw bindException;\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "Sample.java", spec)
    assert fm.error is None
    sites = _throws_by_caller(fm.throws)["Sample.java::Sample.handle"]
    assert len(sites) == 1
    assert sites[0].text is not None
    assert sites[0].name == "bindException"


# ---------------------------------------------------------------------
# C++


def test_cpp_throw_repo_and_bare_rethrow(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.cpp")
    assert spec is not None
    (tmp_path / "a.cpp").write_text(
        "void load() {\n"
        '    throw ConfigError("bad");\n'
        "}\n"
        "void reraise() {\n"
        "    throw;\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "a.cpp", spec)
    assert fm.error is None
    by_caller = _throws_by_caller(fm.throws)
    assert by_caller["a.cpp::load"][0].name == "ConfigError"
    bare = by_caller["a.cpp::reraise"][0]
    assert bare.text is None
    assert bare.name is None


def test_cpp_reference_bound_catch_reraise_folds_into_bare(
    tmp_path: Path,
) -> None:
    # `catch (std::exception& e) { throw e; }` — the common C++
    # reference-bound catch-param shape. Exercises
    # `_innermost_identifier`'s `reference_declarator` unwrap (T1 fix).
    spec = languages.spec_for_path("a.cpp")
    assert spec is not None
    (tmp_path / "a.cpp").write_text(
        "void handle() {\n"
        "    try {\n"
        "    } catch (std::exception& e) {\n"
        "        throw e;\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "a.cpp", spec)
    reraise = _throws_by_caller(fm.throws)["a.cpp::handle"]
    assert len(reraise) == 1
    assert reraise[0].text is None
    assert reraise[0].name is None


def test_cpp_by_value_catch_reraise_folds_into_bare(
    tmp_path: Path,
) -> None:
    # `catch (std::exception e) { throw e; }` — by-value, no `&`, so
    # the declarator is a plain `identifier` with no unwrap needed.
    spec = languages.spec_for_path("a.cpp")
    assert spec is not None
    (tmp_path / "a.cpp").write_text(
        "void handle() {\n"
        "    try {\n"
        "    } catch (std::exception e) {\n"
        "        throw e;\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "a.cpp", spec)
    reraise = _throws_by_caller(fm.throws)["a.cpp::handle"]
    assert len(reraise) == 1
    assert reraise[0].text is None
    assert reraise[0].name is None


def test_cpp_catch_typed_and_catch_all(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.cpp")
    assert spec is not None
    (tmp_path / "a.cpp").write_text(
        "void handle() {\n"
        "    try {\n"
        "    } catch (ConfigError& e) {\n"
        "    } catch (...) {\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "a.cpp", spec)
    by_caller = _catches_by_caller(fm.catches)
    clauses = by_caller["a.cpp::handle"]
    assert len(clauses) == 2
    typed, catch_all = clauses
    assert typed.types == ["ConfigError"]
    assert typed.bare is False
    assert catch_all.types == []
    assert catch_all.bare is True


# ---------------------------------------------------------------------
# JS/TS


def test_js_throw_and_always_bare_catch(tmp_path: Path) -> None:
    # Plain JS never type-discriminates a caught value — both a bound
    # and unbound catch clause extract as catch-all (see
    # LanguageSpec.catch_query's docstring).
    spec = languages.spec_for_path("a.js")
    assert spec is not None
    (tmp_path / "a.js").write_text(
        "function load() {\n"
        "    throw new Error('bad');\n"
        "}\n"
        "function handle() {\n"
        "    try {\n"
        "    } catch (e) {\n"
        "    }\n"
        "    try {\n"
        "    } catch {\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "a.js", spec)
    assert fm.error is None
    by_caller = _throws_by_caller(fm.throws)
    assert by_caller["a.js::load"][0].name == "Error"

    catches = _catches_by_caller(fm.catches)["a.js::handle"]
    assert len(catches) == 2
    assert all(c.bare and c.types == [] for c in catches)


def test_ts_typed_catch_is_rare_exception(tmp_path: Path) -> None:
    # TS's optional `catch (e: Type)` annotation is the one case that
    # is NOT a catch-all — everything else (untyped) still is.
    spec = languages.spec_for_path("a.ts")
    assert spec is not None
    (tmp_path / "a.ts").write_text(
        "function load(): void {\n"
        "    throw new ConfigError('bad');\n"
        "}\n"
        "function handle(): void {\n"
        "    try {\n"
        "    } catch (e: ConfigError) {\n"
        "    }\n"
        "    try {\n"
        "    } catch (e) {\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "a.ts", spec)
    assert fm.error is None
    assert fm.throws[0].name == "ConfigError"

    catches = _catches_by_caller(fm.catches)["a.ts::handle"]
    assert len(catches) == 2
    typed, untyped = catches
    assert typed.types == ["ConfigError"]
    assert typed.bare is False
    assert untyped.bare is True
    assert untyped.types == []


def test_js_bound_catch_reraise_folds_into_bare(tmp_path: Path) -> None:
    # `catch (e) { throw e; }` — the JS/TS re-throw idiom (T1 fix).
    spec = languages.spec_for_path("a.js")
    assert spec is not None
    (tmp_path / "a.js").write_text(
        "function handle() {\n"
        "    try {\n"
        "    } catch (e) {\n"
        "        throw e;\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "a.js", spec)
    reraise = _throws_by_caller(fm.throws)["a.js::handle"]
    assert len(reraise) == 1
    assert reraise[0].text is None
    assert reraise[0].name is None


def test_js_bound_catch_new_error_throw_unaffected(tmp_path: Path) -> None:
    # `catch (e) { throw new Error("x"); }` — a real, constructed
    # error inside a handler that also binds `e` must not be folded
    # into bare re-raise.
    spec = languages.spec_for_path("a.js")
    assert spec is not None
    (tmp_path / "a.js").write_text(
        "function handle() {\n"
        "    try {\n"
        "    } catch (e) {\n"
        "        throw new Error('x');\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "a.js", spec)
    thrown = _throws_by_caller(fm.throws)["a.js::handle"]
    assert len(thrown) == 1
    assert thrown[0].name == "Error"


def test_ts_catch_any_and_unknown_are_catch_all(tmp_path: Path) -> None:
    # TS's compiler permits only `any`/`unknown` on a catch variable's
    # type annotation — both are semantically catch-all, not genuine
    # typed matches (T2 fix).
    spec = languages.spec_for_path("a.ts")
    assert spec is not None
    (tmp_path / "a.ts").write_text(
        "function handle(): void {\n"
        "    try {\n"
        "    } catch (e: any) {\n"
        "    }\n"
        "    try {\n"
        "    } catch (e: unknown) {\n"
        "    }\n"
        "    try {\n"
        "    } catch (e) {\n"
        "    }\n"
        "    try {\n"
        "    } catch {\n"
        "    }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "a.ts", spec)
    assert fm.error is None
    clauses = _catches_by_caller(fm.catches)["a.ts::handle"]
    assert len(clauses) == 4
    for clause in clauses:
        assert clause.types == []
        assert clause.bare is True


def test_js_throw_string_literal_has_no_name(tmp_path: Path) -> None:
    # JS/TS's own documented caveat: `throw "a string"` is valid but
    # not a name-able type.
    spec = languages.spec_for_path("a.js")
    assert spec is not None
    (tmp_path / "a.js").write_text(
        "function f() {\n    throw 'a string';\n}\n"
    )
    fm = extract_file(tmp_path, "a.js", spec)
    assert len(fm.throws) == 1
    assert fm.throws[0].name is None
    assert fm.throws[0].text == "'a string'"


# ---------------------------------------------------------------------
# Rust/Go/C: permanently out of scope


def test_no_throw_query_for_rust_go_c(tmp_path: Path) -> None:
    for lang_name, ext, source in (
        ("rust", "a.rs", "fn f() {}\n"),
        ("go", "a.go", "package a\nfunc f() {}\n"),
        ("c", "a.c", "void f(void) {}\n"),
    ):
        spec = languages.spec_for_path(ext)
        assert spec is not None
        assert spec.name == lang_name
        assert spec.throw_query is None
        assert spec.catch_query is None
        (tmp_path / ext).write_text(source)
        fm = extract_file(tmp_path, ext, spec)
        assert fm.throws == []
        assert fm.catches == []
