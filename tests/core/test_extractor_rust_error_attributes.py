"""Rust attributes that tree-sitter's error recovery leaves unparsed.

tree-sitter-rust has no rule for an attribute on a struct-pattern field,
so recovery parsed ``#[cfg_attr(not(..), allow(..))]`` as ordinary call
expressions and ``not`` resolved to an unrelated function of that name.
"""

from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import extract_file

_SOURCE = """\
fn not(x: bool) -> bool {
    !x
}

fn build(options: Options) -> bool {
    let Options {
        bounds,
        titlebar,
        #[cfg_attr(
            not(any(target_os = "linux", target_os = "freebsd")),
            allow(unused_variables)
        )]
        icon,
        #[cfg_attr(not(target_os = "macos"), allow(unused_variables))]
        tabbing,
    } = options;
    helper(bounds, titlebar, icon, tabbing);
    not(true)
}
"""


def _call_lines(tmp_path: Path, source: str) -> list[tuple[str, int]]:
    """``(name, line)`` for every raw call extracted from ``source``."""
    (tmp_path / "lib.rs").write_text(source)
    spec = languages.spec_for_path("lib.rs")
    assert spec is not None
    fm = extract_file(tmp_path, "lib.rs", spec)
    return [(c.name, c.line) for c in fm.calls]


def test_unparsed_attribute_payload_is_not_a_call(tmp_path: Path) -> None:
    names = {name for name, _ in _call_lines(tmp_path, _SOURCE)}
    assert "cfg_attr" not in names
    assert "allow" not in names
    assert "any" not in names


def test_real_calls_around_the_attribute_survive(tmp_path: Path) -> None:
    calls = _call_lines(tmp_path, _SOURCE)
    assert ("helper", 17) in calls
    # The one real ``not`` call is kept; the attribute's are dropped.
    assert [c for c in calls if c[0] == "not"] == [("not", 18)]


def test_clean_file_calls_are_untouched(tmp_path: Path) -> None:
    source = (
        "#[cfg(not(test))]\n"
        "fn run() {\n"
        "    let v = vec![1];\n"
        "    helper(v);\n"
        "}\n"
    )
    assert [n for n, _ in _call_lines(tmp_path, source)] == ["helper"]
