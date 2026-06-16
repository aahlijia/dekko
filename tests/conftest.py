"""Shared pytest configuration.

The project is installed into the test environment (``uv run pytest``
syncs it), so tests import ``dekko`` directly — no path hacks.

The token-counting backend is pinned to the chars/4 fallback for the
whole suite (``DEKKO_TOKENIZER=chars4``) so budget assertions are
byte-stable whether or not the developer has ``tiktoken`` installed.
Tests that exercise the accurate path opt back in explicitly.
"""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

os.environ["DEKKO_TOKENIZER"] = "chars4"

from dekko import cli

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
