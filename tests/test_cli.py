"""CLI surface tests: flags, output resolution, plugin install."""

import json
import subprocess
from importlib.metadata import version
from pathlib import Path

import pytest

from dekko import affected, cli
from dekko.model import FileMap


def test_version_flag(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert version("dekko") in out


def test_bare_invocation_prints_help(capsys: pytest.CaptureFixture) -> None:
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "--map" in out
    assert "--claude-install" in out


def test_affected_budget_defaults_to_affected_default_budget() -> None:
    # Round-08 eval: `dekko affected` had no default --budget at all
    # (confirmed via --help) and returned ~124K uncapped tokens for one
    # commit on a large repo. It should be budgeted by default like
    # every other whole-graph read command (search/summary/workset).
    parser = cli.build_subcommand_parser()
    args = parser.parse_args(["affected"])
    assert args.budget == affected.DEFAULT_BUDGET


def test_map_writes_outputs_to_target_dir(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    assert cli.main(["--map", str(tmp_path), "--quiet"]) == 0
    assert (tmp_path / ".dekko" / "MAP.md").is_file()
    assert (tmp_path / ".dekko" / "map.json").is_file()


def test_map_rejects_missing_dir(tmp_path: Path) -> None:
    assert cli.main(["--map", str(tmp_path / "nope")]) == 2


def test_map_second_run_is_noop_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # Performance #1: once the incremental cache determines nothing
    # needs re-parsing, an unchanged repo should not re-serialize and
    # rewrite MAP.md/map.json on every invocation — verified here by
    # asserting the files' mtimes are untouched by the second run.
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    assert cli.main(["map", str(tmp_path)]) == 0
    capsys.readouterr()

    md_path = tmp_path / ".dekko" / "MAP.md"
    json_path = tmp_path / ".dekko" / "map.json"
    md_before = md_path.stat().st_mtime_ns
    json_before = json_path.stat().st_mtime_ns

    assert cli.main(["map", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "unchanged" in out
    assert "nothing written" in out
    assert md_path.stat().st_mtime_ns == md_before
    assert json_path.stat().st_mtime_ns == json_before


def test_map_noop_summary_suppressed_by_quiet(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    capsys.readouterr()
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    assert capsys.readouterr().out == ""


def test_map_full_bypasses_noop_fast_path(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    assert cli.main(["map", str(tmp_path)]) == 0
    capsys.readouterr()
    assert cli.main(["map", str(tmp_path), "--full"]) == 0
    out = capsys.readouterr().out
    assert "unchanged" not in out
    assert "mapped 1 files" in out


def test_map_added_file_forces_a_real_rewrite(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # cache.parsed == 0 alone would be true here too (the new file
    # still needs first-time extraction, so this isn't the sharpest
    # case, but a *removed* file is the sharp one: discovery simply
    # stops seeing it, so cache.parsed stays 0 even though the map
    # must shrink). Exercise the add case for the common path and the
    # remove case below for the one cache.parsed can't catch alone.
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    assert cli.main(["map", str(tmp_path)]) == 0
    capsys.readouterr()
    (tmp_path / "b.py").write_text("def g() -> int:\n    return 2\n")
    assert cli.main(["map", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "unchanged" not in out
    assert "mapped 2 files" in out


def test_map_removed_file_forces_a_real_rewrite(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    (tmp_path / "b.py").write_text("def g() -> int:\n    return 2\n")
    assert cli.main(["map", str(tmp_path)]) == 0
    capsys.readouterr()
    (tmp_path / "b.py").unlink()
    assert cli.main(["map", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "unchanged" not in out
    assert "mapped 1 files" in out


def test_map_exclude_persists_to_dekkoignore(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / "widget.astro").write_text("---\n---\n")
    ignore_path = tmp_path / ".dekko" / ".dekkoignore"

    assert cli.main(["map", str(tmp_path), "--exclude", "*.astro"]) == 0
    assert ignore_path.read_text().splitlines() == ["*.astro"]

    assert cli.main(["map", str(tmp_path), "--exclude", "*.astro"]) == 0
    assert ignore_path.read_text().splitlines() == ["*.astro"]


def test_bare_map_after_exclude_run_honors_persisted_pattern(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / "widget.astro").write_text("---\n---\n")

    assert cli.main(["map", str(tmp_path), "--exclude", "*.astro"]) == 0
    doc = json.loads((tmp_path / ".dekko" / "map.json").read_text())
    assert "widget.astro" not in doc["provenance"]["files"]

    (tmp_path / "b.py").write_text("def g():\n    return 2\n")
    assert cli.main(["map", str(tmp_path)]) == 0
    doc = json.loads((tmp_path / ".dekko" / "map.json").read_text())
    assert "widget.astro" not in doc["provenance"]["files"]
    assert "b.py" in doc["provenance"]["files"]


def test_regen_map_does_not_re_persist_dekkoignore(
    tmp_path: Path,
) -> None:
    # regen_map() reconstructs a synthetic namespace with `exclude` set
    # from provenance (non-empty here) and re-runs run_map — it must
    # not re-trigger persistence on every auto-regen/--if-stale cycle.
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / "widget.astro").write_text("---\n---\n")
    assert cli.main(["map", str(tmp_path), "--exclude", "*.astro"]) == 0

    ignore_path = tmp_path / ".dekko" / ".dekkoignore"
    before = ignore_path.read_text()
    before_mtime = ignore_path.stat().st_mtime_ns

    (tmp_path / "a.py").write_text("def f():\n    return 2\n")
    assert cli.regen_map(tmp_path, quiet=True) == 0

    assert ignore_path.read_text() == before
    assert ignore_path.stat().st_mtime_ns == before_mtime


def test_map_summary_reports_unsupported_language(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # A confirmed-unsupported extension (.astro) must show up in the
    # build summary instead of being silently dropped — the primary
    # visibility fix for the 2026-07-31 eval's most severe finding
    # (a partially mapped repo with no warning anywhere).
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / "Card.astro").write_text("---\nconst x = 1;\n---\n")
    assert cli.main(["--map", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "mapped 1 files" in out
    assert "skipped: no parser (astro) 1" in out


def test_map_summary_counts_variable_symbols(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # Top-level "export const" data bindings are indexed as
    # kind="variable" symbols and must be counted in the run summary,
    # not silently dropped from the printed totals.
    (tmp_path / "data.ts").write_text("export const jobs = [1, 2, 3];\n")
    assert cli.main(["--map", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "0 functions/methods, 0 types, 1 variables" in out


def test_summary_separates_missing_grammar_from_real_parse_errors() -> None:
    """Round-12 master report §3.10/§3.16: a missing *optional*
    grammar (``pip install dekko[all]``) used to share one alarming
    "parse error N" bucket with genuine parse failures in the run
    summary. They must now be broken out into distinct buckets, built
    directly against synthetic ``FileMap``s (no real Tier-2 grammar
    dependency needed to reproduce the message shape)."""
    files = [
        FileMap(
            path="gen/file.kt",
            language="kotlin",
            error="grammar 'kotlin' is not in the offline Tier-1 set; "
            "install the extras with `pip install dekko[all]`",
        ),
        FileMap(
            path="src/broken.py",
            language="python",
            error="[Errno 13] Permission denied: 'src/broken.py'",
        ),
        FileMap(path="src/a.py", language="python"),
    ]
    out = cli._summary(files, 0, 0, 0, [], [])
    assert "no grammar installed 1" in out
    assert "parse error 1" in out


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


def test_map_writes_provenance_sidecar_at_canonical_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    assert cli.main(["--map", str(tmp_path), "--quiet"]) == 0
    sidecar = tmp_path / ".dekko" / "provenance.json"
    assert sidecar.is_file()
    doc = json.loads(sidecar.read_text())
    map_doc = json.loads((tmp_path / ".dekko" / "map.json").read_text())
    assert doc["provenance"] == map_doc["provenance"]
    assert doc["map_stat"]


def test_custom_output_does_not_write_canonical_sidecar(
    tmp_path: Path,
) -> None:
    # A --output run doesn't touch the canonical .dekko/map.json, so
    # writing .dekko/provenance.json alongside a map written somewhere
    # else would desync it from whatever (if anything) actually lives
    # at the canonical path status/query/etc. always read from.
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    assert (
        cli.main(["--map", str(tmp_path), "--output", str(out_dir), "--quiet"])
        == 0
    )
    assert (out_dir / "map.json").is_file()
    assert not (tmp_path / ".dekko" / "provenance.json").exists()


def test_resolve_outputs_defaults(tmp_path: Path) -> None:
    md, js = cli.resolve_outputs(tmp_path, None, None)
    assert md == tmp_path / ".dekko" / "MAP.md"
    assert js == tmp_path / ".dekko" / "map.json"


def test_resolve_outputs_explicit_json(tmp_path: Path) -> None:
    md, js = cli.resolve_outputs(tmp_path, None, "custom.json")
    assert md == tmp_path / ".dekko" / "MAP.md"
    assert js == Path("custom.json")


def test_claude_install_requires_claude_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    assert cli.claude_install() == 1
    assert "claude" in capsys.readouterr().err


def test_claude_uninstall_requires_claude_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    assert cli.claude_uninstall() == 1
    assert "claude" in capsys.readouterr().err


def test_claude_uninstall_removes_plugin_and_marketplace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/claude")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cli, "_run_subprocess", fake_run)
    assert cli.claude_uninstall() == 0
    assert ["/usr/bin/claude", "plugin", "uninstall", "dekko@dekko"] in calls
    assert [
        "/usr/bin/claude",
        "plugin",
        "marketplace",
        "remove",
        "dekko",
    ] in calls


def test_claude_uninstall_tolerates_missing_plugin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/claude")

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 1, "", "not found")

    monkeypatch.setattr(cli, "_run_subprocess", fake_run)
    assert cli.claude_uninstall() == 0
    assert "already removed?" in capsys.readouterr().err


def test_mcp_uninstall_requires_claude_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    assert cli.mcp_uninstall() == 1
    assert "claude" in capsys.readouterr().err


def test_claude_install_dry_run_prints_commands_without_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/claude")
    plugin_dir = tmp_path / "_plugin"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    monkeypatch.setattr(cli, "_pkg_files", lambda _pkg: tmp_path)

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run_subprocess", lambda cmd: calls.append(cmd))

    assert cli.claude_install(dry_run=True) == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "would run" in out
    assert "plugin marketplace add" in out
    assert "plugin install dekko@dekko" in out


def test_claude_uninstall_dry_run_prints_commands_without_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/claude")

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run_subprocess", lambda cmd: calls.append(cmd))

    assert cli.claude_uninstall(dry_run=True) == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "would run" in out
    assert "plugin uninstall dekko@dekko" in out
    assert "plugin marketplace remove dekko" in out


def test_claude_install_no_dry_run_runs_as_before(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/claude")
    plugin_dir = tmp_path / "_plugin"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    monkeypatch.setattr(cli, "_pkg_files", lambda _pkg: tmp_path)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cli, "_run_subprocess", fake_run)

    assert cli.claude_install() == 0
    assert [
        "/usr/bin/claude",
        "plugin",
        "marketplace",
        "add",
        str(plugin_dir),
    ] in calls
    assert [
        "/usr/bin/claude",
        "plugin",
        "install",
        "dekko@dekko",
    ] in calls
    assert "installed" in capsys.readouterr().out


def test_legacy_parser_dry_run_wired_to_claude_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/claude")
    plugin_dir = tmp_path / "_plugin"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    monkeypatch.setattr(cli, "_pkg_files", lambda _pkg: tmp_path)

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run_subprocess", lambda cmd: calls.append(cmd))

    assert cli.main(["--claude-install", "--dry-run"]) == 0
    assert calls == []
    assert "would run" in capsys.readouterr().out


def test_mcp_uninstall_removes_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/claude")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cli, "_run_subprocess", fake_run)
    assert cli.mcp_uninstall() == 0
    assert ["/usr/bin/claude", "mcp", "remove", "dekko"] in calls


def test_mcp_uninstall_tolerates_missing_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/claude")

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 1, "", "not found")

    monkeypatch.setattr(cli, "_run_subprocess", fake_run)
    assert cli.mcp_uninstall() == 0
    assert "already removed?" in capsys.readouterr().err


def test_cline_install_flag_dispatches_with_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple] = []

    def fake_install(config: Path | None, scope: str, *, force: bool) -> int:
        calls.append((config, scope, force))
        return 0

    monkeypatch.setattr(cli.cline_mod, "install", fake_install)
    assert cli.main(["--cline-install"]) == 0
    assert calls == [(None, "vscode", False)]


def test_cline_install_flag_passes_scope_config_force(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple] = []

    def fake_install(config: Path | None, scope: str, *, force: bool) -> int:
        calls.append((config, scope, force))
        return 0

    monkeypatch.setattr(cli.cline_mod, "install", fake_install)
    cfg = tmp_path / "settings.json"
    assert (
        cli.main(
            [
                "--cline-install",
                "--cline-scope",
                "global",
                "--cline-config",
                str(cfg),
                "--cline-force",
            ]
        )
        == 0
    )
    assert calls == [(cfg, "global", True)]


def test_cline_uninstall_flag_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []

    def fake_uninstall(config: Path | None, scope: str, *, force: bool) -> int:
        calls.append((config, scope, force))
        return 0

    monkeypatch.setattr(cli.cline_mod, "uninstall", fake_uninstall)
    assert cli.main(["--cline-uninstall"]) == 0
    assert calls == [(None, "vscode", False)]


def test_claude_invoked_by_resolved_path_not_bare_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """argv[0] is the path shutil.which resolved (the Windows-shim fix)."""
    resolved = "/opt/claude/bin/claude.cmd"
    monkeypatch.setattr(cli.shutil, "which", lambda _name: resolved)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cli, "_run_subprocess", fake_run)
    assert cli.mcp_install() == 0
    assert calls and all(cmd[0] == resolved for cmd in calls)
