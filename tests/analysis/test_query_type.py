"""``dekko query type``: type-usage search over params/returns."""

import json

import pytest

from dekko.integrations import cli

from conftest import RepoFactory

PY_TYPE_USAGE = {
    "app.py": (
        "from typing import Optional\n"
        "\n"
        "\n"
        "class Config:\n"
        "    pass\n"
        "\n"
        "\n"
        "class ConfigManager:\n"
        "    pass\n"
        "\n"
        "\n"
        "class AppConfig:\n"
        "    pass\n"
        "\n"
        "\n"
        "def start(cfg: Config) -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "def load_config() -> Config:\n"
        "    return Config()\n"
        "\n"
        "\n"
        "def run(cfg: Optional[Config] = None) -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "def run_union(cfg: Config | None = None) -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "def manage(mgr: ConfigManager) -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "def configure(app: AppConfig) -> None:\n"
        "    pass\n"
    ),
}


def test_type_default_matches_param_and_return(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "start(cfg: Config) -> None" in out
    assert "[param: cfg]" in out
    assert "load_config() -> Config" in out
    assert "[return]" in out


def test_type_default_matches_wrapper_syntax(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Optional[Config] and Config | None both tokenize to a bare
    # `Config` identifier, so the default (non-exact) match must find
    # both wrapper forms.
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "run(cfg: Optional[Config]" in out
    assert "run_union(cfg: Config | None" in out


def test_type_default_rejects_similarly_named_types(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # ConfigManager/AppConfig must not match a bare "Config" query —
    # this is the false-positive guard the identifier-token match
    # exists for.
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "manage(mgr: ConfigManager)" not in out
    assert "configure(app: AppConfig)" not in out


def test_type_exact_rejects_wrapper_syntax(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(
        ["query", "type", "Config", "--exact", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    # Only the bare `Config` param/return survive --exact; the wrapped
    # forms (Optional[Config], Config | None) are deliberately dropped.
    assert "start(cfg: Config) -> None" in out
    assert "load_config() -> Config" in out
    assert "run(cfg: Optional[Config]" not in out
    assert "run_union(cfg: Config | None" not in out


def test_type_exact_returns_fewer_or_equal_results(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_TYPE_USAGE)
    assert (
        cli.main(["query", "type", "Config", "--json", "--root", str(root)])
        == 0
    )
    default_doc = json.loads(capsys.readouterr().out)
    assert (
        cli.main(
            [
                "query",
                "type",
                "Config",
                "--exact",
                "--json",
                "--root",
                str(root),
            ]
        )
        == 0
    )
    exact_doc = json.loads(capsys.readouterr().out)
    assert len(exact_doc["results"]) <= len(default_doc["results"])
    assert len(exact_doc["results"]) < len(default_doc["results"])


def test_type_json_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(["query", "type", "Config", "--json", "--root", str(root)])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["action"] == "type"
    assert doc["name"] == "Config"
    assert doc["exact"] is False
    param_entries = {
        e["raw_type"]: e for e in doc["results"] if e["usage"] == "param"
    }
    bare_param = param_entries["Config"]
    assert bare_param["param_name"] == "cfg"
    assert "Optional[Config]" in param_entries
    assert "Config | None" in param_entries
    return_entries = [e for e in doc["results"] if e["usage"] == "return"]
    assert return_entries
    assert "param_name" not in return_entries[0]
    assert return_entries[0]["raw_type"] == "Config"


def test_type_no_matches_reports_closest_type_names(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(["query", "type", "Confg", "--root", str(root)])
    assert code == 3
    err = capsys.readouterr().err
    assert "no results for type 'Confg'" in err
    assert "Config" in err


def test_type_no_matches_json_success_path_unaffected(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # A not-found type target is not a crash in --json mode either;
    # it's the same not-found exit code and stderr report as text mode
    # (no JSON is printed to stdout on a not-found path, matching the
    # 'uses' action's contract).
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(
        ["query", "type", "TotallyUnknownType", "--json", "--root", str(root)]
    )
    assert code == 3
    assert capsys.readouterr().out == ""


def test_type_budget_caps_many_matches(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    pad = "z" * 60
    files = {"types.py": "class Shared:\n    pass\n"}
    for i in range(30):
        files[f"user_{i}.py"] = (
            "from types import Shared\n\n\n"
            f"def use_with_a_long_padded_name_{pad}_{i}"
            "(x: Shared) -> None:\n"
            "    pass\n"
        )
    root = make_mapped_repo(files)
    code = cli.main(["query", "type", "Shared", "--root", str(root)])
    assert code == 0
    assert "omitted" in capsys.readouterr().out


def test_type_json_budget_reports_truncated_by_budget(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    pad = "z" * 60
    files = {"types.py": "class Shared:\n    pass\n"}
    for i in range(30):
        files[f"user_{i}.py"] = (
            "from types import Shared\n\n\n"
            f"def use_with_a_long_padded_name_{pad}_{i}"
            "(x: Shared) -> None:\n"
            "    pass\n"
        )
    root = make_mapped_repo(files)
    code = cli.main(
        [
            "query",
            "type",
            "Shared",
            "--json",
            "--budget",
            "50",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["meta"]["truncated_by"] == "budget"


def test_type_no_tests_excludes_test_files(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "app.py": "class Config:\n    pass\n",
        "tests/test_app.py": (
            "from app import Config\n\n\n"
            "def make_config(cfg: Config) -> None:\n"
            "    pass\n"
        ),
    }
    root = make_mapped_repo(files)

    # Default: test-file usages are included.
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    assert "make_config" in capsys.readouterr().out

    # --no-tests: test-file usages are excluded.
    code = cli.main(
        ["query", "type", "Config", "--no-tests", "--root", str(root)]
    )
    assert code == 3
    assert "make_config" not in capsys.readouterr().out


def test_type_rust_pointer_and_option_wrappers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "lib.rs": (
            "struct Config;\n"
            "\n"
            "fn start(cfg: &Config) {\n"
            "}\n"
            "\n"
            "fn maybe(cfg: Option<Config>) {\n"
            "}\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "start" in out
    assert "maybe" in out


def test_type_go_pointer_wrapper(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "main.go": (
            "package main\n"
            "\n"
            "type Config struct{}\n"
            "\n"
            "func start(cfg *Config) {\n"
            "}\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    assert "start" in capsys.readouterr().out


def test_type_typescript_union_wrapper(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "app.ts": (
            "interface Config {}\n"
            "\n"
            "function start(cfg: Config | undefined): void {\n"
            "}\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    assert "start" in capsys.readouterr().out
