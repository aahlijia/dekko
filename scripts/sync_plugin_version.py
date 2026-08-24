#!/usr/bin/env python3
"""Keep plugin.json/marketplace.json version in sync with pyproject.toml.

Run with no arguments to sync the two Claude Code plugin manifests to
whatever version pyproject.toml already declares -- this is what the
``sync-plugin-version`` pre-commit hook runs on every commit that
touches ``pyproject.toml``. Pass a version string to also bump
pyproject.toml itself and refresh uv.lock in one step, e.g.::

    uv run python scripts/sync_plugin_version.py 0.44.0

Either way the manifests end up agreeing with pyproject.toml, which is
exactly what ``tests/test_version.py::test_declared_versions_agree``
checks for.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
PLUGIN_MANIFESTS: tuple[tuple[Path, tuple[str, ...]], ...] = (
    (
        ROOT / "integrations/claude/.claude-plugin/plugin.json",
        ("version",),
    ),
    (
        ROOT / "integrations/claude/.claude-plugin/marketplace.json",
        ("metadata", "version"),
    ),
)
_VERSION_LINE = re.compile(r'(?m)^version = "([^"]+)"$')


def read_pyproject_version() -> str:
    """Return the ``[project]`` version declared in pyproject.toml.

    Returns:
        The version string, e.g. ``"0.43.5"``.

    Raises:
        SystemExit: If pyproject.toml has no ``version = "..."`` line.
    """
    text = PYPROJECT.read_text()
    match = _VERSION_LINE.search(text)
    if match is None:
        sys.exit(f"no version found in {PYPROJECT}")

    return match.group(1)


def write_pyproject_version(version: str) -> None:
    """Rewrite pyproject.toml's ``[project]`` version in place.

    Args:
        version: The new version string to write.

    Raises:
        SystemExit: If pyproject.toml has no ``version = "..."`` line.
    """
    text = PYPROJECT.read_text()
    new_text, count = _VERSION_LINE.subn(f'version = "{version}"', text)
    if count == 0:
        sys.exit(f"no version found in {PYPROJECT}")

    PYPROJECT.write_text(new_text)


def sync_manifest(
    path: Path,
    keys: tuple[str, ...],
    version: str,
) -> bool:
    """Set a nested JSON key to ``version`` if it disagrees.

    Args:
        path: JSON file to update.
        keys: Path of nested keys leading to the version field.
        version: The version string the field should hold.

    Returns:
        True if the file was rewritten, False if it already agreed.
    """
    data = json.loads(path.read_text())
    node = data
    for key in keys[:-1]:
        node = node[key]

    if node[keys[-1]] == version:
        return False

    node[keys[-1]] = version
    text = json.dumps(data, indent=2, ensure_ascii=False)
    path.write_text(text + "\n")
    return True


def run_uv_lock() -> None:
    """Refresh uv.lock so its pinned dekko version matches pyproject.toml."""
    subprocess.run(["uv", "lock"], cwd=ROOT, check=True)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sync plugin.json/marketplace.json with pyproject.toml's "
            "version, optionally bumping pyproject.toml itself."
        ),
    )
    parser.add_argument(
        "version",
        nargs="?",
        default=None,
        help=(
            "New version to bump pyproject.toml to. Omit to sync the "
            "manifests to pyproject.toml's current version instead."
        ),
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Skip running `uv lock` after a version bump.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Sync the plugin manifests, optionally bumping the version first.

    Args:
        argv: Command-line arguments, or None to use ``sys.argv``.

    Returns:
        Process exit code (always 0 -- failures raise ``SystemExit``
        or propagate a ``subprocess.CalledProcessError`` instead).
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    if args.version is not None:
        write_pyproject_version(args.version)

    version = read_pyproject_version()
    changed = [
        str(path.relative_to(ROOT))
        for path, keys in PLUGIN_MANIFESTS
        if sync_manifest(path, keys, version)
    ]

    if args.version is not None and not args.no_lock:
        run_uv_lock()

    if changed:
        print(f"synced to {version}: {', '.join(changed)}")
    else:
        print(f"already in sync at {version}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
