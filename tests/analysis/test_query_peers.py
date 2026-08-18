"""``dekko query peers``: shared-callee co-usage lookup."""

import json

import pytest

from dekko.integrations import cli

from conftest import RepoFactory

PY_PEERS = {
    "app.py": (
        "def read_file():\n"
        "    pass\n"
        "\n"
        "\n"
        "def parse_yaml():\n"
        "    pass\n"
        "\n"
        "\n"
        "def validate():\n"
        "    pass\n"
        "\n"
        "\n"
        "def other_helper():\n"
        "    pass\n"
        "\n"
        "\n"
        "def load_config():\n"
        "    read_file()\n"
        "    parse_yaml()\n"
        "    validate()\n"
        "\n"
        "\n"
        "def bootstrap():\n"
        "    read_file()\n"
        "    parse_yaml()\n"
        "    validate()\n"
        "\n"
        "\n"
        "def start():\n"
        "    read_file()\n"
        "    parse_yaml()\n"
        "\n"
        "\n"
        "def only_one():\n"
        "    read_file()\n"
    ),
    "script.py": (
        "from app import read_file, parse_yaml\n\nread_file()\nparse_yaml()\n"
    ),
}


def test_peers_default_min_shared_excludes_single_overlap(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_PEERS)
    code = cli.main(["query", "peers", "load_config", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "bootstrap" in out
    assert "start" in out
    assert "only_one" not in out


def test_peers_ranks_by_shared_count_descending(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_PEERS)
    code = cli.main(["query", "peers", "load_config", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    bootstrap_idx = out.index("bootstrap")
    start_idx = out.index("start()")
    assert bootstrap_idx < start_idx
    assert "shares:" in out
    assert "(3)" in out
    assert "(2)" in out


def test_peers_min_shared_raises_threshold(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_PEERS)
    code = cli.main(
        [
            "query",
            "peers",
            "load_config",
            "--min-shared",
            "3",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "bootstrap" in out
    assert "start" not in out


def test_peers_min_shared_lowered_includes_weaker_overlap(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_PEERS)
    code = cli.main(
        [
            "query",
            "peers",
            "load_config",
            "--min-shared",
            "1",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "only_one" in out


def test_peers_leaf_symbol_reports_clean_empty_result(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_PEERS)
    code = cli.main(["query", "peers", "other_helper", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "no peers of" in out
    assert "leaf function" in out
    assert "--min-shared" not in out


def test_peers_fan_out_one_suggests_lower_threshold(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # only_one has exactly 1 callee — mathematically impossible to
    # share >= 2 (the default) with anything, so the empty result
    # should suggest --min-shared 1 rather than reading like a bug.
    root = make_mapped_repo(PY_PEERS)
    code = cli.main(["query", "peers", "only_one", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "no peers of" in out
    assert "--min-shared 1" in out


def test_peers_module_level_caller_rendered_without_crash(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # script.py's module-level code calls read_file()+parse_yaml() —
    # a MODULE_CALLER_SUFFIX pseudo-caller id in calls_out, not a real
    # symbol id. This must render as "(module level)", not KeyError.
    root = make_mapped_repo(PY_PEERS)
    code = cli.main(["query", "peers", "load_config", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "script.py" in out
    assert "(module level)" in out


def test_peers_json_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_PEERS)
    code = cli.main(
        ["query", "peers", "load_config", "--json", "--root", str(root)]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["action"] == "peers"
    assert doc["target"].endswith("load_config")
    assert doc["min_shared"] == 2
    bootstrap_entry = next(
        e
        for e in doc["results"]
        if e.get("signature", "").startswith("bootstrap")
    )
    assert bootstrap_entry["shared_count"] == 3
    assert set(bootstrap_entry["shared_callees"]) == {
        "read_file",
        "parse_yaml",
        "validate",
    }
    module_entry = next(
        e for e in doc["results"] if e.get("module_level") is True
    )
    assert module_entry["path"] == "script.py"
    assert module_entry["shared_count"] == 2


def test_peers_json_empty_result_has_empty_results_list(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_PEERS)
    code = cli.main(
        ["query", "peers", "other_helper", "--json", "--root", str(root)]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["results"] == []


def test_peers_budget_caps_many_matches(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    pad = "z" * 60
    files = {
        "app.py": (
            "def a():\n"
            "    pass\n"
            "\n"
            "\n"
            "def b():\n"
            "    pass\n"
            "\n"
            "\n"
            "def load_config():\n"
            "    a()\n"
            "    b()\n"
        ),
    }
    for i in range(30):
        files[f"user_{i}.py"] = (
            "from app import a, b\n"
            "\n"
            "\n"
            f"def use_with_a_long_padded_name_{pad}_{i}():\n"
            "    a()\n"
            "    b()\n"
        )
    root = make_mapped_repo(files)
    code = cli.main(["query", "peers", "load_config", "--root", str(root)])
    assert code == 0
    assert "omitted" in capsys.readouterr().out
