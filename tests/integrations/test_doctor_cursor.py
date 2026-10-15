"""``dekko doctor``'s Cursor rows.

The home directory, ``shutil.which`` and the macOS app path are all
faked, so no test reads or writes the real ``~/.cursor``.
"""

import json
import shutil
import stat
from pathlib import Path

import pytest

from dekko.integrations import cursor, doctor, hooks, policy_md

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE = REPO_ROOT / "integrations" / "claude"
VERSION = "1.2.3"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home dir, no Cursor app, and nothing on PATH."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(doctor, "_CURSOR_APP", tmp_path / "no-Cursor.app")
    monkeypatch.setattr(doctor, "_running_version", lambda: VERSION)
    _on_path(monkeypatch, {})
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def dekko_exe(tmp_path: Path) -> str:
    """A real executable file standing in for the ``dekko`` binary."""
    exe = tmp_path / "bin" / "dekko"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


def _on_path(monkeypatch: pytest.MonkeyPatch, found: dict[str, str]) -> None:
    """Make ``shutil.which`` resolve exactly the names in ``found``."""
    monkeypatch.setattr(doctor.shutil, "which", found.get)


def _install_plugin(command: str, version: str = VERSION) -> Path:
    """Write a dekko plugin copy the way ``dekko install cursor`` does."""
    dest = cursor.local_plugin_dir()
    shutil.copytree(BUNDLE, dest)
    manifest = dest / ".cursor-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = version
    manifest.write_text(json.dumps(data), encoding="utf-8")
    mcp = dest / "cursor.mcp.json"
    config = json.loads(mcp.read_text(encoding="utf-8"))
    config["mcpServers"]["dekko"]["command"] = command
    mcp.write_text(json.dumps(config), encoding="utf-8")
    return dest


def _rows(root: Path) -> dict[str, doctor.Finding]:
    """The Cursor rows of a doctor run, by name."""
    return {
        f.name: f
        for f in doctor._collect_cursor(root)
        if f.name.startswith("cursor")
    }


# --- gating -------------------------------------------------------------


def test_no_cursor_rows_without_cursor(home: Path, repo: Path) -> None:
    assert not doctor._cursor_present()
    assert doctor._collect_cursor(repo) == []


def test_a_bare_dot_cursor_dir_shows_no_rows(home: Path, repo: Path) -> None:
    (home / ".cursor" / "projects").mkdir(parents=True)

    assert not doctor._cursor_present()


