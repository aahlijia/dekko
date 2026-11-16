"""``dekko doctor``'s Codex rows.

Codex's CLI, its state directory and the user's home are all faked:
``shutil.which``/``subprocess.run`` are monkeypatched, and
``CODEX_HOME``/``Path.home`` point into ``tmp_path``, so no test ever
reads or writes the real ``~/.codex``.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dekko.integrations import doctor, hooks, policy_md

CODEX = "/usr/local/bin/codex"


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``CODEX_HOME`` and home dir, with no ``codex`` on PATH."""
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(doctor.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _fake_codex(
    monkeypatch: pytest.MonkeyPatch, outputs: dict[str, str], code: int = 0
) -> list[list[str]]:
    """Answer ``codex <sub> list --json`` from ``outputs`` by subcommand."""
    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, code, outputs[cmd[1]], "")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    return calls


def _plugin_listing(**entry: object) -> str:
    plugin = {"name": "dekko", "enabled": True, "version": "1.0.0", **entry}
    return json.dumps({"installed": [plugin], "available": []})


def _mcp_listing(command: str = "dekko", **entry: object) -> str:
    server = {
        "name": "dekko",
        "enabled": True,
        "transport": {
            "type": "stdio",
            "command": command,
            "args": ["serve", "--mcp"],
        },
        **entry,
    }
    return json.dumps([server])


# --- gating -------------------------------------------------------------


def test_no_codex_rows_without_binary_or_plugin_cache(
    codex_home: Path, repo: Path
) -> None:
    (codex_home / "config.toml").write_text("model = 'x'\n")

    assert doctor._collect_codex(repo) == []


