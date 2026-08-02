"""Cline MCP config install/uninstall: idempotent JSON merge/removal.

Mirrors ``tests/test_hooks.py``'s style for the analogous
``.claude/settings.json`` installer, but against a single named key
(``mcpServers.dekko``) rather than a list of hook entries, and against
Cline's more conservative "abort on malformed JSON" contract.
"""

import json
from pathlib import Path

import pytest

from dekko import cline


def _config(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture(autouse=True)
def _dekko_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``shutil.which("dekko")`` resolve to a fixed path by default."""
    monkeypatch.setattr(
        cline.shutil, "which", lambda name: "/usr/local/bin/dekko"
    )


# --- default_config_path ----------------------------------------------


def test_default_config_path_vscode_scope() -> None:
    path = cline.default_config_path("vscode")
    assert path.name == "cline_mcp_settings.json"
    assert "saoudrizwan.claude-dev" in path.parts


def test_default_config_path_global_scope() -> None:
    path = cline.default_config_path("global")
    assert path == Path.home() / ".cline" / "settings" / (
        "cline_mcp_settings.json"
    )


def test_default_config_path_rejects_unknown_scope() -> None:
    with pytest.raises(ValueError):
        cline.default_config_path("nonsense")


# --- install ------------------------------------------------------------


def test_install_writes_fresh_config(tmp_path: Path) -> None:
    target = tmp_path / "cline_mcp_settings.json"
    assert cline.install(target) == 0
    servers = _config(target)["mcpServers"]
    assert servers["dekko"] == {
        "type": "stdio",
        "command": "/usr/local/bin/dekko",
        "args": ["serve", "--mcp"],
        "cwd": "${workspaceFolder}",
    }


def test_install_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "cline_mcp_settings.json"
    cline.install(target)
    cline.install(target)
    servers = _config(target)["mcpServers"]
    assert list(servers) == ["dekko"]


def test_install_preserves_unrelated_servers_and_keys(
    tmp_path: Path,
) -> None:
    target = tmp_path / "cline_mcp_settings.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {"other": {"command": "other-server"}},
                "someOtherClineSetting": True,
            }
        )
    )
    assert cline.install(target) == 0
    doc = _config(target)
    assert doc["someOtherClineSetting"] is True
    assert doc["mcpServers"]["other"] == {"command": "other-server"}
    assert "dekko" in doc["mcpServers"]


def test_install_fails_when_dekko_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setattr(cline.shutil, "which", lambda _name: None)
    target = tmp_path / "cline_mcp_settings.json"
    assert cline.install(target) == 1
    assert "dekko" in capsys.readouterr().err
    assert not target.exists()


def test_install_aborts_on_malformed_json_without_force(
    tmp_path: Path, capsys: object
) -> None:
    target = tmp_path / "cline_mcp_settings.json"
    target.write_text("{not json")
    assert cline.install(target) == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert target.read_text() == "{not json"  # untouched


def test_install_force_resets_malformed_json(tmp_path: Path) -> None:
    target = tmp_path / "cline_mcp_settings.json"
    target.write_text("{not json")
    assert cline.install(target, force=True) == 0
    assert "dekko" in _config(target)["mcpServers"]


def test_install_creates_missing_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "cline_mcp_settings.json"
    assert cline.install(target) == 0
    assert target.is_file()


def test_install_default_path_warns_when_scope_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setattr(
        cline, "default_config_path", lambda scope: tmp_path / "x.json"
    )
    monkeypatch.setattr(cline, "_scope_plausibly_installed", lambda s: False)
    assert cline.install() == 0
    assert "doesn't appear to be" in capsys.readouterr().err


def test_install_explicit_config_skips_scope_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setattr(cline, "_scope_plausibly_installed", lambda s: False)
    target = tmp_path / "cline_mcp_settings.json"
    assert cline.install(target) == 0
    assert "doesn't appear to be" not in capsys.readouterr().err


# --- uninstall ------------------------------------------------------------


def test_uninstall_removes_only_dekko(tmp_path: Path) -> None:
    target = tmp_path / "cline_mcp_settings.json"
    target.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "dekko": {"command": "/usr/local/bin/dekko"},
                    "other": {"command": "other-server"},
                }
            }
        )
    )
    assert cline.uninstall(target) == 0
    doc = _config(target)
    assert list(doc["mcpServers"]) == ["other"]


def test_uninstall_drops_empty_mcp_servers_key(tmp_path: Path) -> None:
    target = tmp_path / "cline_mcp_settings.json"
    target.write_text(
        json.dumps({"mcpServers": {"dekko": {"command": "dekko"}}})
    )
    assert cline.uninstall(target) == 0
    assert "mcpServers" not in _config(target)


def test_uninstall_tolerates_missing_file(
    tmp_path: Path, capsys: object
) -> None:
    target = tmp_path / "cline_mcp_settings.json"
    assert cline.uninstall(target) == 0
    assert "nothing to remove" in capsys.readouterr().out
    assert not target.exists()


def test_uninstall_tolerates_missing_dekko_key(
    tmp_path: Path, capsys: object
) -> None:
    target = tmp_path / "cline_mcp_settings.json"
    target.write_text(json.dumps({"mcpServers": {"other": {}}}))
    assert cline.uninstall(target) == 0
    assert "nothing to remove" in capsys.readouterr().out
    doc = _config(target)
    assert doc["mcpServers"] == {"other": {}}


def test_uninstall_aborts_on_malformed_json_without_force(
    tmp_path: Path, capsys: object
) -> None:
    target = tmp_path / "cline_mcp_settings.json"
    target.write_text("{not json")
    assert cline.uninstall(target) == 2
    assert "not valid JSON" in capsys.readouterr().err
