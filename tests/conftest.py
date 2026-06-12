"""Shared pytest configuration.

The project is installed into the test environment (``uv run pytest``
syncs it), so tests import ``lidar_map`` directly — no path hacks.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from lidar_map import cli

RepoFactory = Callable[[dict[str, str]], Path]


@pytest.fixture
def make_mapped_repo(tmp_path: Path) -> RepoFactory:
    """Factory: write source files into tmp_path and map them."""

    def _make(files: dict[str, str]) -> Path:
        for name, text in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
        return tmp_path

    return _make
