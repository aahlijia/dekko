"""``dekko doctor``: composed environment/install-state diagnostic.

Each check is exercised in isolation (monkeypatching ``shutil.which``/
``subprocess.run`` to simulate PATH shadowing, a missing ``claude``
CLI, and a live/absent ``dekko serve --mcp`` process), then the
composed report is exercised through ``doctor.collect()`` and the CLI
entrypoint, mirroring ``test_hooks.py``'s/``test_claude_md.py``'s
fixture use.
"""

import json
import subprocess
from pathlib import Path

import pytest

from dekko.integrations import claude_md, cli, doctor, hooks

from conftest import RepoFactory

SRC = {"a.py": "def f() -> int:\n    return 1\n"}


# --- binary resolution -------------------------------------------------


def test_binary_resolution_not_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    monkeypatch.setattr(doctor.sys, "argv", ["/opt/dekko/bin/dekko"])
    finding = doctor._check_binary_resolution()
    assert finding.status == "missing"
    assert finding.fix is not None


def test_binary_resolution_matches_running_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    running = tmp_path / "dekko"
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: str(running))
    monkeypatch.setattr(doctor.sys, "argv", [str(running)])
    finding = doctor._check_binary_resolution()
    assert finding.status == "ok"
    assert finding.fix is None


def test_binary_resolution_shadowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    running = tmp_path / "running" / "dekko"
    shadowing = tmp_path / "shadow" / "dekko"
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: str(shadowing))
    monkeypatch.setattr(doctor.sys, "argv", [str(running)])

    def fake_run(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, "dekko 0.1.0\n", "")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    finding = doctor._check_binary_resolution()
    assert finding.status == "stale"
    assert finding.fix is not None
    assert "0.1.0" in finding.detail


# --- map freshness delegation -------------------------------------------


def test_map_freshness_delegates_to_status_signal(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    fresh_finding = doctor._check_map_freshness(root)
    assert fresh_finding.status == "ok"
    assert fresh_finding.fix is None

    (root / "a.py").write_text(SRC["a.py"] + "\nX = 2\n")
    stale_finding = doctor._check_map_freshness(root)
    assert stale_finding.status == "stale"
    assert stale_finding.fix == "dekko map"


def test_map_freshness_missing_map(tmp_path: Path) -> None:
    finding = doctor._check_map_freshness(tmp_path)
    assert finding.status == "missing"
    assert finding.fix == "dekko map"


def test_map_freshness_version_stale(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    map_path = root / ".dekko" / "map.json"
    doc = json.loads(map_path.read_text())
    doc["provenance"]["tool_version"] = "0.0.0-stale"
    map_path.write_text(json.dumps(doc))

    finding = doctor._check_map_freshness(root)
    assert finding.status == "stale"
    assert "0.0.0-stale" in finding.detail
    assert finding.fix == "dekko map"


# --- MCP/plugin registration --------------------------------------------


def test_claude_registration_unknown_without_claude_cli() -> None:
    finding = doctor._check_mcp_registered(None)
    assert finding.status == "unknown"
    assert finding.fix is None


def test_mcp_registered_ok_when_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, "dekko  Connected\n", "")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    finding = doctor._check_mcp_registered("/usr/bin/claude")
    assert finding.status == "ok"
    assert finding.fix is None


def test_mcp_registered_missing_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            cmd, 0, "no servers configured\n", ""
        )

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    finding = doctor._check_mcp_registered("/usr/bin/claude")
    assert finding.status == "missing"
    assert finding.fix is not None


def test_plugin_installed_unknown_on_claude_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    finding = doctor._check_plugin_installed("/usr/bin/claude")
    assert finding.status == "unknown"
    assert finding.fix is None


# --- MCP server liveness -------------------------------------------------


def test_mcp_server_running_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "darwin")

    def fake_run(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    finding = doctor._check_mcp_server_running()
    assert finding.status == "ok"
    assert finding.fix is None


def test_mcp_server_running_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "darwin")

    def fake_run(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            cmd, 0, "12345 dekko serve --mcp\n", ""
        )

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    finding = doctor._check_mcp_server_running()
    assert finding.status == "unknown"
    assert "12345" in finding.detail
    assert finding.fix == "restart Claude Code"


