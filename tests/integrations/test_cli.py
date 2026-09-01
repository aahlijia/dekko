"""CLI surface tests: flags, output resolution, plugin install."""

import contextlib
import json
import subprocess
import threading
import time
from collections.abc import Iterator
from importlib.metadata import version
from pathlib import Path

import pytest

from dekko import repo_ops
from dekko.analysis import affected
from dekko.integrations import cli
from dekko.render import mapfile
from dekko.storage import filelock
from dekko.core.model import FileMap


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


def test_sanity_smoke(tmp_path: Path) -> None:
    """``dekko sanity`` is wired into the subcommand dispatch (not left
    routing into the legacy flag parser — see ``SUBCOMMANDS``) and
    doesn't crash on a small real repo."""
    (tmp_path / "a.py").write_text(
        "def somefn():\n    return 1\n\n\ndef caller():\n    return somefn()\n"
    )
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    assert cli.main(["sanity", "somefn", "--root", str(tmp_path)]) == 0


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


def test_write_pages_creates_missing_parent_dir(tmp_path: Path) -> None:
    # round-13 spring-boot.md: `_write_pages` used to write the index
    # page (`md_path`) without first re-asserting its parent directory
    # exists, unlike every subsequent page write in the same loop --
    # a `FileNotFoundError` was seen once, right after `.dekko/` had
    # just been removed by `test-repos/reset.sh`. `md_path.parent` not
    # existing at all when `_write_pages` runs is exactly that
    # scenario, reproduced directly rather than relying on a timing
    # race to hit the same code path.
    md_path = tmp_path / ".dekko" / "MAP.md"
    assert not md_path.parent.exists()
    written = repo_ops._write_pages(md_path, [("MAP.md", "# hello\n")])
    assert written == [md_path]
    assert md_path.read_text(encoding="utf-8") == "# hello\n"


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


