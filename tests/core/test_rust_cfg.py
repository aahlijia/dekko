"""``rust_cfg``: evaluating ``cfg`` predicates and locating ``mod x;``.

A predicate is test-only exactly when it is false with ``test`` false
and every other atom unknown. The rows that matter most are the ones a
pattern match gets wrong: a ``test`` nested inside ``any(...)`` does
not make an item test-only.
"""

import pytest

from dekko.core.rust_cfg import cfg_is_test_only, submodule_candidates


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("#[cfg(test)]", True),
        ("#![cfg(test)]", True),
        ('#[cfg(all(test, feature = "x"))]', True),
        ("#[cfg(all(test, not(windows)))]", True),
        ("#[cfg( test )]", True),
        ('#[cfg(any(test, feature = "test-support"))]', False),
        (
            '#[cfg(all(target_os = "macos", '
            'any(test, feature = "test-support")))]',
            False,
        ),
        (
            "#[cfg(all(\n    not(rust_analyzer),\n    any(\n        test,\n"
            '        feature = "test-support",\n'
            '        target_os = "freebsd"\n    )\n))]',
            False,
        ),
        ("#[cfg(not(test))]", False),
        ('#[cfg(target_os = "windows")]', False),
        ("#[cfg_attr(test, allow(dead_code))]", False),
        ("#[test]", False),
        ("#[derive(Debug)]", False),
        ("#[cfg(test", False),
        ("#[cfg()]", False),
        ("#[cfg(all(test, $bad))]", False),
    ],
)
def test_cfg_is_test_only(attr: str, expected: bool) -> None:
    assert cfg_is_test_only(attr) is expected


def test_candidates_from_index_file_sit_beside_it() -> None:
    assert submodule_candidates("crates/a/src/lib.rs", "x", None, ()) == [
        "crates/a/src/x.rs",
        "crates/a/src/x/mod.rs",
    ]
    assert submodule_candidates("src/tools/mod.rs", "x", None, ()) == [
        "src/tools/x.rs",
        "src/tools/x/mod.rs",
    ]


def test_candidates_from_crate_named_root_sit_beside_it() -> None:
    got = submodule_candidates(
        "crates/editor/src/editor.rs", "editor_tests", None, ()
    )
    assert got == [
        "crates/editor/src/editor_tests.rs",
        "crates/editor/src/editor_tests/mod.rs",
    ]


def test_candidates_from_leaf_file_nest_then_fall_back_to_siblings() -> None:
    got = submodule_candidates(
        "crates/remote_server/src/server.rs", "remote_editing_tests", None, ()
    )
    assert got == [
        "crates/remote_server/src/server/remote_editing_tests.rs",
        "crates/remote_server/src/server/remote_editing_tests/mod.rs",
        "crates/remote_server/src/remote_editing_tests.rs",
        "crates/remote_server/src/remote_editing_tests/mod.rs",
    ]


def test_candidates_honour_path_attribute_and_inline_nesting() -> None:
    assert submodule_candidates(
        "src/lib.rs", "x", "support/x_impl.rs", ()
    ) == ["src/support/x_impl.rs"]
    assert submodule_candidates("src/lib.rs", "b", None, ("a",)) == [
        "src/a/b.rs",
        "src/a/b/mod.rs",
    ]