def test_bare_dot_codex_in_home_is_not_enough(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(doctor.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("model = 'x'\n")

    assert doctor._collect_codex(repo) == []


def test_plugin_cache_in_codex_home_shows_rows(
    codex_home: Path, repo: Path
) -> None:
    (codex_home / "plugins" / "cache").mkdir(parents=True)

    names = [f.name for f in doctor._collect_codex(repo)]

    assert names[:3] == [
        "codex-plugin-installed",
        "codex-mcp-registered",
        "agents-md-policy",
    ]
    assert {n for n in names if n.startswith("codex-hook:")} == {
        "codex-hook:session-start",
        "codex-hook:prompt-submit",
        "codex-hook:pre-bash",
    }


def test_plugin_cache_under_default_home_shows_rows(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(doctor.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    (tmp_path / ".codex" / "plugins" / "cache").mkdir(parents=True)

    assert doctor._collect_codex(repo)


def test_codex_binary_shows_rows(
    codex_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: CODEX if name == "codex" else None,
    )
    _fake_codex(
        monkeypatch, {"plugin": _plugin_listing(), "mcp": _mcp_listing()}
    )

    rows = {f.name: f for f in doctor._collect_codex(repo)}

    assert "codex-plugin-installed" in rows
    assert "codex-mcp-registered" in rows


def test_agents_md_row_shows_without_codex_when_block_present(
    codex_home: Path, repo: Path
) -> None:
    policy_md.install(repo, "agents")

    rows = doctor._collect_codex(repo)

    assert [(f.name, f.status) for f in rows] == [("agents-md-policy", "ok")]


def test_collect_includes_codex_rows_in_json(
    codex_home: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(doctor, "_claude_exe", lambda: None)
    monkeypatch.setattr(
        doctor,
        "_check_mcp_server_running",
        lambda: doctor.Finding("mcp-server-running", "ok", "stubbed"),
    )
    (codex_home / "plugins" / "cache").mkdir(parents=True)

    doctor.run(repo, as_json=True)
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}

    assert set(rows["agents-md-policy"]) == {"name", "status", "detail", "fix"}
    assert rows["codex-plugin-installed"]["status"] == "unknown"


# --- plugin -------------------------------------------------------------


def test_plugin_ok_when_installed_and_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "_running_version", lambda: "1.0.0")
    calls = _fake_codex(monkeypatch, {"plugin": _plugin_listing()})

    finding = doctor._check_codex_plugin(CODEX)

    assert finding.status == "ok"
    assert finding.fix is None
    assert calls == [[CODEX, "plugin", "list", "--json"]]


def test_plugin_missing_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = json.dumps({"installed": [{"name": "other"}], "available": []})
    _fake_codex(monkeypatch, {"plugin": listing})

    finding = doctor._check_codex_plugin(CODEX)

    assert finding.status == "missing"
    assert finding.fix == "dekko install codex"


def test_plugin_disabled_is_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_running_version", lambda: "1.0.0")
    _fake_codex(monkeypatch, {"plugin": _plugin_listing(enabled=False)})

    finding = doctor._check_codex_plugin(CODEX)

    assert finding.status == "advisory"
    assert "disabled" in finding.detail


def test_plugin_stale_when_cached_version_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "_running_version", lambda: "1.1.0")
    _fake_codex(monkeypatch, {"plugin": _plugin_listing(version="1.0.0")})

    finding = doctor._check_codex_plugin(CODEX)

    assert finding.status == "stale"
    assert "1.0.0" in finding.detail and "1.1.0" in finding.detail
    assert finding.fix is not None


@pytest.mark.parametrize(
    ("stdout", "status"),
    [("dekko@dekko  1.0.0\n", "ok"), ("nothing here\n", "missing")],
)
def test_plugin_unreadable_json_falls_back_to_text_match(
    monkeypatch: pytest.MonkeyPatch, stdout: str, status: str
) -> None:
    _fake_codex(monkeypatch, {"plugin": stdout})

    assert doctor._check_codex_plugin(CODEX).status == status


def test_plugin_unknown_without_codex_or_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert doctor._check_codex_plugin(None).status == "unknown"

    _fake_codex(monkeypatch, {"plugin": ""}, code=1)
    finding = doctor._check_codex_plugin(CODEX)

    assert finding.status == "unknown"
    assert finding.fix is None


# --- MCP server ---------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    ["dekko", "/home/u/.local/bin/dekko", r"C:\Users\u\bin\dekko.exe"],
)
def test_mcp_ok_when_dekko_server_registered(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    calls = _fake_codex(monkeypatch, {"mcp": _mcp_listing(command)})

    finding = doctor._check_codex_mcp(CODEX)

    assert finding.status == "ok"
    assert calls == [[CODEX, "mcp", "list", "--json"]]


def test_mcp_missing_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_codex(monkeypatch, {"mcp": "[]"})

    finding = doctor._check_codex_mcp(CODEX)

    assert finding.status == "missing"
    assert finding.fix is not None


def test_mcp_disabled_is_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_codex(monkeypatch, {"mcp": _mcp_listing(enabled=False)})

    assert doctor._check_codex_mcp(CODEX).status == "advisory"


def test_mcp_server_not_running_dekko_is_advisory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_codex(monkeypatch, {"mcp": _mcp_listing("python")})

    finding = doctor._check_codex_mcp(CODEX)

    assert finding.status == "advisory"
    assert "dekko serve" in finding.detail


def test_mcp_unreadable_json_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_codex(monkeypatch, {"mcp": "dekko  dekko serve --mcp\n"})

    finding = doctor._check_codex_mcp(CODEX)

    assert finding.status == "unknown"
    assert finding.fix is None


# --- AGENTS.md ----------------------------------------------------------


def test_agents_md_missing_without_block(repo: Path) -> None:
    finding = doctor._check_agents_md(repo)

    assert finding.status == "missing"
    assert finding.fix == "dekko install agents-md"


def test_agents_md_ok_with_block(repo: Path) -> None:
    policy_md.install(repo, "agents")

    assert doctor._check_agents_md(repo).status == "ok"


def test_agents_md_shadowed_by_override(repo: Path) -> None:
    policy_md.install(repo, "agents")
    (repo / "AGENTS.override.md").write_text("# local rules\n")

    finding = doctor._check_agents_md(repo)

    assert finding.status == "advisory"
    assert "shadowed" in finding.detail


def test_agents_md_over_codex_limit(repo: Path) -> None:
    padding = "x" * (policy_md.AGENTS_MD_MAX_BYTES + 1)
    (repo / "AGENTS.md").write_text(f"# rules\n\n{padding}\n")
    policy_md.install(repo, "agents")

    finding = doctor._check_agents_md(repo)

    assert finding.status == "advisory"
    assert str(policy_md.AGENTS_MD_MAX_BYTES) in finding.detail


# --- hooks --------------------------------------------------------------

_CODEX_EVENTS = ["session-start", "prompt-submit", "pre-bash"]


def _hook_rows(
    repo: Path, monkeypatch: pytest.MonkeyPatch, state: dict | None
) -> dict[str, doctor.Finding]:
    monkeypatch.setattr(doctor, "_load_codex_hook_state", lambda: state)
    return {f.name: f for f in doctor._check_codex_hooks(repo)}


def test_hooks_missing_without_codex_hook_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _hook_rows(repo, monkeypatch, {})

    assert {f.status for f in rows.values()} == {"missing"}
    assert rows["codex-hook:pre-bash"].fix == (
        "dekko hooks install --agent codex --enable pre-bash"
    )


def test_hooks_wired_but_not_reviewed_are_advisory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks.install(repo, _CODEX_EVENTS, agent="codex")

    rows = _hook_rows(repo, monkeypatch, {})

    assert {f.status for f in rows.values()} == {"advisory"}
    assert "not yet reviewed" in rows["codex-hook:session-start"].detail


def test_hooks_ok_when_trust_unreadable(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks.install(repo, _CODEX_EVENTS, agent="codex")

    rows = _hook_rows(repo, monkeypatch, None)

    assert {f.status for f in rows.values()} == {"ok"}
    assert "trust not checked" in rows["codex-hook:pre-bash"].detail


def test_hooks_ok_when_trusted(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks.install(repo, ["pre-bash"], strict=True, agent="codex")
    path = hooks.settings_path(repo, "codex")
    key = f"{path.absolute()}:pre_tool_use:0:0"

    rows = _hook_rows(repo, monkeypatch, {key: {"trusted_hash": "sha256:x"}})

    finding = rows["codex-hook:pre-bash"]
    assert finding.status == "ok"
    assert "trusted (hash not verified)" in finding.detail


def test_hook_entry_changed_by_hand_is_stale(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks.install(repo, ["session-start"], agent="codex")
    path = hooks.settings_path(repo, "codex")
    settings = json.loads(path.read_text())
    settings["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 30
    path.write_text(json.dumps(settings))

    finding = _hook_rows(repo, monkeypatch, {})["codex-hook:session-start"]

    assert finding.status == "stale"
    assert finding.fix is not None


def test_claude_command_in_codex_file_is_stale(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = hooks.settings_path(repo, "codex")
    path.parent.mkdir()
    entry = hooks._entry("pre-bash", "Bash")
    path.write_text(json.dumps({"hooks": {"PreToolUse": [entry]}}))

    finding = _hook_rows(repo, monkeypatch, {})["codex-hook:pre-bash"]

    assert finding.status == "stale"


def test_trust_keys_use_codex_snake_case_event_labels(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "hooks.json"

    keys = doctor._codex_trust_keys(path, "UserPromptSubmit", 2, 1)

    assert f"{path.absolute()}:user_prompt_submit:2:1" in keys


@pytest.mark.skipif(
    sys.version_info < (3, 11), reason="tomllib is stdlib from 3.11"
)
def test_hook_state_read_from_codex_config(codex_home: Path) -> None:
    (codex_home / "config.toml").write_text(
        '[hooks.state."/r/.codex/hooks.json:pre_tool_use:0:0"]\n'
        'trusted_hash = "sha256:abc"\n'
    )

    state = doctor._load_codex_hook_state()

    assert state == {
        "/r/.codex/hooks.json:pre_tool_use:0:0": {"trusted_hash": "sha256:abc"}
    }


@pytest.mark.skipif(
    sys.version_info < (3, 11), reason="tomllib is stdlib from 3.11"
)
def test_hook_state_empty_without_codex_config(codex_home: Path) -> None:
    assert doctor._load_codex_hook_state() == {}


@pytest.mark.skipif(sys.version_info >= (3, 11), reason="3.10 has no tomllib")
def test_hook_state_unknown_on_python_310(codex_home: Path) -> None:
    assert doctor._load_codex_hook_state() is None
