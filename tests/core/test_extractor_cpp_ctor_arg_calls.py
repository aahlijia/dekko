"""C/C++ "most vexing parse" constructor-argument call recovery.

Round 24's tensorflow eval (``test-repos/reports/
24-tokentest-7repo-post04328/tensorflow.md`` §2.1;
``.features/plans/round24/01-cpp-vexing-parse-ctor-calls-dropped.md``)
found that a statement shaped like ``Type name(Ctor(), deleter);`` (the
idiomatic RAII smart-pointer construction ``std::unique_ptr<T, D> p(
Ctor(), deleter);``) hits C++'s "most vexing parse" ambiguity:
tree-sitter-cpp/tree-sitter-c have no type information and always
commit to parsing this as a local function *declaration* rather than a
variable construction, so ``Ctor()`` becomes a ``parameter_declaration``
wrapping an ``abstract_function_declarator`` instead of a
``call_expression`` -- structurally invisible to ``_collect_calls``'s
``call_query``. ``_collect_cpp_ctor_arg_calls`` is a second extraction
pass (mirroring ``_collect_rust_macro_calls``'s precedent for a
different grammar-level gap) that recovers the dropped call.
"""

from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import extract_file


def test_multiline_ctor_arg_call_recovered(tmp_path: Path) -> None:
    """The exact tensorflow repro shape, wrapped across lines."""
    spec = languages.spec_for_path("dtensor_device.cc")
    assert spec is not None
    (tmp_path / "dtensor_device.cc").write_text(
        "void AsyncWait() {\n"
        "  std::unique_ptr<TF_Status, decltype(&TF_DeleteStatus)>"
        " async_wait_status(\n"
        "      TF_NewStatus(), TF_DeleteStatus);\n"
        "  async_wait_status.reset(TF_NewStatus());\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "dtensor_device.cc", spec)
    assert fm.error is None
    async_wait = next(sym for sym in fm.symbols if sym.qualname == "AsyncWait")
    recovered = [
        c
        for c in fm.calls
        if c.name == "TF_NewStatus" and c.caller_id == async_wait.id
    ]
    # One recovered from the vexing-parse declaration, one captured
    # normally by ``.reset(TF_NewStatus())`` -- both attributed to the
    # enclosing function.
    assert len(recovered) == 2
    lines = {c.line for c in recovered}
    assert lines == {3, 4}


def test_single_line_variant_recovered_identically(tmp_path: Path) -> None:
    """Line wrapping is coincidental to the bug, not the trigger."""
    spec = languages.spec_for_path("single_line.cc")
    assert spec is not None
    (tmp_path / "single_line.cc").write_text(
        "void AsyncWait() {\n"
        "  std::unique_ptr<TF_Status, decltype(&TF_DeleteStatus)>"
        " async_wait_status(TF_NewStatus(), TF_DeleteStatus);\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "single_line.cc", spec)
    assert fm.error is None
    recovered = [c for c in fm.calls if c.name == "TF_NewStatus"]
    assert len(recovered) == 1
    assert recovered[0].line == 2
    assert recovered[0].caller_id is not None


def test_two_call_shaped_ctor_args_both_recovered(
    tmp_path: Path,
) -> None:
    """Both ``parameter_declaration`` siblings recover, not just the
    first."""
    spec = languages.spec_for_path("two_args.cc")
    assert spec is not None
    (tmp_path / "two_args.cc").write_text(
        "void run() {\n  Foo bar(Baz(), Qux());\n}\n"
    )
    fm = extract_file(tmp_path, "two_args.cc", spec)
    assert fm.error is None
    names = sorted(c.name for c in fm.calls if c.caller_id is not None)
    assert names == ["Baz", "Qux"]


def test_nested_ctor_arg_recovers_only_outer_level(
    tmp_path: Path,
) -> None:
    """Documented v1 limitation: single-level recovery only.

    ``Foo bar(Baz(Qux()), other);`` recovers ``Baz`` (the direct
    constructor argument); ``Qux`` (nested inside ``Baz``'s own
    misparsed argument list) is a known, narrower residual gap --
    round 24's evidence never surfaced this shape in practice, but the
    boundary should be an explicit, tested one rather than silent.
    """
    spec = languages.spec_for_path("nested.cc")
    assert spec is not None
    (tmp_path / "nested.cc").write_text(
        "void run() {\n  Foo bar(Baz(Qux()), other);\n}\n"
    )
    fm = extract_file(tmp_path, "nested.cc", spec)
    assert fm.error is None
    names = {c.name for c in fm.calls if c.caller_id is not None}
    assert "Baz" in names
    assert "Qux" not in names


def test_genuine_local_prototype_not_misidentified(
    tmp_path: Path,
) -> None:
    """A real forward-declared local prototype has typed parameters
    (``int x``), never a bare ``abstract_function_declarator`` -- the
    shape check discriminates correctly and synthesizes nothing."""
    spec = languages.spec_for_path("proto.cc")
    assert spec is not None
    (tmp_path / "proto.cc").write_text(
        "void run() {\n  int helper(int x, int y);\n}\n"
    )
    fm = extract_file(tmp_path, "proto.cc", spec)
    assert fm.error is None
    assert fm.calls == []


def test_file_scope_declaration_not_recovered(tmp_path: Path) -> None:
    """The block-scope precondition (a direct ``compound_statement``
    parent) excludes the identical shape at file/header scope, where
    it is never at risk of the vexing-parse ambiguity in the first
    place (a real top-level prototype's "parameters" are always
    types)."""
    spec = languages.spec_for_path("file_scope.cc")
    assert spec is not None
    (tmp_path / "file_scope.cc").write_text(
        "std::unique_ptr<TF_Status, decltype(&TF_DeleteStatus)>"
        " top_level(\n"
        "    TF_NewStatus(), TF_DeleteStatus);\n"
    )
    fm = extract_file(tmp_path, "file_scope.cc", spec)
    assert fm.error is None
    assert fm.calls == []


def test_c_grammar_also_recovers_the_same_shape(tmp_path: Path) -> None:
    """The grammar-level ambiguity is identical in C; the fix covers
    both ``LanguageSpec``s (round 23/24's design decision -- C hits it
    less often in practice with no RAII idiom driving it, but the
    mechanism is the same)."""
    spec = languages.spec_for_path("ctor_arg.c")
    assert spec is not None
    (tmp_path / "ctor_arg.c").write_text(
        "void foo(void) {\n  Bar baz(qux());\n}\n"
    )
    fm = extract_file(tmp_path, "ctor_arg.c", spec)
    assert fm.error is None
    recovered = [c for c in fm.calls if c.name == "qux"]
    assert len(recovered) == 1
    assert recovered[0].caller_id is not None
