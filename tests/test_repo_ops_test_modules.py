"""Whole Rust files made test code by their parent's ``#[cfg(test)] mod x;``.

The attribute sits in one file and the code in another, so the flag is
set in ``repo_ops.map_repository`` over every extracted file, never in
a single extraction or the cache. The cache tests pin that: editing
only the parent must re-flag the unchanged child, both ways.
"""

from pathlib import Path

from dekko.integrations import cli
from dekko.render import mapfile


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _map(root: Path, *extra: str) -> dict[str, bool]:
    assert cli.main(["map", str(root), "--quiet", *extra]) == 0
    index = mapfile.load_map(root)
    assert index is not None
    return {sid: s.test for sid, s in index.symbols_by_id.items()}


CRATE = {
    "crates/ed/src/ed.rs": (
        "mod plain;\n#[cfg(test)]\nmod ed_tests;\n\npub fn run() {}\n"
    ),
    "crates/ed/src/plain.rs": "pub fn prod() {}\n",
    "crates/ed/src/ed_tests.rs": "mod helpers;\n\nfn case() {}\n",
    "crates/ed/src/ed_tests/helpers.rs": "pub fn build() {}\n",
}


def test_declared_test_module_and_its_descendants_are_test_code(
    tmp_path: Path,
) -> None:
    _write(tmp_path, CRATE)
    flags = _map(tmp_path)
    assert flags["crates/ed/src/ed_tests.rs::case"] is True
    assert flags["crates/ed/src/ed_tests/helpers.rs::build"] is True
    assert flags["crates/ed/src/plain.rs::prod"] is False
    assert flags["crates/ed/src/ed.rs::run"] is False


def test_crate_root_the_convention_misses_resolves_beside_it(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        {
            "crates/rs/src/server.rs": (
                "#[cfg(test)]\nmod remote_tests;\n\npub fn serve() {}\n"
            ),
            "crates/rs/src/remote_tests.rs": "fn case() {}\n",
        },
    )
    flags = _map(tmp_path)
    assert flags["crates/rs/src/remote_tests.rs::case"] is True
    assert flags["crates/rs/src/server.rs::serve"] is False


def test_editing_only_the_parent_reflags_the_cached_child(
    tmp_path: Path,
) -> None:
    _write(tmp_path, CRATE)
    child = "crates/ed/src/ed_tests.rs::case"
    assert _map(tmp_path)[child] is True

    parent = tmp_path / "crates/ed/src/ed.rs"
    parent.write_text("mod plain;\nmod ed_tests;\n\npub fn run() {}\n")
    assert _map(tmp_path)[child] is False

    parent.write_text(CRATE["crates/ed/src/ed.rs"])
    assert _map(tmp_path)[child] is True


def test_skipped_child_is_not_replaced_by_a_fallback_candidate(
    tmp_path: Path,
) -> None:
    # ``a.rs``'s test module is ``a/t.rs`` (too large to map). The
    # sibling ``t.rs`` is a different, production module of the crate
    # root and must not inherit the flag.
    _write(
        tmp_path,
        {
            "src/lib.rs": "mod a;\nmod t;\n",
            "src/a.rs": "#[cfg(test)]\nmod t;\n\npub fn a() {}\n",
            "src/a/t.rs": "fn big() {}\n" + "// padding\n" * 200,
            "src/t.rs": "pub fn prod() {}\n",
        },
    )
    flags = _map(tmp_path, "--max-file-size", "1000")
    assert "src/a/t.rs::big" not in flags
    assert flags["src/t.rs::prod"] is False


def test_test_support_module_named_test_stays_production(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        {
            "crates/cl/src/cl.rs": (
                '#[cfg(any(test, feature = "test-support"))]\npub mod test;\n'
            ),
            "crates/cl/src/test.rs": "pub struct FakeServer;\n",
        },
    )
    assert _map(tmp_path)["crates/cl/src/test.rs::FakeServer"] is False