def test_mcp_server_running_skipped_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    finding = doctor._check_mcp_server_running()
    assert finding.status == "unknown"
    assert finding.fix is None


def test_mcp_server_running_degrades_when_pgrep_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "darwin")

    def fake_run(
        cmd: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        raise FileNotFoundError("pgrep not found")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    finding = doctor._check_mcp_server_running()
    assert finding.status == "unknown"
    assert finding.fix is None


# --- hooks / CLAUDE.md opt-in layers ------------------------------------


def test_hooks_none_installed(tmp_path: Path) -> None:
    findings = doctor._check_hooks(tmp_path)
    assert len(findings) == 4
    assert all(f.status == "missing" for f in findings)
    assert all(f.fix is not None for f in findings)


def test_hooks_one_installed(tmp_path: Path) -> None:
    hooks.install(tmp_path, ["session-start"])
    findings = {f.name: f for f in doctor._check_hooks(tmp_path)}
    assert findings["hook:session-start"].status == "ok"
    assert findings["hook:session-start"].fix is None
    assert findings["hook:prompt-submit"].status == "missing"


def test_hooks_all_installed(tmp_path: Path) -> None:
    hooks.install(
        tmp_path,
        ["session-start", "prompt-submit", "pre-read", "pre-bash"],
    )
    findings = doctor._check_hooks(tmp_path)
    assert all(f.status == "ok" for f in findings)
    assert all(f.fix is None for f in findings)


def test_claude_md_absent(tmp_path: Path) -> None:
    finding = doctor._check_claude_md(tmp_path)
    assert finding.status == "missing"
    assert finding.fix == "dekko --claude-md-install"


def test_claude_md_present(tmp_path: Path) -> None:
    claude_md.install(tmp_path)
    finding = doctor._check_claude_md(tmp_path)
    assert finding.status == "ok"
    assert finding.fix is None


# --- composed report / --json shape -------------------------------------


def test_collect_json_shape_ok_rows_have_no_fix(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_mapped_repo(SRC)
    monkeypatch.setattr(doctor, "_claude_exe", lambda: None)
    findings = doctor.collect(root)
    assert findings, "expected at least one finding"
    for f in findings:
        assert f.name and f.status in {"ok", "missing", "stale", "unknown"}
        assert isinstance(f.detail, str) and f.detail
        if f.status == "ok":
            assert f.fix is None
        if f.status in {"missing", "stale"}:
            assert f.fix is not None


def test_collect_reports_gaps_on_bare_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "_claude_exe", lambda: None)
    findings = {f.name: f for f in doctor.collect(tmp_path)}
    assert findings["map-freshness"].status == "missing"
    assert findings["claude-md-policy"].status == "missing"
    assert findings["mcp-registered"].status == "unknown"
    assert findings["plugin-installed"].status == "unknown"


def test_check_failure_degrades_to_unknown_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(doctor, "_check_binary_resolution", boom)
    findings = {f.name: f for f in doctor.collect(tmp_path)}
    assert findings["binary-resolution"].status == "unknown"
    assert "simulated failure" in findings["binary-resolution"].detail


# --- CLI wiring -----------------------------------------------------------


def test_cli_doctor_smoke_on_bare_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    assert cli.main(["doctor", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "dekko doctor" in out
    assert "map-freshness" in out


def test_cli_doctor_json_smoke(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    assert cli.main(["doctor", "--root", str(tmp_path), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert isinstance(doc, list)
    names = {row["name"] for row in doc}
    assert "map-freshness" in names
    assert "claude-md-policy" in names


def test_cli_doctor_nonexistent_root_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    bogus = tmp_path / "does-not-exist"
    assert cli.main(["doctor", "--root", str(bogus)]) == 2
    assert "not a directory" in capsys.readouterr().err
