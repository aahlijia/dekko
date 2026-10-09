"""Rust ``use`` records: globs kept, ``super`` re-based in inline mods.

A glob is how a crate root re-exports a whole module (``pub use
geometry::*;``) and how a test module reaches its parent (``use
super::*;``), so it is recorded. A ``use`` written inside an inline
``mod m { .. }`` is recorded at file level like every other, so its
``super`` is rewritten to what it means from the file's own module.
"""

from pathlib import Path

from dekko.core.extractor import extract_file
from dekko.core.languages import RUST


def _imports(tmp_path: Path, text: str) -> list[tuple[str, str]]:
    (tmp_path / "a.rs").write_text(text)
    fm = extract_file(tmp_path, "a.rs", RUST)
    return [(imp.name, imp.source) for imp in fm.imports]


def test_a_glob_is_recorded(tmp_path: Path) -> None:
    assert _imports(tmp_path, "use a::*;\n") == [("*", "a::*")]


def test_a_glob_inside_a_group_is_recorded(tmp_path: Path) -> None:
    assert _imports(tmp_path, "use a::{b::*, c};\n") == [
        ("*", "a::b::*"),
        ("c", "a::c"),
    ]


def test_super_glob_in_a_test_module_records_nothing(tmp_path: Path) -> None:
    assert _imports(tmp_path, "mod tests {\n    use super::*;\n}\n") == []


def test_a_file_level_super_glob_is_kept(tmp_path: Path) -> None:
    assert _imports(tmp_path, "use super::*;\n") == [("*", "super::*")]


def test_super_super_glob_in_a_test_module_climbs_once(
    tmp_path: Path,
) -> None:
    text = "mod tests {\n    use super::super::*;\n}\n"
    assert _imports(tmp_path, text) == [("*", "super::*")]


def test_super_name_in_a_test_module_is_the_files_own(tmp_path: Path) -> None:
    text = "mod tests {\n    use super::X;\n}\n"
    assert _imports(tmp_path, text) == [("X", "self::X")]


def test_a_use_inside_a_fn_in_a_test_module_is_one_level_deep(
    tmp_path: Path,
) -> None:
    text = "mod tests {\n    fn f() {\n        use super::W;\n    }\n}\n"
    assert _imports(tmp_path, text) == [("W", "self::W")]


def test_a_use_inside_a_file_level_fn_is_as_written(tmp_path: Path) -> None:
    text = "fn f() {\n    use super::V;\n}\n"
    assert _imports(tmp_path, text) == [("V", "super::V")]


def test_a_crate_name_in_a_test_module_is_as_written(tmp_path: Path) -> None:
    text = "mod tests {\n    use foo::Z;\n}\n"
    assert _imports(tmp_path, text) == [("Z", "foo::Z")]


def test_two_inline_levels(tmp_path: Path) -> None:
    text = (
        "mod a {\n"
        "    mod b {\n"
        "        use super::*;\n"
        "        use super::super::Q;\n"
        "    }\n"
        "}\n"
    )
    assert _imports(tmp_path, text) == [("Q", "self::Q")]
