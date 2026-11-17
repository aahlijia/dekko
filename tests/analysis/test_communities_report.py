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

    assert label_groups(groups, ["a.py", "x.py"], paths) == ["core", "web"]


def test_two_directories_share_a_label_when_neither_has_half() -> None:
    group = ["p/a.py", "p/d.py", "q/b.py", "q/e.py", "r/c.py"]

    assert label_groups([group], ["a.py"], [*group, "z/w.py"]) == ["p + q"]


def test_a_scattered_community_is_named_by_its_hub() -> None:
    group = ["a/1.py", "b/2.py", "c/3.py", "d/4.py", "e/5.py"]

    assert label_groups([group], ["c/3.py"], group) == ["3.py"]


def test_a_community_that_scatters_below_a_directory_adds_its_hub() -> None:
    group = [f"src/{d}/{i}.py" for i, d in enumerate("abcde")]

    labels = label_groups([group], ["src/c/2.py"], [*group, "lib/x.py"])

    assert labels == ["src (2.py)"]


def test_long_directory_names_lose_their_middle() -> None:
    deep = "core/spring-boot/src/main/java/org/x"
    group = [f"{deep}/A.java", f"{deep}/B.java", f"{deep}/C.java"]

    labels = label_groups([group], ["A.java"], [*group, "other/D.java"])

    assert labels == ["core/spring-boot/…/x"]


def test_communities_with_the_same_label_name_their_hub() -> None:
    groups = [
        ["web/a.py", "web/b.py", "web/c.py"],
        ["web/m.py", "web/n.py", "web/o.py"],
    ]
    paths = [*groups[0], *groups[1], "api/q.py"]

    labels = label_groups(groups, ["web/a.py", "web/m.py"], paths)

    assert labels == ["web (a.py)", "web (m.py)"]


def test_a_community_split_evenly_names_both_halves() -> None:
    group = ["app/a.py", "app/b.py", "lib/c.py", "lib/d.py"]

    assert label_groups([group], ["app/a.py"], [*group, "x/y.py"]) == [
        "app + lib"
    ]


def test_the_label_only_descends_into_the_directory_it_took() -> None:
    # src/ holds 3 of 5 files, one per subfolder; tools/x holds the
    # other 2. One level down tools/x outranks every src/ child, but it
    # sits outside src/, so it must not name the community.
    group = ["src/a/1.py", "src/b/2.py", "src/c/3.py", "tools/x/4.py"]
    group.append("tools/x/5.py")

    labels = label_groups([group], ["src/a/1.py"], [*group, "z/w.py"])

    assert labels == ["src (1.py)"]


def test_hub_named_communities_with_the_same_hub_name_use_its_path() -> None:
    groups = [
        [f"{d}/index.ts" for d in ("a", "b", "c", "d", "e")],
        [f"{d}/index.ts" for d in ("f", "g", "h", "i", "j")],
    ]

    labels = label_groups(
        groups, ["c/index.ts", "h/index.ts"], groups[0] + groups[1]
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
