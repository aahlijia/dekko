"""Tests for scripts/sync_plugin_version.py.

The script runs against a temp copy of pyproject.toml + the two
Claude Code plugin manifests (never the real repo files) so it's
tested the same way the ``sync-plugin-version`` pre-commit hook and a
deliberate version bump would exercise it: as a standalone process
with its own ``ROOT`` resolved from its own location on disk. See
``.features/fixes/plugin-manifest-version-drift.md`` for the drift
this guards against.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "sync_plugin_version.py"
)

PLUGIN_JSON = "integrations/claude/.claude-plugin/plugin.json"
MARKETPLACE_JSON = "integrations/claude/.claude-plugin/marketplace.json"


def _make_repo(tmp_path: Path, *, pyproject: str, manifests: str) -> Path:
    """Lay out a minimal repo skeleton the script can run against.

    Args:
        tmp_path: Pytest's per-test temp directory.
        pyproject: Version to write into pyproject.toml.
        manifests: Version to write into both plugin manifests.

    Returns:
        The temp repo root (same directory as ``tmp_path``).
    """
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)

    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "dekko"\nversion = "{pyproject}"\n'
    )

    plugin_dir = tmp_path / "integrations" / "claude" / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {"name": "dekko", "version": manifests},
            indent=2,
        )
        + "\n"
    )
    (plugin_dir / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "dekko",
                "metadata": {"version": manifests},
            },
            indent=2,
        )
        + "\n"
    )
    return tmp_path


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the script against ``repo`` and return the completed run."""
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / SCRIPT.name), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def _read_versions(repo: Path) -> tuple[str, str, str]:
    pyproject_version = (repo / "pyproject.toml").read_text()
    plugin = json.loads((repo / PLUGIN_JSON).read_text())
    marketplace = json.loads((repo / MARKETPLACE_JSON).read_text())
    return (
        pyproject_version,
        plugin["version"],
        marketplace["metadata"]["version"],
    )


def test_sync_rewrites_mismatched_manifests(tmp_path: Path) -> None:
    """Manifests behind pyproject.toml are rewritten to match it."""
    repo = _make_repo(tmp_path, pyproject="0.43.5", manifests="0.43.3")

    result = _run(repo)

    _, plugin_version, marketplace_version = _read_versions(repo)
    assert plugin_version == "0.43.5"
    assert marketplace_version == "0.43.5"
    assert "synced to 0.43.5" in result.stdout


def test_sync_is_a_noop_when_already_agreeing(
    tmp_path: Path,
) -> None:
    """Nothing is rewritten when the manifests already agree."""
    repo = _make_repo(tmp_path, pyproject="0.43.5", manifests="0.43.5")
    plugin_path = repo / PLUGIN_JSON
    before = plugin_path.stat().st_mtime_ns
    before_text = plugin_path.read_text()

    result = _run(repo)

    assert plugin_path.stat().st_mtime_ns == before
    assert plugin_path.read_text() == before_text
    assert "already in sync" in result.stdout


def test_bump_updates_pyproject_and_manifests_together(
    tmp_path: Path,
) -> None:
    """A version argument bumps pyproject.toml and the manifests."""
    repo = _make_repo(tmp_path, pyproject="0.43.5", manifests="0.43.5")

    _run(repo, "0.44.0", "--no-lock")

    pyproject_text, plugin_version, marketplace_version = _read_versions(repo)
    assert 'version = "0.44.0"' in pyproject_text
    assert plugin_version == "0.44.0"
    assert marketplace_version == "0.44.0"


def test_bump_preserves_manifest_formatting(tmp_path: Path) -> None:
    """Only the version value changes -- key order/indent survive."""
    repo = _make_repo(tmp_path, pyproject="0.43.5", manifests="0.43.3")

    _run(repo)

    plugin_text = (repo / PLUGIN_JSON).read_text()
    assert plugin_text.startswith('{\n  "name": "dekko"')
    assert plugin_text.endswith("}\n")
