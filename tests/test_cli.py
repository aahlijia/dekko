"""CLI surface tests: flags, output resolution, plugin install."""

from importlib.metadata import version
from pathlib import Path

import pytest

from lidar_map import cli


def test_version_flag(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert version("lidar-map") in out


def test_bare_invocation_prints_help(capsys: pytest.CaptureFixture) -> None:
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "--map" in out
    assert "--claude-install" in out


def test_map_writes_outputs_to_target_dir(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    assert cli.main(["--map", str(tmp_path), "--quiet"]) == 0
    assert (tmp_path / "MAP.md").is_file()
    assert (tmp_path / "map.json").is_file()


def test_map_rejects_missing_dir(tmp_path: Path) -> None:
    assert cli.main(["--map", str(tmp_path / "nope")]) == 2


def test_output_as_directory(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    assert (
        cli.main(
            [
                "--map",
                str(tmp_path),
                "--output",
                str(out_dir),
                "--quiet",
            ]
        )
        == 0
    )
    assert (out_dir / "MAP.md").is_file()
    assert (out_dir / "map.json").is_file()


def test_output_as_file_renames_json_sibling(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    md = tmp_path / "docs" / "codemap.md"
    assert (
        cli.main(["--map", str(tmp_path), "--output", str(md), "--quiet"]) == 0
    )
    assert md.is_file()
    assert (tmp_path / "docs" / "codemap.json").is_file()


def test_resolve_outputs_defaults(tmp_path: Path) -> None:
    md, js = cli.resolve_outputs(tmp_path, None, None)
    assert md == tmp_path / "MAP.md"
    assert js == tmp_path / "map.json"


def test_resolve_outputs_explicit_json(tmp_path: Path) -> None:
    md, js = cli.resolve_outputs(tmp_path, None, "custom.json")
    assert md == tmp_path / "MAP.md"
    assert js == Path("custom.json")


def test_claude_install_requires_claude_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    assert cli.claude_install() == 1
    assert "claude" in capsys.readouterr().err
