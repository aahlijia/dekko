"""The idempotent dekko usage-policy block in CLAUDE.md and AGENTS.md."""

from pathlib import Path

import pytest

from dekko.integrations import cli, policy_md

_START = "<!-- dekko:usage-policy:start -->"
_END = "<!-- dekko:usage-policy:end -->"

_TARGETS = pytest.mark.parametrize(
    ("target", "filename"), [("claude", "CLAUDE.md"), ("agents", "AGENTS.md")]
)


@_TARGETS
def test_install_creates_the_file(
    tmp_path: Path, target: str, filename: str
) -> None:
    assert policy_md.install(tmp_path, target) == 0
    text = (tmp_path / filename).read_text()
    assert _START in text and _END in text
    assert "search_code" in text
    assert "outline" in text


def test_install_defaults_to_claude_md(tmp_path: Path) -> None:
    policy_md.install(tmp_path)

    assert (tmp_path / "CLAUDE.md").is_file()
    assert not (tmp_path / "AGENTS.md").exists()


@_TARGETS
def test_install_appends_to_existing_content(
    tmp_path: Path, target: str, filename: str
) -> None:
    path = tmp_path / filename
    path.write_text("# Project notes\n\nSome existing content.\n")
    policy_md.install(tmp_path, target)
    text = path.read_text()
    assert "# Project notes" in text
    assert "Some existing content." in text
    assert _START in text


@_TARGETS
def test_install_is_idempotent(
    tmp_path: Path, target: str, filename: str
) -> None:
    policy_md.install(tmp_path, target)
    policy_md.install(tmp_path, target)
    text = (tmp_path / filename).read_text()
    assert text.count(_START) == 1
    assert text.count(_END) == 1


@_TARGETS
def test_install_replaces_stale_block_in_place(
    tmp_path: Path, target: str, filename: str
) -> None:
    path = tmp_path / filename
    path.write_text(
        f"# Notes\n\n{_START}\nstale content\n{_END}\n\n# More notes after\n"
    )
    policy_md.install(tmp_path, target)
    text = path.read_text()
    assert "stale content" not in text
    assert "search_code" in text
    assert "# Notes" in text
    assert "# More notes after" in text


@_TARGETS
def test_uninstall_removes_only_the_block(
    tmp_path: Path, target: str, filename: str
) -> None:
    path = tmp_path / filename
    path.write_text("# Before\n\nkeep me\n")
    policy_md.install(tmp_path, target)
    policy_md.uninstall(tmp_path, target)
    text = path.read_text()
    assert _START not in text
    assert "# Before" in text
    assert "keep me" in text


@_TARGETS
def test_uninstall_missing_file_is_a_noop(
    tmp_path: Path, target: str, filename: str
) -> None:
    assert policy_md.uninstall(tmp_path, target) == 0
    assert not (tmp_path / filename).exists()


@_TARGETS
def test_uninstall_no_block_present_is_a_noop(
    tmp_path: Path, target: str, filename: str
) -> None:
    path = tmp_path / filename
    path.write_text("# Just some notes\n")
    policy_md.uninstall(tmp_path, target)
    assert path.read_text() == "# Just some notes\n"


@_TARGETS
def test_install_then_uninstall_round_trips_cleanly(
    tmp_path: Path, target: str, filename: str
) -> None:
    path = tmp_path / filename
    original = "# Project\n\nHand-written notes.\n"
    path.write_text(original)
    policy_md.install(tmp_path, target)
    policy_md.uninstall(tmp_path, target)
    assert path.read_text() == original


@_TARGETS
def test_uninstall_deletes_file_it_created_from_nothing(
    tmp_path: Path, target: str, filename: str
) -> None:
    # A full install/uninstall round trip restores the pre-install
    # state exactly: no file at all, not a 0-byte one.
    policy_md.install(tmp_path, target)
    assert (tmp_path / filename).is_file()
    policy_md.uninstall(tmp_path, target)
    assert not (tmp_path / filename).exists()


@_TARGETS
def test_uninstall_keeps_file_when_other_content_remains(
    tmp_path: Path, target: str, filename: str
) -> None:
    path = tmp_path / filename
    path.write_text("# My project\n\nSome notes.\n")
    policy_md.install(tmp_path, target)
    policy_md.uninstall(tmp_path, target)
    text = path.read_text()
    assert "My project" in text
    assert _START not in text


