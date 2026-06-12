"""Status subcommand exit codes and the auto-regen/no-regen paths."""

import json
from pathlib import Path

import pytest

from dekko import cli

from conftest import RepoFactory

SRC = {"a.py": "def f() -> int:\n    return 1\n"}


def test_status_missing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert cli.main(["status", "--root", str(tmp_path)]) == 1
    assert "no map.json" in capsys.readouterr().err


def test_status_fresh_and_stale(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert cli.main(["status", "--root", str(root)]) == 0
    assert "map fresh" in capsys.readouterr().out

    (root / "a.py").write_text(SRC["a.py"] + "\nX = 2\n")
    assert cli.main(["status", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "stale" in out
    assert "changed: a.py" in out


def test_status_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    (root / "b.py").write_text("def g() -> int:\n    return 2\n")
    assert cli.main(["status", "--root", str(root), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "stale"
    assert doc["added"] == ["b.py"]


def test_read_command_auto_regenerates(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    (root / "b.py").write_text(
        "from a import f\n\n\ndef g() -> int:\n    return f()\n"
    )
    assert cli.main(["query", "callers", "f", "--root", str(root)]) == 0
    assert "g() -> int" in capsys.readouterr().out
    # the regen also refreshed the map on disk
    assert cli.main(["status", "--root", str(root)]) == 0


def test_no_regen_fails_on_stale(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    (root / "a.py").write_text(SRC["a.py"] + "\nY = 3\n")
    code = cli.main(
        ["query", "symbol", "f", "--root", str(root), "--no-regen"]
    )
    assert code == 5
    assert "missing or stale" in capsys.readouterr().err


def test_map_if_stale_short_circuits(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    assert cli.main(["map", str(root), "--if-stale"]) == 0
    assert "map fresh" in capsys.readouterr().out

    (root / "a.py").write_text(SRC["a.py"] + "\nZ = 4\n")
    assert cli.main(["map", str(root), "--if-stale", "--quiet"]) == 0
    assert cli.main(["status", "--root", str(root)]) == 0
