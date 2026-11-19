"""What the community listing derives on read: labels, cohesion,
bridges, and where communities and directories disagree."""

from pathlib import Path

from conftest import RepoFactory

from dekko.analysis.communities import (
    bridges,
    cohesion,
    describe,
    label_groups,
    spans,
    splits,
)
from dekko.render import mapfile


def test_a_label_drops_the_shared_prefix_and_follows_the_majority() -> None:
    groups = [
        ["src/app/core/a.py", "src/app/core/b.py", "src/app/core/c.py"],
        ["src/app/api/z.py", "src/app/web/x.py", "src/app/web/y.py"],
    ]
    paths = [p for g in groups for p in g]

    assert label_groups(groups, groups, paths) == ["core", "web"]


def test_two_directories_share_a_label_when_neither_has_half() -> None:
    group = ["p/a.py", "p/d.py", "q/b.py", "q/e.py", "r/c.py"]

    assert label_groups([group], [group], [*group, "z/w.py"]) == ["p + q"]


def test_a_scattered_community_is_named_by_its_hub() -> None:
    group = ["a/1.py", "b/2.py", "c/3.py", "d/4.py", "e/5.py"]

    assert label_groups([group], [["c/3.py"]], group) == ["3.py"]


def test_a_community_that_scatters_below_a_directory_adds_its_hub() -> None:
    group = [f"src/{d}/{i}.py" for i, d in enumerate("abcde")]

    labels = label_groups([group], [["src/c/2.py"]], [*group, "lib/x.py"])

    assert labels == ["src (2.py)"]


def test_long_directory_names_lose_their_middle() -> None:
    deep = "core/spring-boot/src/main/java/org/x"
    group = [f"{deep}/A.java", f"{deep}/B.java", f"{deep}/C.java"]

    labels = label_groups([group], [group], [*group, "other/D.java"])

    assert labels == ["core/spring-boot/…/x"]


def test_communities_with_the_same_label_name_their_hub() -> None:
    groups = [
        ["web/a.py", "web/b.py", "web/c.py"],
        ["web/m.py", "web/n.py", "web/o.py"],
    ]
    paths = [*groups[0], *groups[1], "api/q.py"]

    labels = label_groups(groups, groups, paths)

    assert labels == ["web (a.py)", "web (m.py)"]


def test_a_community_split_evenly_names_both_halves() -> None:
    group = ["app/a.py", "app/b.py", "lib/c.py", "lib/d.py"]

    assert label_groups([group], [group], [*group, "x/y.py"]) == ["app + lib"]


def test_the_label_only_descends_into_the_directory_it_took() -> None:
    # src/ holds 3 of 5 files, one per subfolder; tools/x holds the
    # other 2. One level down tools/x outranks every src/ child, but it
    # sits outside src/, so it must not name the community.
    group = ["src/a/1.py", "src/b/2.py", "src/c/3.py", "tools/x/4.py"]
    group.append("tools/x/5.py")

    labels = label_groups([group], [group], [*group, "z/w.py"])

    assert labels == ["src (1.py)"]


def test_hub_named_communities_with_the_same_hub_name_use_its_path() -> None:
    groups = [
        [f"{d}/index.ts" for d in ("a", "b", "c", "d", "e")],
        [f"{d}/index.ts" for d in ("f", "g", "h", "i", "j")],
    ]

    labels = label_groups(
        groups, [["c/index.ts"], ["h/index.ts"]], groups[0] + groups[1]
    )

    assert labels == ["c/index.ts", "h/index.ts"]


def test_cohesion_is_the_share_of_weight_that_stays_inside() -> None:
    edges = {("a", "b"): 3, ("a", "c"): 1}

    assert cohesion(["a", "b"], edges) == 0.75
    assert cohesion(["x", "y"], {("x", "y"): 2}) == 1.0