def test_map_regens_when_doc_version_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # round-15 plan: MAP_DOC_VERSION (the on-disk map.json *format*,
    # e.g. the id-interning change) can bump independently of a
    # package release, so tool_version/spec_hash alone would call an
    # old-format map.json "fresh" forever on an unchanged source tree
    # — the no-op fast path must also check doc_version, not just
    # those two. Reproduced without a real old build: hand-edit the
    # on-disk doc's "version" down by one after a normal run.
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    assert cli.main(["map", str(tmp_path)]) == 0
    capsys.readouterr()

    json_path = tmp_path / ".dekko" / "map.json"
    doc = json.loads(json_path.read_text())
    doc["version"] = doc["version"] - 1
    json_path.write_text(json.dumps(doc))
    stale_mtime = json_path.stat().st_mtime_ns

    assert cli.main(["map", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "unchanged" not in out
    assert json_path.stat().st_mtime_ns != stale_mtime
    assert (
        json.loads(json_path.read_text())["version"] == mapfile.MAP_DOC_VERSION
    )


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


def test_map_scoped_run_refuses_to_overwrite_full_map(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # Round-22 item 7: a subpath-scoped `dekko map DIR SUBPATH` used to
    # silently replace a full-repo map with a 1-file scoped one at the
    # default .dekko/ location, same success shape as an ordinary run.
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("def g() -> int:\n    return 2\n")
    assert cli.main(["map", str(tmp_path)]) == 0
    before = (tmp_path / ".dekko" / "map.json").read_text()
    capsys.readouterr()

    code = cli.main(["map", str(tmp_path), "sub"])

    assert code == 2
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err
    assert (tmp_path / ".dekko" / "map.json").read_text() == before


def test_map_scoped_run_force_overwrites_full_map(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("def g() -> int:\n    return 2\n")
    assert cli.main(["map", str(tmp_path)]) == 0

    assert cli.main(["map", str(tmp_path), "sub", "--force"]) == 0

    doc = json.loads((tmp_path / ".dekko" / "map.json").read_text())
    assert len(doc["files"]) == 1


def test_map_scoped_run_with_explicit_output_never_blocked(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("def g() -> int:\n    return 2\n")
    assert cli.main(["map", str(tmp_path)]) == 0

    out_dir = tmp_path / "scoped-out"
    out_dir.mkdir()
    code = cli.main(["map", str(tmp_path), "sub", "--output", str(out_dir)])

    assert code == 0
    assert (out_dir / "map.json").exists()
    # The original full map at the default location is untouched.
    doc = json.loads((tmp_path / ".dekko" / "map.json").read_text())
    assert len(doc["files"]) == 2


def test_map_scoped_run_over_existing_scoped_map_not_blocked(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("def g() -> int:\n    return 2\n")
    assert cli.main(["map", str(tmp_path), "sub"]) == 0

    # Re-running the same scoped subpath is narrowing nothing -- the
    # existing map was already scoped, so this must not be blocked.
    assert cli.main(["map", str(tmp_path), "sub", "--full"]) == 0


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
    assert repo_ops.regen_map(tmp_path, quiet=True) == 0

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
    out = repo_ops._summary(files, 0, 0, 0, [], [])
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
    md, js = repo_ops.resolve_outputs(tmp_path, None, None)
    assert md == tmp_path / ".dekko" / "MAP.md"
    assert js == tmp_path / ".dekko" / "map.json"


def test_resolve_outputs_explicit_json(tmp_path: Path) -> None:
    md, js = repo_ops.resolve_outputs(tmp_path, None, "custom.json")
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


# --- load_or_regen: inter-process regen locking (round-12 §4.1b) ------


def test_load_or_regen_waits_for_other_process_regen_instead_of_redoing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """When ``filelock.try_regen_lock`` reports another process already
    holds the lock, ``load_or_regen`` must wait for that process's
    regen to land and reuse it, rather than redundantly regenerating
    itself.

    Round-23 §14: this wait used to be entirely silent -- on a large
    repo, blocking here for up to the wait cap with zero output read
    as indistinguishable from a hang. Also confirms the disclosure
    note is printed exactly once, not once per poll iteration (a
    common bug when adding a "print before a loop" note)."""
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    # Invalidate the map so load_or_regen takes the regen branch.
    (tmp_path / "b.py").write_text("def g() -> int:\n    return 2\n")

    monkeypatch.setattr(repo_ops, "_REGEN_LOCK_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(repo_ops, "_REGEN_LOCK_WAIT_CAP", 3.0)

    real_regen_map = repo_ops.regen_map
    regen_calls: list[Path] = []

    def counting_regen_map(
        root: Path, full: bool = False, quiet: bool = True
    ) -> int:
        regen_calls.append(root)
        return real_regen_map(root, full=full, quiet=quiet)

    monkeypatch.setattr(repo_ops, "regen_map", counting_regen_map)

    @contextlib.contextmanager
    def fake_lock_held_by_other_process(root: Path) -> Iterator[bool]:
        # Simulate a different process already holding the lock, which
        # finishes its own regen shortly after this call starts waiting.
        def finish_other_process_regen() -> None:
            time.sleep(0.1)
            real_regen_map(root, quiet=True)

        threading.Thread(
            target=finish_other_process_regen, daemon=True
        ).start()
        yield False

    monkeypatch.setattr(
        filelock, "try_regen_lock", fake_lock_held_by_other_process
    )

    index, code = repo_ops.load_or_regen(tmp_path, no_regen=False)
    assert code == 0
    assert index is not None
    # The caller must never have run its own regen -- the other
    # process's regen was reused once it became fresh.
    assert regen_calls == []

    err = capsys.readouterr().err
    assert err.count("note:") == 1
    assert "already regenerating this repo's map" in err
    assert "waiting up to" in err


def test_load_or_regen_fails_open_after_lock_wait_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """If the lock-wait cap is hit without the other holder's regen
    ever landing, ``load_or_regen`` must fail open and regen locally
    rather than blocking indefinitely.

    Round-23 §14: the fall-through to an uncoordinated local regen
    used to also be silent -- a caller watching stderr saw nothing
    explaining why a second, redundant regen was about to run. Both
    the entry-wait note and the fall-through note must appear."""
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    (tmp_path / "b.py").write_text("def g() -> int:\n    return 2\n")

    monkeypatch.setattr(repo_ops, "_REGEN_LOCK_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(repo_ops, "_REGEN_LOCK_WAIT_CAP", 0.05)

    @contextlib.contextmanager
    def fake_lock_never_released(root: Path) -> Iterator[bool]:
        # The other holder never finishes within the wait cap.
        yield False

    monkeypatch.setattr(filelock, "try_regen_lock", fake_lock_never_released)

    index, code = repo_ops.load_or_regen(tmp_path, no_regen=False)
    assert code == 0
    assert index is not None

    err = capsys.readouterr().err
    assert "already regenerating this repo's map" in err
    assert "gave up waiting" in err
    assert "running an independent regen" in err


def test_load_or_regen_concurrent_callers_regen_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end, using the real ``filelock`` (not monkeypatched):
    several concurrent ``load_or_regen`` calls against the same stale
    root must only trigger one actual regen -- the losers wait and
    reuse the winner's fresh map -- while every caller still gets a
    correct, fresh result."""
    (tmp_path / "a.py").write_text("def f() -> int:\n    return 1\n")
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    (tmp_path / "b.py").write_text("def g() -> int:\n    return 2\n")

    monkeypatch.setattr(repo_ops, "_REGEN_LOCK_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(repo_ops, "_REGEN_LOCK_WAIT_CAP", 10.0)

    real_regen_map = repo_ops.regen_map
    regen_calls: list[Path] = []
    calls_lock = threading.Lock()

    def slow_regen_map(
        root: Path, full: bool = False, quiet: bool = True
    ) -> int:
        with calls_lock:
            regen_calls.append(root)
        # Widen the race window so every thread has a real chance to
        # contend for the lock before the winner finishes.
        time.sleep(0.3)
        return real_regen_map(root, full=full, quiet=quiet)

    monkeypatch.setattr(repo_ops, "regen_map", slow_regen_map)

    results: list[tuple] = []
    results_lock = threading.Lock()

    def worker() -> None:
        result = repo_ops.load_or_regen(tmp_path, no_regen=False)
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15.0)

    assert len(regen_calls) == 1
    assert len(results) == 3
    assert all(code == 0 and index is not None for index, code in results)
