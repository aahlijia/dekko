"""Guard: the version is declared in four places and must agree.

A release bump has to touch pyproject.toml, both plugin manifests, and
uv.lock together. This test fails loudly when they drift, which is the
mistake that has slipped through before.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    """The ``[project]`` version from pyproject.toml."""
    text = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'(?m)^version = "([^"]+)"', text)
    assert match is not None, "no version found in pyproject.toml"
    return match.group(1)


def _json_version(rel: str, *keys: str) -> str:
    """Follow ``keys`` into a JSON file and return the value."""
    value = json.loads((ROOT / rel).read_text())
    for key in keys:
        value = value[key]
    assert isinstance(value, str)
    return value


def _uvlock_version() -> str:
    """The pinned lidar-map version recorded in uv.lock."""
    text = (ROOT / "uv.lock").read_text()
    match = re.search(r'name = "lidar-map"\s*\nversion = "([^"]+)"', text)
    assert match is not None, "lidar-map not pinned in uv.lock"
    return match.group(1)


def test_declared_versions_agree() -> None:
    versions = {
        "pyproject.toml": _pyproject_version(),
        "plugin.json": _json_version(".claude-plugin/plugin.json", "version"),
        "marketplace.json": _json_version(
            ".claude-plugin/marketplace.json", "metadata", "version"
        ),
        "uv.lock": _uvlock_version(),
    }
    assert len(set(versions.values())) == 1, (
        f"version strings disagree: {versions}"
    )