def test_bridges_rank_community_pairs_by_cross_weight() -> None:
    groups = [["a", "b"], ["c", "d"], ["e"]]
    edges = {("a", "c"): 5, ("b", "d"): 1, ("a", "e"): 2}

    found = bridges(groups, edges)

    assert [(b.first, b.second, b.weight) for b in found] == [
        (1, 2, 6),
        (1, 3, 2),
    ]
    assert found[0].first_files == ["a", "b"]
    assert found[0].second_files == ["c", "d"]


def test_spans_lists_three_or_more_top_directories() -> None:
    assert spans(["a/1.py", "b/1.py", "c/1.py", "a/2.py"], 0) == [
        "a",
        "b",
        "c",
    ]
    assert spans(["a/1.py", "b/1.py"], 0) == []


def test_splits_lists_directories_cut_into_three_communities() -> None:
    groups = [["x/1", "y/1"], ["x/2", "y/2"], ["x/3", "z/3"]]

    assert splits(groups) == [("x", 3)]


SOURCES = {
    "app/__init__.py": "",
    "app/a.py": (
        "from app.b import b\nfrom lib.x import x\n\n\n"
        "def a():\n    b()\n    x()\n"
    ),
    "app/b.py": "from app.c import c\n\n\ndef b():\n    c()\n    c()\n",
    "app/c.py": "def c():\n    return 1\n",
    "lib/__init__.py": "",
    "lib/x.py": "from lib.y import y\n\n\ndef x():\n    y()\n    y()\n",
    "lib/y.py": "from lib.z import z\n\n\ndef y():\n    z()\n    z()\n",
    "lib/z.py": "def z():\n    return 2\n",
}


def test_describe_lists_communities_with_their_key_symbols(
    make_mapped_repo: RepoFactory,
) -> None:
    root: Path = make_mapped_repo(SOURCES)
    index = mapfile.load_map(root)
    assert index is not None

    report = describe(index, tests=False)

    # Same size, so the heavier inside comes first.
    assert [c.label for c in report.communities] == ["lib", "app"]
    lib = report.communities[0]
    assert lib.files == ["lib/x.py", "lib/y.py", "lib/z.py"]
    assert "lib/z.py::z" in lib.key_symbols
    assert 0 < lib.cohesion < 1
    assert report.unconnected == 2
    assert [(b.first, b.second) for b in report.bridges] == [(1, 2)]


def _helpers_and_api() -> dict[str, str]:
    """``core.py`` has a helper four of its own functions call, and
    three functions that ``a.py`` and ``b.py`` both use."""
    runs = "".join(f"\n\ndef run{i}():\n    _helper()\n" for i in range(4))
    apis = "".join(f"\n\ndef api{i}():\n    return {i}\n" for i in range(3))
    calls = "".join(f"    api{i}()\n" for i in range(3))
    return {
        "pkg/__init__.py": "",
        "pkg/core.py": f"def _helper():\n    return 0\n{runs}{apis}",
        "pkg/b.py": (
            f"from pkg.core import api0, api1, api2\n\n\ndef g():\n{calls}"
        ),
        "pkg/a.py": (
            "from pkg.b import g\nfrom pkg.core import api0, api1, api2\n\n\n"
            f"def f():\n    g()\n{calls}"
        ),
    }


def test_key_symbols_are_the_ones_other_files_use(
    make_mapped_repo: RepoFactory,
) -> None:
    index = mapfile.load_map(make_mapped_repo(_helpers_and_api()))
    assert index is not None

    (community,) = describe(index, tests=False).communities

    assert "pkg/core.py::_helper" not in community.key_symbols
    assert community.key_symbols[0] == "pkg/core.py::api0"


def test_key_symbols_take_at_most_two_from_one_file(
    make_mapped_repo: RepoFactory,
) -> None:
    index = mapfile.load_map(make_mapped_repo(_helpers_and_api()))
    assert index is not None

    (community,) = describe(index, tests=False).communities

    assert community.key_symbols == [
        "pkg/core.py::api0",
        "pkg/core.py::api1",
        "pkg/b.py::g",
    ]


