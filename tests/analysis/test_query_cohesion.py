"""``dekko query cohesion``: intra-file connected-components (weak
signal — connectivity only, not real clustering; see the design doc
in ``.features/plans/post-indexing-tooling/``).
"""

import json

import pytest

from dekko.integrations import cli

from conftest import RepoFactory

TWO_GROUPS = {
    "app.py": (
        "def parse_config():\n"
        "    validate_config()\n"
        "\n"
        "\n"
        "def validate_config():\n"
        "    pass\n"
        "\n"
        "\n"
        "def load_config():\n"
        "    parse_config()\n"
        "\n"
        "\n"
        "def render_html():\n"
        "    render_css()\n"
        "\n"
        "\n"
        "def render_css():\n"
        "    pass\n"
        "\n"
        "\n"
        "def standalone_helper():\n"
        "    pass\n"
    ),
}

ALL_ISOLATED = {
    "app.py": (
        "def a():\n    pass\n\n\ndef b():\n    pass\n\n\ndef c():\n    pass\n"
    ),
}

ONE_COMPONENT = {
    "app.py": (
        "def a():\n    b()\n\n\ndef b():\n    c()\n\n\ndef c():\n    pass\n"
    ),
}


def test_cohesion_splits_two_groups_and_an_isolate(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_GROUPS)
    code = cli.main(["query", "cohesion", "app.py", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "6 symbols, 3 intra-file edges" in out
    assert "weak signal" in out
    first = out.index("connected component 1")
    second = out.index("connected component 2")
    assert first < second
    # Larger cluster (3 members) listed before the smaller (2 members).
    assert "connected component 1 (3 symbols)" in out
    assert "parse_config" in out
    assert "validate_config" in out
    assert "load_config" in out
    assert "connected component 2 (2 symbols)" in out
    assert "render_html" in out
    assert "render_css" in out
    assert "isolated (1 symbols, no intra-file edges): standalone_helper" in (
        out
    )


def test_cohesion_note_is_the_verbatim_weak_signal_disclosure(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # The design doc calls this note "load-bearing, not decorative" —
    # it must always ship, never be dropped by budget/limit capping.
    root = make_mapped_repo(TWO_GROUPS)
    code = cli.main(["query", "cohesion", "app.py", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "mutually reachable" in out
    assert "not implemented" in out


def test_cohesion_all_isolated_when_no_intra_file_edges(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ALL_ISOLATED)
    code = cli.main(["query", "cohesion", "app.py", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "3 symbols, 0 intra-file edges" in out
    assert "connected component 1" not in out
    assert "isolated (3 symbols" in out
    for name in ("a", "b", "c"):
        assert name in out


def test_cohesion_single_connected_component_has_no_isolated_line(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # The common (and, per the design doc, unhelpful) case: a fully
    # connected file gets one big cluster and no useful split signal.
    root = make_mapped_repo(ONE_COMPONENT)
    code = cli.main(["query", "cohesion", "app.py", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "connected component 1 (3 symbols)" in out
    assert "connected component 2" not in out
    assert "isolated" not in out


def test_cohesion_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_GROUPS)
    code = cli.main(["query", "cohesion", "nope.py", "--root", str(root)])
    assert code == 3
    assert "no mapped file matches" in capsys.readouterr().err


def test_cohesion_ambiguous_path(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Neither candidate is an exact match for "app.py" (which would
    # win outright per ``paths_matching``) — both are suffix matches,
    # so the ambiguity has to be resolved by the caller.
    files = {
        "a/app.py": TWO_GROUPS["app.py"],
        "b/app.py": TWO_GROUPS["app.py"],
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "cohesion", "app.py", "--root", str(root)])
    assert code == 4
    assert "ambiguous" in capsys.readouterr().err


def test_cohesion_json_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_GROUPS)
    code = cli.main(
        ["query", "cohesion", "app.py", "--json", "--root", str(root)]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["action"] == "cohesion"
    assert doc["path"].endswith("app.py")
    assert doc["symbol_count"] == 6
    assert doc["edge_count"] == 3
    assert doc["weak_signal"] is True
    assert "mutually reachable" in doc["note"]
    sizes = [c["size"] for c in doc["components"]]
    assert sizes == sorted(sizes, reverse=True)
    assert doc["components"][0]["symbols"] == [
        "parse_config",
        "validate_config",
        "load_config",
    ]
    assert doc["components"][1]["symbols"] == ["render_html", "render_css"]
    assert doc["isolated"] == ["standalone_helper"]


def test_cohesion_json_all_isolated_has_empty_components(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(ALL_ISOLATED)
    code = cli.main(
        ["query", "cohesion", "app.py", "--json", "--root", str(root)]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["components"] == []
    assert set(doc["isolated"]) == {"a", "b", "c"}
