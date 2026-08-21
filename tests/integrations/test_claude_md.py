"""Tier 1: the idempotent dekko usage-policy block in CLAUDE.md."""

from pathlib import Path

from dekko.integrations import claude_md, cli

_START = "<!-- dekko:usage-policy:start -->"
_END = "<!-- dekko:usage-policy:end -->"


def test_install_creates_claude_md(tmp_path: Path) -> None:
    assert claude_md.install(tmp_path) == 0
    text = (tmp_path / "CLAUDE.md").read_text()
    assert _START in text and _END in text
    assert "search_code" in text
    assert "outline" in text


def test_install_appends_to_existing_content(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text("# Project notes\n\nSome existing content.\n")
    claude_md.install(tmp_path)
    text = path.read_text()
    assert "# Project notes" in text
    assert "Some existing content." in text
    assert _START in text


def test_install_is_idempotent(tmp_path: Path) -> None:
    claude_md.install(tmp_path)
    claude_md.install(tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text()
    assert text.count(_START) == 1
    assert text.count(_END) == 1


def test_install_replaces_stale_block_in_place(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text(
        f"# Notes\n\n{_START}\nstale content\n{_END}\n\n# More notes after\n"
    )
    claude_md.install(tmp_path)
    text = path.read_text()
    assert "stale content" not in text
    assert "search_code" in text
    assert "# Notes" in text
    assert "# More notes after" in text


def test_uninstall_removes_only_the_block(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text("# Before\n\nkeep me\n")
    claude_md.install(tmp_path)
    claude_md.uninstall(tmp_path)
    text = path.read_text()
    assert _START not in text
    assert "# Before" in text
    assert "keep me" in text


def test_uninstall_missing_file_is_a_noop(tmp_path: Path) -> None:
    assert claude_md.uninstall(tmp_path) == 0


def test_uninstall_no_block_present_is_a_noop(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text("# Just some notes\n")
    claude_md.uninstall(tmp_path)
    assert path.read_text() == "# Just some notes\n"


def test_install_then_uninstall_round_trips_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    original = "# Project\n\nHand-written notes.\n"
    path.write_text(original)
    claude_md.install(tmp_path)
    claude_md.uninstall(tmp_path)
    assert path.read_text() == original


def test_uninstall_deletes_file_it_created_from_nothing(
    tmp_path: Path,
) -> None:
    # round-19 claude-buddy finding: uninstall previously left a 0-byte
    # CLAUDE.md instead of deleting it when install had created the file
    # from nothing -- a full install/uninstall round trip should restore
    # the pre-install state exactly (no file at all).
    claude_md.install(tmp_path)
    assert (tmp_path / "CLAUDE.md").is_file()
    claude_md.uninstall(tmp_path)
    assert not (tmp_path / "CLAUDE.md").exists()


def test_uninstall_keeps_file_when_other_content_remains(
    tmp_path: Path,
) -> None:
    # Regression guard: the fix only deletes the file when the remainder
    # is genuinely empty, not whenever uninstall runs.
    path = tmp_path / "CLAUDE.md"
    path.write_text("# My project\n\nSome notes.\n")
    claude_md.install(tmp_path)
    claude_md.uninstall(tmp_path)
    text = path.read_text()
    assert "My project" in text
    assert _START not in text


def test_cli_claude_md_install_smoke(tmp_path: Path) -> None:
    code = cli.main(["--claude-md-install", "--root", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "CLAUDE.md").is_file()


def test_cli_claude_md_uninstall_smoke(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text("# Notes\n\nKeep me.\n")
    cli.main(["--claude-md-install", "--root", str(tmp_path)])
    code = cli.main(["--claude-md-uninstall", "--root", str(tmp_path)])
    assert code == 0
    text = path.read_text()
    assert _START not in text
