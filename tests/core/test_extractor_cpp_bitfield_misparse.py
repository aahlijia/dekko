"""A C++ call whose scope a misparse cut off keeps its written scope.

A macro before a method's return type (``static EIGEN_ALWAYS_INLINE
absl::Status Compute(..)``) can keep tree-sitter from seeing the
method, and its body is then read as a struct's field list:
``return absl::OkStatus();`` parses as a bitfield ``absl`` whose width
is the call ``OkStatus()``. The scope is put back from the source, so
the call resolves by what was written, not to the one in-repo
``OkStatus`` in another namespace.
"""

from pathlib import Path

import pytest

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.core.model import CallGraph, FileMap
from dekko.core.resolver import resolve
from dekko.repo_ops import map_repository


def _hidden_method(statement: str) -> str:
    """A struct whose one method the macro hides, ending in ``statement``."""
    return (
        "template <typename T>\n"
        "struct Functor<Device, T> {\n"
        "  static EIGEN_ALWAYS_INLINE absl::Status Compute(\n"
        "      int n, typename TTypes<T, 2>::Tensor out) {\n"
        "    out = input.maximum(cols).eval();\n"
        f"    {statement}\n"
        "  }\n"
        "};\n"
    )


def _extract(tmp_path: Path, source: str) -> FileMap:
    spec = languages.spec_for_path("user.cc")
    assert spec is not None
    (tmp_path / "user.cc").write_text(source)
    fm = extract_file(tmp_path, "user.cc", spec)
    assert fm.error is None
    return fm


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


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("return absl::OkStatus();", ("absl::OkStatus", "OkStatus", "absl")),
        ("return ns::G(n);", ("ns::G", "G", "ns")),
        # The misparse cuts only the first scope; the rest is still a
        # qualified callee, and the two join back up.
        ("return a::b::Deep(n);", ("a::b::Deep", "Deep", "a::b")),
        ("return x::y::z::W();", ("x::y::z::W", "W", "x::y::z")),
    ],
)
def test_misparsed_call_keeps_its_written_scope(
    tmp_path: Path,
    statement: str,
    expected: tuple[str, str, str | None],
) -> None:
    fm = _extract(tmp_path, _hidden_method(statement))
    line = 6
    assert [
        (c.text, c.name, c.receiver) for c in fm.calls if c.line == line
    ] == [expected]


def test_a_real_bitfield_is_not_a_call(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "constexpr int Width() { return 3; }\n"
        "struct Flags {\n  unsigned mode : Width();\n};\n",
    )
    assert [(c.text, c.receiver) for c in fm.calls] == [("Width", None)]


def test_a_correctly_parsed_qualified_call_is_unchanged(
    tmp_path: Path,
) -> None:
    fm = _extract(tmp_path, "void Use() {\n  return absl::OkStatus();\n}\n")
    assert [(c.text, c.name, c.receiver) for c in fm.calls] == [
        ("absl::OkStatus", "OkStatus", "absl")
    ]


def test_foreign_scope_stays_external(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/absl_hash.h": "namespace absl { struct Hasher {}; }\n",
            "lib/status.cc": "namespace tensorflow { int OkStatus() {} }\n",
            "u/user.cc": _hidden_method("return absl::OkStatus();"),
        },
    )
    target = "lib/status.cc::tensorflow.OkStatus"
    assert [e for e in graph.edges if e.callee == target] == []
    assert "absl::OkStatus" in {e.callee for e in graph.external}


def test_in_repo_scope_still_resolves(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "lib/g.cc": "namespace ns { int G(int n) { return n; } }\n",
            "lib/other.cc": "namespace other { int G(int n) { return n; } }\n",
            "u/user.cc": _hidden_method("return ns::G(n);"),
        },
    )
    callees = {e.callee for e in graph.edges if e.callee.endswith(".G")}
    assert callees == {"lib/g.cc::ns.G"}
