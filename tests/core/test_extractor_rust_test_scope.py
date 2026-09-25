"""Which Rust symbols the extractor flags as test code, within one file.

Rust decides by attribute: ``#[cfg(test)]`` on a module or a single
item, or ``#![cfg(test)]`` over a whole scope. A name (``mod tests``)
is kept as a second signal. The out-of-line half, a whole file made
test code by its parent's declaration, is ``repo_ops``'s.
"""

from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.core.model import FileMap


def _extract(tmp_path: Path, source: str, name: str = "lib.rs") -> FileMap:
    spec = languages.spec_for_path(name)
    assert spec is not None
    (tmp_path / name).write_text(source)
    fm = extract_file(tmp_path, name, spec)
    assert fm.error is None
    return fm


def _test_flags(fm: FileMap) -> dict[str, bool]:
    return {s.qualname: s.test for s in fm.symbols}


def test_cfg_test_module_with_any_name_is_test_code(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "pub fn prod() {}\n"
        "\n"
        "#[cfg(test)]\n"
        "mod keymap_checks {\n"
        "    fn helper() {}\n"
        "}\n",
    )
    assert _test_flags(fm) == {"prod": False, "keymap_checks.helper": True}


def test_cfg_test_on_a_single_item_is_test_code(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "pub struct Foo;\n"
        "\n"
        "impl Foo {\n"
        "    pub fn real(&self) {}\n"
        "\n"
        "    #[cfg(test)]\n"
        "    pub fn for_test(&self) {}\n"
        "}\n"
        "\n"
        "#[cfg(test)]\n"
        "impl Foo {\n"
        "    fn gated(&self) {}\n"
        "}\n"
        "\n"
        '#[cfg(all(test, feature = "x"))]\n'
        "fn also_gated() {}\n",
    )
    flags = _test_flags(fm)
    assert flags["Foo"] is False
    assert flags["Foo.real"] is False
    assert flags["Foo.for_test"] is True
    assert flags["Foo.gated"] is True
    assert flags["also_gated"] is True


def test_fn_nested_in_a_test_fn_is_test_code(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    #[test]\n"
        "    fn outer() {\n"
        "        fn nested() {}\n"
        "        nested();\n"
        "    }\n"
        "}\n",
    )
    assert all(s.test for s in fm.symbols)
    assert {s.name for s in fm.symbols} == {"outer", "nested"}


def test_test_support_feature_gate_stays_production(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        '#[cfg(any(test, feature = "test-support"))]\n'
        "pub mod fakes {\n"
        "    pub fn fake_fs() {}\n"
        "}\n",
    )
    assert _test_flags(fm) == {"fakes.fake_fs": False}


def test_mod_tests_name_rule_still_applies(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        '#[cfg(any(test, feature = "test-support"))]\n'
        "mod tests {\n"
        "    fn helper() {}\n"
        "}\n",
    )
    assert _test_flags(fm) == {"tests.helper": True}


def test_inner_cfg_test_marks_the_whole_file(tmp_path: Path) -> None:
    fm = _extract(tmp_path, "#![cfg(test)]\n\nfn helper() {}\n", "undo.rs")
    assert _test_flags(fm) == {"helper": True}


def test_out_of_line_declarations_are_recorded(tmp_path: Path) -> None:
    fm = _extract(
        tmp_path,
        "mod plain;\n"
        "#[cfg(test)]\n"
        "mod editor_tests;\n"
        '#[path = "support/impl.rs"]\n'
        "mod renamed;\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    mod inner;\n"
        "}\n",
    )
    subs = [(s.candidates[0], s.test_only) for s in fm.submodules]
    assert subs == [
        ("plain.rs", False),
        ("editor_tests.rs", True),
        ("support/impl.rs", False),
        ("tests/inner.rs", True),
    ]


def test_non_rust_files_record_no_submodules(tmp_path: Path) -> None:
    fm = _extract(tmp_path, "def f():\n    pass\n", "a.py")
    assert fm.submodules == []