def test_the_two_targets_are_independent(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# Claude notes\n")
    policy_md.install(tmp_path, "agents")

    assert (tmp_path / "CLAUDE.md").read_text() == "# Claude notes\n"
    policy_md.uninstall(tmp_path, "claude")
    assert _START in (tmp_path / "AGENTS.md").read_text()


# --- a symlink between the two files -------------------------------------


@pytest.mark.parametrize(
    ("link", "real"), [("CLAUDE.md", "AGENTS.md"), ("AGENTS.md", "CLAUDE.md")]
)
def test_symlinked_files_get_one_block_and_keep_the_link(
    tmp_path: Path, link: str, real: str
) -> None:
    (tmp_path / real).write_text("# Shared notes\n")
    (tmp_path / link).symlink_to(real)

    policy_md.install(tmp_path, "claude")
    policy_md.install(tmp_path, "agents")

    assert (tmp_path / link).is_symlink()
    text = (tmp_path / real).read_text()
    assert text.count(_START) == 1
    assert text.startswith("# Shared notes\n")


@pytest.mark.parametrize("via", ["claude", "agents"])
def test_uninstall_through_a_symlink_restores_the_shared_file(
    tmp_path: Path, via: str
) -> None:
    original = "# Shared notes\n"
    (tmp_path / "AGENTS.md").write_text(original)
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    policy_md.install(tmp_path, "claude")

    policy_md.uninstall(tmp_path, via)

    assert (tmp_path / "CLAUDE.md").is_symlink()
    assert (tmp_path / "AGENTS.md").read_text() == original


def test_uninstall_through_a_symlink_deletes_the_file_it_points_at(
    tmp_path: Path,
) -> None:
    # The link dangled before install, so install created AGENTS.md
    # through it; removing just the link would leave the block behind.
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    policy_md.install(tmp_path, "claude")
    assert (tmp_path / "AGENTS.md").is_file()

    policy_md.uninstall(tmp_path, "claude")

    assert not (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / "CLAUDE.md").is_symlink()


# --- what Codex will and won't see ----------------------------------------


def test_no_warning_for_a_small_agents_md(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    policy_md.install(tmp_path, "agents")

    assert capsys.readouterr().err == ""
    assert policy_md.agents_md_overflow(tmp_path) is None
    assert policy_md.agents_md_override(tmp_path) is None


def test_warns_when_agents_md_exceeds_codex_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / "AGENTS.md").write_text("x" * policy_md.AGENTS_MD_MAX_BYTES)

    assert policy_md.install(tmp_path, "agents") == 0

    size = (tmp_path / "AGENTS.md").stat().st_size
    assert policy_md.agents_md_overflow(tmp_path) == size
    err = capsys.readouterr().err
    assert f"is {size} bytes" in err
    assert "cuts the end of the file" in err


def test_a_file_exactly_at_the_limit_is_not_over(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("x" * policy_md.AGENTS_MD_MAX_BYTES)

    assert policy_md.agents_md_overflow(tmp_path) is None


def test_warns_when_an_override_shadows_agents_md(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    override = tmp_path / "AGENTS.override.md"
    override.write_text("# Local override\n")

    assert policy_md.install(tmp_path, "agents") == 0

    assert policy_md.agents_md_override(tmp_path) == override
    err = capsys.readouterr().err
    assert "AGENTS.override.md exists" in err
    assert _START in (tmp_path / "AGENTS.md").read_text()


def test_claude_target_never_warns_about_codex(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / "AGENTS.override.md").write_text("# Local override\n")

    policy_md.install(tmp_path, "claude")

    assert capsys.readouterr().err == ""


# --- the CLI entrypoints --------------------------------------------------


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


def test_cli_agents_md_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    original = "# Agent notes\n"
    path.write_text(original)

    assert cli.main(["install", "agents-md", "--root", str(tmp_path)]) == 0
    assert _START in path.read_text()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert cli.main(["uninstall", "agents-md", "--root", str(tmp_path)]) == 0
    assert path.read_text() == original
