"""Tests for the Cursor installers: the local plugin and ``mcp.json``."""

import json
import os
import shutil
from pathlib import Path

import pytest

from dekko.integrations import cursor

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_BUNDLE = REPO_ROOT / "integrations" / "claude"
FAKE_EXE = "/opt/bin/dekko"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway home directory, so nothing touches the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A copy of the plugin bundle the wheel ships."""
    return Path(shutil.copytree(SOURCE_BUNDLE, tmp_path / "bundle"))


@pytest.fixture
def on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend ``dekko`` resolves to :data:`FAKE_EXE`."""
    monkeypatch.setattr(cursor.shutil, "which", lambda _name: FAKE_EXE)


def _mcp_server(plugin_dir: Path) -> dict:
    """The ``dekko`` server entry in a bundle's ``cursor.mcp.json``."""
    config = json.loads(
        (plugin_dir / "cursor.mcp.json").read_text(encoding="utf-8")
    )
    return config["mcpServers"]["dekko"]


def _foreign_plugin(dest: Path) -> None:
    """Write a plugin at ``dest`` whose manifest names someone else."""
    (dest / ".cursor-plugin").mkdir(parents=True)
    (dest / ".cursor-plugin" / "plugin.json").write_text(
        json.dumps({"name": "other"}),
        encoding="utf-8",
    )


def test_install_plugin_copies_the_bundle(
    home: Path,
    bundle: Path,
    on_path: None,
) -> None:
    rc = cursor.install_plugin(bundle)
    dest = cursor.local_plugin_dir()

    assert rc == 0
    assert dest == home / ".cursor" / "plugins" / "local" / "dekko"
    assert cursor.read_plugin_manifest(dest)["name"] == "dekko"
    assert (dest / "skills").is_dir()
    assert not dest.is_symlink()


def test_install_plugin_points_mcp_at_the_resolved_exe(
    home: Path,
    bundle: Path,
    on_path: None,
) -> None:
    cursor.install_plugin(bundle)
    server = _mcp_server(cursor.local_plugin_dir())

    assert server["command"] == FAKE_EXE
    assert server["args"] == list(cursor.MCP_ARGS)
    assert _mcp_server(bundle)["command"] == "dekko"


def test_install_plugin_keeps_bare_command_without_dekko_on_path(
    home: Path,
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cursor.shutil, "which", lambda _name: None)

    rc = cursor.install_plugin(bundle)

    assert rc == 0
    assert _mcp_server(cursor.local_plugin_dir())["command"] == "dekko"
    assert "not found on PATH" in capsys.readouterr().err


def test_install_plugin_replaces_its_own_previous_copy(
    home: Path,
    bundle: Path,
    on_path: None,
) -> None:
    cursor.install_plugin(bundle)
    stale = cursor.local_plugin_dir() / "stale.txt"
    stale.write_text("old", encoding="utf-8")

    rc = cursor.install_plugin(bundle)

    assert rc == 0
    assert not stale.exists()


def test_install_plugin_refuses_a_foreign_directory(
    home: Path,
    bundle: Path,
    on_path: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dest = cursor.local_plugin_dir()
    _foreign_plugin(dest)

    rc = cursor.install_plugin(bundle)

    assert rc == 2
    assert cursor.read_plugin_manifest(dest) == {"name": "other"}
    assert not (dest / "skills").exists()
    assert str(dest) in capsys.readouterr().err


def test_install_plugin_refuses_a_symlinked_destination(
    home: Path,
    bundle: Path,
    on_path: None,
    tmp_path: Path,
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    dest = cursor.local_plugin_dir()
    dest.parent.mkdir(parents=True)
    try:
        dest.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    rc = cursor.install_plugin(bundle)

    assert rc == 2
    assert dest.is_symlink()
    assert list(target.iterdir()) == []


def test_install_plugin_leaves_no_temp_copy_behind(
    home: Path,
    bundle: Path,
    on_path: None,
) -> None:
    cursor.install_plugin(bundle)
    cursor.install_plugin(bundle)

    assert os.listdir(cursor.local_plugin_dir().parent) == ["dekko"]


def test_install_plugin_failed_copy_keeps_the_previous_one(
    home: Path,
    bundle: Path,
    on_path: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor.install_plugin(bundle)

    def fail(_plugin_dir: Path) -> None:
        raise OSError("disk full")

    # Fails after the new copy is staged, before it's swapped in.
    monkeypatch.setattr(cursor, "_pin_mcp_command", fail)

    with pytest.raises(OSError, match="disk full"):
        cursor.install_plugin(bundle)

    dest = cursor.local_plugin_dir()
    assert cursor.read_plugin_manifest(dest)["name"] == "dekko"
    assert _mcp_server(dest)["command"] == FAKE_EXE
    assert os.listdir(dest.parent) == ["dekko"]


def test_install_plugin_warns_when_the_claude_plugin_is_installed(
    home: Path,
    bundle: Path,
    on_path: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = home / ".claude" / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps({"version": 2, "plugins": {"dekko@dekko": [{}]}}),
        encoding="utf-8",
    )

    cursor.install_plugin(bundle)

    assert "Claude Code" in capsys.readouterr().out


def test_install_plugin_says_nothing_of_claude_without_its_plugin(
    home: Path,
    bundle: Path,
    on_path: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cursor.install_plugin(bundle)

    assert "Claude Code" not in capsys.readouterr().out


def test_install_plugin_fails_without_a_cursor_manifest(
    home: Path,
    bundle: Path,
    on_path: None,
) -> None:
    shutil.rmtree(bundle / ".cursor-plugin")

    rc = cursor.install_plugin(bundle)

    assert rc == 1
    assert not cursor.local_plugin_dir().exists()


def test_uninstall_plugin_removes_its_copy(
    home: Path,
    bundle: Path,
    on_path: None,
) -> None:
    cursor.install_plugin(bundle)

    rc = cursor.uninstall_plugin()

    assert rc == 0
    assert not cursor.local_plugin_dir().exists()


def test_uninstall_plugin_refuses_a_foreign_directory(home: Path) -> None:
    dest = cursor.local_plugin_dir()
    _foreign_plugin(dest)

    rc = cursor.uninstall_plugin()

    assert rc == 2
    assert dest.is_dir()


def test_uninstall_plugin_with_nothing_installed_is_ok(home: Path) -> None:
    assert cursor.uninstall_plugin() == 0


@pytest.mark.parametrize(
    "registry",
    [
        None,
        "not json",
        json.dumps({"version": 2, "plugins": {"other@x": [{}]}}),
        json.dumps({"version": 2, "plugins": []}),
    ],
)
def test_claude_plugin_is_not_installed(
    home: Path,
    registry: str | None,
) -> None:
    if registry is not None:
        path = home / ".claude" / "plugins" / "installed_plugins.json"
        path.parent.mkdir(parents=True)
        path.write_text(registry, encoding="utf-8")

    assert cursor.claude_plugin_installed() is False
