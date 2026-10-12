"""``dekko install|uninstall <target>`` and its legacy flag aliases.

The subcommand family replaced one ``--<client>-install`` flag per
client. The old flags stay as aliases, so for every target the new
command and its alias must reach the same handler with the same
arguments; that is what keeps the two from drifting apart.
"""

from pathlib import Path

import pytest

from dekko.integrations import claude_md as claude_md_mod
from dekko.integrations import cli
from dekko.integrations import cline as cline_mod

# (id, new argv, legacy argv, owner of the handler, handler name). A
# ``{tmp}`` in either argv is replaced with the test's tmp_path.
ALIAS_CASES = [
    (
        "install-claude",
        ["install", "claude"],
        ["--claude-install"],
        cli,
        "claude_install",
    ),
    (
        "install-claude-dry-run",
        ["install", "claude", "--dry-run"],
        ["--claude-install", "--dry-run"],
        cli,
        "claude_install",
    ),
    (
        "uninstall-claude",
        ["uninstall", "claude"],
        ["--claude-uninstall"],
        cli,
        "claude_uninstall",
    ),
    (
        "install-claude-mcp-only",
        ["install", "claude", "--mcp-only"],
        ["--mcp-install"],
        cli,
        "mcp_install",
    ),
    (
        "uninstall-claude-mcp-only-dry-run",
        ["uninstall", "claude", "--mcp-only", "--dry-run"],
        ["--mcp-uninstall", "--dry-run"],
        cli,
        "mcp_uninstall",
    ),
    (
        "install-cline-defaults",
        ["install", "cline"],
        ["--cline-install"],
        cline_mod,
        "install",
    ),
    (
        "install-cline-options",
        [
            "install",
            "cline",
            "--scope",
            "global",
            "--config",
            "{tmp}/cline.json",
            "--force",
        ],
        [
            "--cline-install",
            "--cline-scope",
            "global",
            "--cline-config",
            "{tmp}/cline.json",
            "--cline-force",
        ],
        cline_mod,
        "install",
    ),
    (
        "uninstall-cline",
        ["uninstall", "cline"],
        ["--cline-uninstall"],
        cline_mod,
        "uninstall",
    ),
    (
        "install-claude-md",
        ["install", "claude-md", "--root", "{tmp}"],
        ["--claude-md-install", "--root", "{tmp}"],
        claude_md_mod,
        "install",
    ),
    (
        "uninstall-claude-md",
        ["uninstall", "claude-md", "--root", "{tmp}"],
        ["--claude-md-uninstall", "--root", "{tmp}"],
        claude_md_mod,
        "uninstall",
    ),
]


def _record_calls(
    monkeypatch: pytest.MonkeyPatch,
    owner: object,
    name: str,
) -> list[tuple[tuple, dict]]:
    """Replace ``owner.name`` with a stub that records each call."""
    calls: list[tuple[tuple, dict]] = []

    def fake(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(owner, name, fake)
    return calls


def _expand(argv: list[str], tmp_path: Path) -> list[str]:
    """Substitute the ``{tmp}`` placeholder in an argv list."""
    return [arg.replace("{tmp}", str(tmp_path)) for arg in argv]


@pytest.mark.parametrize(
    ("new_argv", "legacy_argv", "owner", "name"),
    [case[1:] for case in ALIAS_CASES],
    ids=[case[0] for case in ALIAS_CASES],
)
def test_new_command_and_legacy_alias_call_the_same_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    new_argv: list[str],
    legacy_argv: list[str],
    owner: object,
    name: str,
) -> None:
    calls = _record_calls(monkeypatch, owner, name)

    assert cli.main(_expand(new_argv, tmp_path)) == 0
    assert cli.main(_expand(legacy_argv, tmp_path)) == 0

    assert len(calls) == 2
    assert calls[0] == calls[1]


@pytest.mark.parametrize(
    "argv",
    [
        ["install", "cline", "--dry-run"],
        ["uninstall", "cline", "--dry-run"],
        ["install", "claude-md", "--dry-run"],
        ["uninstall", "claude-md", "--dry-run"],
    ],
)
def test_file_editing_targets_reject_dry_run(argv: list[str]) -> None:
    """A target that edits files has no preview, so it refuses one."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)

    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [["install"], ["uninstall"], ["install", "nonexistent"]],
)
def test_missing_or_unknown_target_exits_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)

    assert excinfo.value.code == 2


def test_install_help_lists_every_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["install", "--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for target in ("claude", "cline", "claude-md"):
        assert target in out


def test_claude_md_round_trip_through_new_commands(tmp_path: Path) -> None:
    """Install then uninstall leaves no CLAUDE.md behind."""
    path = tmp_path / "CLAUDE.md"

    assert cli.main(["install", "claude-md", "--root", str(tmp_path)]) == 0
    assert "dekko:usage-policy:start" in path.read_text(encoding="utf-8")

    assert cli.main(["uninstall", "claude-md", "--root", str(tmp_path)]) == 0
    assert not path.exists()
