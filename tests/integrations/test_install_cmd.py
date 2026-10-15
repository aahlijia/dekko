"""``dekko install`` / ``dekko uninstall``: targets, aliases, refusals."""

import argparse
from pathlib import Path
from typing import Any

import pytest

from dekko.daemon import daemon as daemon_mod
from dekko.integrations import cli

Calls = list[tuple[str, tuple, dict]]

_HANDLERS = (
    (cli, "claude_install"),
    (cli, "claude_uninstall"),
    (cli, "mcp_install"),
    (cli, "mcp_uninstall"),
    (cli, "codex_install"),
    (cli, "codex_uninstall"),
    (cli, "codex_mcp_install"),
    (cli, "codex_mcp_uninstall"),
    (cli.cline_mod, "install"),
    (cli.cline_mod, "uninstall"),
    (cli.policy_md_mod, "install"),
    (cli.policy_md_mod, "uninstall"),
)


def _label(owner: Any, name: str) -> str:
    """A handler's name, qualified by its module unless it's ``cli``'s."""
    if owner is cli:
        return name

    return f"{owner.__name__.rsplit('.', 1)[-1]}.{name}"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Replace every install handler with a recorder that returns 0."""
    recorded: Calls = []
    for owner, name in _HANDLERS:
        label = _label(owner, name)

        def record(*args: Any, _label: str = label, **kwargs: Any) -> int:
            recorded.append((_label, args, kwargs))
            return 0

        monkeypatch.setattr(owner, name, record)
    return recorded


_ALIASES = [
    (["install", "claude"], ["--claude-install"]),
    (["uninstall", "claude"], ["--claude-uninstall"]),
    (["install", "claude", "--dry-run"], ["--claude-install", "--dry-run"]),
    (
        ["uninstall", "claude", "--dry-run"],
        ["--claude-uninstall", "--dry-run"],
    ),
    (["install", "claude", "--mcp-only"], ["--mcp-install"]),
    (["uninstall", "claude", "--mcp-only"], ["--mcp-uninstall"]),
    (
        ["install", "claude", "--mcp-only", "--dry-run"],
        ["--mcp-install", "--dry-run"],
    ),
    (["install", "cline"], ["--cline-install"]),
    (["uninstall", "cline"], ["--cline-uninstall"]),
    (
        [
            "install",
            "cline",
            "--scope",
            "global",
            "--config",
            "{root}/settings.json",
            "--force",
        ],
        [
            "--cline-install",
            "--cline-scope",
            "global",
            "--cline-config",
            "{root}/settings.json",
            "--cline-force",
        ],
    ),
    (
        ["uninstall", "cline", "--scope", "global", "--force"],
        ["--cline-uninstall", "--cline-scope", "global", "--cline-force"],
    ),
    (
        ["install", "claude-md", "--root", "{root}"],
        ["--claude-md-install", "--root", "{root}"],
    ),
    (
        ["uninstall", "claude-md", "--root", "{root}"],
        ["--claude-md-uninstall", "--root", "{root}"],
    ),
]


def _fill(argv: list[str], root: Path) -> list[str]:
    """Substitute ``{root}`` in an argv template."""
    return [a.replace("{root}", str(root)) for a in argv]


@pytest.mark.parametrize(("command", "legacy"), _ALIASES)
def test_install_target_and_its_legacy_flag_call_the_same_handler(
    calls: Calls,
    tmp_path: Path,
    command: list[str],
    legacy: list[str],
) -> None:
    assert cli.main(_fill(command, tmp_path)) == 0
    via_command = list(calls)
    calls.clear()

    assert cli.main(_fill(legacy, tmp_path)) == 0

    assert len(via_command) == 1
    assert calls == via_command


@pytest.mark.parametrize(
    ("argv", "handler", "dry_run"),
    [
        (["install", "codex"], "codex_install", False),
        (["uninstall", "codex"], "codex_uninstall", False),
        (["install", "codex", "--dry-run"], "codex_install", True),
        (["uninstall", "codex", "--dry-run"], "codex_uninstall", True),
        (["install", "codex", "--mcp-only"], "codex_mcp_install", False),
        (
            ["uninstall", "codex", "--mcp-only"],
            "codex_mcp_uninstall",
            False,
        ),
        (
            ["install", "codex", "--mcp-only", "--dry-run"],
            "codex_mcp_install",
            True,
        ),
    ],
)
def test_codex_target_calls_its_handler(
    calls: Calls,
    argv: list[str],
    handler: str,
    dry_run: bool,
) -> None:
    assert cli.main(argv) == 0

    assert calls == [(handler, (), {"dry_run": dry_run})]


@pytest.mark.parametrize("verb", ["install", "uninstall"])
@pytest.mark.parametrize("target", ["cline", "claude-md", "agents-md"])
def test_dry_run_is_refused_on_a_file_editing_target(
    calls: Calls,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    verb: str,
    target: str,
) -> None:
    argv = [verb, target, "--dry-run"]
    if target.endswith("-md"):
        argv += ["--root", str(tmp_path)]

    assert cli.main(argv) == 2
    assert calls == []
    err = capsys.readouterr().err
    assert "--dry-run works with 'claude', 'codex' only" in err
    assert f"'{target}' edits a file" in err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("verb", ["install", "uninstall"])
@pytest.mark.parametrize("extra", [[], ["zed"]])
def test_a_missing_or_unknown_target_exits_2_with_the_list(
    calls: Calls,
    capsys: pytest.CaptureFixture,
    verb: str,
    extra: list[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([verb, *extra])

    assert exc.value.code == 2
    assert calls == []
    err = capsys.readouterr().err
    for target in cli.INSTALL_TARGETS:
        assert target.name in err
    assert "unknown command" not in err


@pytest.mark.parametrize("verb", ["install", "uninstall"])
def test_help_lists_every_target(
    capsys: pytest.CaptureFixture, verb: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([verb, "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    for target in cli.INSTALL_TARGETS:
        assert target.name in out


@pytest.mark.parametrize(
    ("target", "shown"),
    [
        ("claude", True),
        ("codex", True),
        ("cline", False),
        ("claude-md", False),
        ("agents-md", False),
    ],
)
def test_target_help_mentions_dry_run_only_when_previewable(
    capsys: pytest.CaptureFixture, target: str, shown: bool
) -> None:
    with pytest.raises(SystemExit):
        cli.main(["install", target, "--help"])

    assert ("--dry-run" in capsys.readouterr().out) is shown


@pytest.mark.parametrize(
    ("target", "file_key"), [("claude-md", "claude"), ("agents-md", "agents")]
)
@pytest.mark.parametrize("verb", ["install", "uninstall"])
def test_policy_targets_pass_their_file_to_the_handler(
    calls: Calls, tmp_path: Path, verb: str, target: str, file_key: str
) -> None:
    assert cli.main([verb, target, "--root", str(tmp_path)]) == 0

    assert calls == [(f"policy_md.{verb}", (tmp_path.resolve(), file_key), {})]


def test_install_and_uninstall_never_route_through_the_daemon(
    tmp_path: Path,
) -> None:
    assert "install" in cli.SUBCOMMANDS
    assert "uninstall" in cli.SUBCOMMANDS
    for verb in ("install", "uninstall"):
        assert verb not in daemon_mod._DAEMON_ELIGIBLE
        args = argparse.Namespace(
            command=verb, target="claude-md", root=str(tmp_path)
        )
        assert daemon_mod.try_daemon(args) is None


def test_target_names_are_unique() -> None:
    names = [t.name for t in cli.INSTALL_TARGETS]

    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    ("flag", "alias"),
    [
        ("--claude-install", "dekko install claude"),
        ("--claude-uninstall", "dekko uninstall claude"),
        ("--mcp-install", "dekko install claude --mcp-only"),
        ("--mcp-uninstall", "dekko uninstall claude --mcp-only"),
        ("--cline-install", "dekko install cline"),
        ("--cline-uninstall", "dekko uninstall cline"),
        ("--cline-scope", "dekko install cline --scope"),
        ("--cline-config", "dekko install cline --config"),
        ("--cline-force", "dekko install cline --force"),
        ("--claude-md-install", "dekko install claude-md"),
        ("--claude-md-uninstall", "dekko uninstall claude-md"),
    ],
)
def test_legacy_flag_help_names_its_new_command(flag: str, alias: str) -> None:
    parser = cli.build_legacy_parser()
    action = next(a for a in parser._actions if flag in a.option_strings)

    assert f"alias of '{alias}'" in (action.help or "")


def test_top_level_help_names_the_legacy_install_flags_as_aliases() -> None:
    text = " ".join(cli.build_subcommand_parser().format_help().split())

    assert "dekko --claude-install dekko install claude" in text
    assert "dekko --mcp-install dekko install claude --mcp-only" in text
    assert "dekko --cline-install dekko install cline" in text
    assert "dekko --claude-md-install dekko install claude-md" in text
    assert "dekko uninstall TARGET" in text