def test_an_unrelated_agent_binary_shows_no_rows(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = tmp_path / "usr" / "bin" / "agent"
    agent.parent.mkdir(parents=True)
    agent.write_text("", encoding="utf-8")
    _on_path(monkeypatch, {"agent": str(agent)})

    assert not doctor._cursor_present()


def test_cursor_agent_on_path_shows_rows(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = tmp_path / "share" / "cursor-agent" / "versions" / "1" / "agent"
    real.parent.mkdir(parents=True)
    real.write_text("", encoding="utf-8")
    link = tmp_path / "bin" / "agent"
    link.parent.mkdir()
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")
    _on_path(monkeypatch, {"agent": str(link)})

    assert doctor._cursor_present()


@pytest.mark.parametrize("name", ["cursor-agent", "cursor"])
def test_a_cursor_command_on_path_shows_rows(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    _on_path(monkeypatch, {name: f"/usr/local/bin/{name}"})

    assert doctor._cursor_present()


def test_the_cursor_app_shows_rows(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "Cursor.app"
    app.mkdir()
    monkeypatch.setattr(doctor, "_CURSOR_APP", app)
    monkeypatch.setattr(doctor.sys, "platform", "darwin")

    assert doctor._cursor_present()


def test_our_plugin_copy_shows_rows(home: Path, dekko_exe: str) -> None:
    _install_plugin(dekko_exe)

    assert doctor._cursor_present()


def test_collect_includes_cursor_rows(
    home: Path,
    repo: Path,
    dekko_exe: str,
) -> None:
    _install_plugin(dekko_exe)

    names = {f.name for f in doctor.collect(repo)}

    assert "cursor-plugin" in names


# --- the plugin row -----------------------------------------------------


def test_plugin_row_is_ok_for_the_current_version(
    home: Path,
    repo: Path,
    dekko_exe: str,
) -> None:
    _install_plugin(dekko_exe)

    row = _rows(repo)["cursor-plugin"]

    assert row.status == "ok"
    assert row.fix is None


def test_plugin_row_is_stale_for_an_old_copy(
    home: Path,
    repo: Path,
    dekko_exe: str,
) -> None:
    _install_plugin(dekko_exe, version="0.0.1")

    row = _rows(repo)["cursor-plugin"]

    assert row.status == "stale"
    assert "0.0.1" in row.detail
    assert row.fix == "dekko install cursor"


def test_plugin_row_flags_an_unresolvable_command(
    home: Path,
    repo: Path,
    tmp_path: Path,
) -> None:
    _install_plugin(str(tmp_path / "gone" / "dekko"))

    row = _rows(repo)["cursor-plugin"]

    assert row.status == "stale"
    assert row.fix == "dekko install cursor"


def test_plugin_row_is_missing_without_a_copy(
    home: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_path(monkeypatch, {"cursor": "/usr/local/bin/cursor"})

    row = _rows(repo)["cursor-plugin"]

    assert row.status == "missing"
    assert row.fix == "dekko install cursor"


def test_plugin_row_flags_a_foreign_directory(
    home: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_path(monkeypatch, {"cursor": "/usr/local/bin/cursor"})
    dest = cursor.local_plugin_dir()
    (dest / ".cursor-plugin").mkdir(parents=True)
    (dest / ".cursor-plugin" / "plugin.json").write_text('{"name": "x"}')

    row = _rows(repo)["cursor-plugin"]

    assert row.status == "unknown"
    assert str(dest) in row.detail


# --- the MCP row --------------------------------------------------------


def test_mcp_row_reads_the_project_file(
    home: Path,
    repo: Path,
    dekko_exe: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_path(monkeypatch, {"cursor": "/usr/local/bin/cursor"})
    path = cursor.mcp_config_path(repo)
    path.parent.mkdir()
    path.write_text(
        json.dumps({"mcpServers": {"dekko": {"command": dekko_exe}}}),
        encoding="utf-8",
    )

    row = _rows(repo)["cursor-mcp"]

    assert row.status == "ok"
    assert str(path) in row.detail


def test_mcp_row_reads_the_global_file(
    home: Path,
    repo: Path,
    dekko_exe: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_path(monkeypatch, {"cursor": "/usr/local/bin/cursor"})
    path = home / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"mcpServers": {"dekko": {"command": dekko_exe}}}),
        encoding="utf-8",
    )

    assert _rows(repo)["cursor-mcp"].status == "ok"


def test_mcp_row_flags_an_unresolvable_command(
    home: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_path(monkeypatch, {"cursor": "/usr/local/bin/cursor"})
    path = cursor.mcp_config_path(repo)
    path.parent.mkdir()
    path.write_text(
        json.dumps({"mcpServers": {"dekko": {"command": "dekko"}}}),
        encoding="utf-8",
    )

    row = _rows(repo)["cursor-mcp"]

    assert row.status == "stale"
    assert row.fix == "dekko install cursor --mcp-only"


def test_mcp_row_is_ok_through_the_plugin(
    home: Path,
    repo: Path,
    dekko_exe: str,
) -> None:
    _install_plugin(dekko_exe)

    assert _rows(repo)["cursor-mcp"].status == "ok"


def test_mcp_row_is_missing_without_any_entry(
    home: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_path(monkeypatch, {"cursor": "/usr/local/bin/cursor"})

    row = _rows(repo)["cursor-mcp"]

    assert row.status == "missing"
    assert row.fix == "dekko install cursor --mcp-only"


# --- the policy row -----------------------------------------------------


@pytest.mark.parametrize("file_key", ["agents", "claude"])
def test_policy_row_accepts_claude_md_or_agents_md(
    home: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_key: str,
) -> None:
    _on_path(monkeypatch, {"cursor": "/usr/local/bin/cursor"})
    assert _rows(repo)["cursor-policy"].status == "missing"

    policy_md.install(repo, file_key)

    assert _rows(repo)["cursor-policy"].status == "ok"


# --- hook rows ----------------------------------------------------------


def test_hook_rows_ok_after_install_and_stale_after_edit(
    home: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_path(monkeypatch, {"cursor": "/usr/local/bin/cursor"})
    events = list(hooks.events_for("cursor"))
    assert _rows(repo)["cursor-hook:pre-bash"].status == "missing"

    hooks.install(repo, events, strict=True, agent="cursor")
    rows = _rows(repo)

    assert {rows[f"cursor-hook:{e}"].status for e in events} == {"ok"}

    path = hooks.settings_path(repo, "cursor")
    config = json.loads(path.read_text(encoding="utf-8"))
    config["hooks"]["sessionStart"][0]["timeout"] = 5
    path.write_text(json.dumps(config), encoding="utf-8")

    row = _rows(repo)["cursor-hook:session-start"]
    assert row.status == "stale"
    assert "--agent cursor" in (row.fix or "")


# --- overlap ------------------------------------------------------------


def test_overlap_row_is_ok_without_overlap(
    home: Path,
    repo: Path,
    dekko_exe: str,
) -> None:
    _install_plugin(dekko_exe)

    assert _rows(repo)["cursor-claude-overlap"].status == "ok"


def test_overlap_row_flags_both_plugins(
    home: Path,
    repo: Path,
    dekko_exe: str,
) -> None:
    _install_plugin(dekko_exe)
    registry = home / ".claude" / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps({"version": 2, "plugins": {"dekko@dekko": [{}]}}),
        encoding="utf-8",
    )

    row = _rows(repo)["cursor-claude-overlap"]

    assert row.status == "advisory"
    assert "Third-Party Imports" in (row.fix or "")


def test_overlap_row_flags_hooks_in_both_files(
    home: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_path(monkeypatch, {"cursor": "/usr/local/bin/cursor"})
    hooks.install(repo, ["session-start"])
    hooks.install(repo, ["session-start"], agent="cursor")

    row = _rows(repo)["cursor-claude-overlap"]

    assert row.status == "advisory"
    assert ".claude/settings.json" in row.detail
