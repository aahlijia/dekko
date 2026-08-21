"""``extractor.looks_like_cpp_header``: the C/C++ ``.h`` content-sniff.

Round 18's tensorflow finding: ``.h`` was always parsed with the C
grammar, silently dropping every ``class``/``namespace``/``template``
construct in a genuine C++ header instead of erroring -- see the
h-header-cpp-c-grammar implementation plan/report under
``test-repos/reports/18-tokentest-7repo-post0404/``. These cover the
heuristic itself (option 1 of that plan); ``tests/core/
test_languages.py::test_cpp_header_dispatch_by_content_not_extension``
covers the same fix through the real end-to-end pipeline.
"""

from dekko.core.extractor import looks_like_cpp_header


def test_class_marks_a_header_as_cpp() -> None:
    source = b"class Widget {\n public:\n  void Spin();\n};\n"
    assert looks_like_cpp_header(source) is True


def test_namespace_marks_a_header_as_cpp() -> None:
    source = b"namespace demo {\nint spin();\n}\n"
    assert looks_like_cpp_header(source) is True


def test_template_marks_a_header_as_cpp() -> None:
    source = (
        b"template <typename T>\nT Max(T a, T b) { return a > b ? a : b; }\n"
    )
    assert looks_like_cpp_header(source) is True


def test_plain_c_header_is_not_cpp() -> None:
    source = (
        b"struct Point {\n  int x;\n  int y;\n};\nint sum(struct Point p);\n"
    )
    assert looks_like_cpp_header(source) is False


def test_extern_c_wrapped_c_header_is_not_cpp() -> None:
    # Option 2 (a directory/sibling-count heuristic) was rejected in
    # the implementation plan precisely because this case -- a plain C
    # header that happens to sit in a C++-heavy directory -- would get
    # misclassified by directory composition. Content-sniffing must
    # get it right regardless of what else lives alongside it.
    source = (
        b'#ifdef __cplusplus\nextern "C" {\n#endif\n'
        b"struct Point { int x; int y; };\n"
        b"int sum(struct Point p);\n"
        b"#ifdef __cplusplus\n}\n#endif\n"
    )
    assert looks_like_cpp_header(source) is False


def test_class_as_a_c_identifier_is_not_cpp() -> None:
    # `class` and `template` are ordinary identifiers in C (they are
    # reserved keywords only in C++), so a real C header could legally
    # use them as field/parameter names. Confirms the heuristic checks
    # for the *parse-tree node type*, not a substring match, and so
    # cannot be fooled by this the way a naive token scan would be.
    source = b"struct Widget {\n  int class;\n  int template;\n};\n"
    assert looks_like_cpp_header(source) is False


def test_empty_source_is_not_cpp() -> None:
    assert looks_like_cpp_header(b"") is False