def test_a_constructor_counts_toward_its_class(
    make_mapped_repo: RepoFactory,
) -> None:
    sources = {
        "p/Foo.java": (
            "package p;\n\npublic class Foo {\n"
            "    public Foo() {}\n    public int size() { return 1; }\n}\n"
        ),
        "p/A.java": (
            "package p;\n\npublic class A {\n"
            "    int a() { return new Foo().size(); }\n}\n"
        ),
        "p/B.java": (
            "package p;\n\npublic class B {\n"
            "    int b() { return new Foo().size() + new A().a(); }\n}\n"
        ),
    }
    index = mapfile.load_map(make_mapped_repo(sources))
    assert index is not None

    (community,) = describe(index, tests=False).communities

    assert "p/Foo.java::Foo" in community.key_symbols
    assert "p/Foo.java::Foo.Foo" not in community.key_symbols


def _files(directory: str, count: int) -> list[str]:
    return [
        f"{directory}/{directory.replace('/', '_')}{i}.py"
        for i in range(count)
    ]


def test_a_third_directory_tying_the_second_blocks_a_pair() -> None:
    group = [*_files("r", 5), *_files("a", 4), *_files("i", 4)]

    labels = label_groups([group], [group], [*group, "z/w.py"])

    assert labels == ["r0.py"]


def test_a_directory_far_ahead_of_the_rest_names_the_group_alone() -> None:
    group = [*_files("db", 8), *_files("api", 1)]
    group += [f"x{i}/f{i}.py" for i in range(8)]

    assert label_groups([group], [group], [*group, "z/w.py"]) == ["db"]


def test_files_in_the_directory_taken_outweigh_a_subfolder() -> None:
    group = [*_files("app", 4), *_files("app/sub", 2)]

    assert label_groups([group], [group], [*group, "z/w.py"]) == ["app"]


def test_a_hub_added_to_tell_labels_apart_sits_in_the_named_folder() -> None:
    first = ["cli/a.ts", "cli/b.ts", "server/path.ts"]
    second = ["cli/c.ts", "cli/d.ts", "server/engine.ts"]
    ranked = [
        ["server/path.ts", "cli/a.ts", "cli/b.ts"],
        ["server/engine.ts", "cli/c.ts", "cli/d.ts"],
    ]

    labels = label_groups([first, second], ranked, first + second)

    assert labels == ["cli (a.ts)", "cli (c.ts)"]


def test_folders_every_label_shares_are_dropped() -> None:
    groups = [_files("src/pkg/core", 3), _files("src/pkg/web", 3)]
    paths = [*groups[0], *groups[1], "tools/t.py"]

    assert label_groups(groups, groups, paths) == ["core", "web"]


def test_a_group_scattered_inside_the_dropped_folders_is_its_hub() -> None:
    scattered = [f"src/pkg/{d}/{d}.py" for d in ("a", "b", "c", "d")]
    groups = [
        _files("src/pkg/core", 3),
        _files("src/pkg/web", 3),
        scattered,
    ]
    ranked = [groups[0], groups[1], ["src/pkg/c/c.py"]]
    paths = [p for g in groups for p in g] + ["tools/t.py"]

    labels = label_groups(groups, ranked, paths)

    assert labels == ["core", "web", "c.py"]


def test_labels_naming_the_same_folder_both_add_their_hub() -> None:
    single = ["a/x.py", "a/y.py", "a/z.py"]
    pair = ["a/p.py", "a/q.py", "b/r.py", "b/s.py"]
    ranked = [["a/y.py", "a/x.py", "a/z.py"], ["b/r.py", *pair]]

    labels = label_groups([single, pair], ranked, [*single, *pair, "z/w.py"])

    assert labels == ["a (y.py)", "a + b (r.py)"]
