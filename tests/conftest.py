"""Shared pytest configuration.

The project is installed into the test environment (``uv run pytest``
syncs it), so tests import ``dekko`` directly — no path hacks.

The token-counting backend is pinned to the chars/4 fallback for the
whole suite (``DEKKO_TOKENIZER=chars4``) so budget assertions are
byte-stable whether or not the developer has ``tiktoken`` installed.
Tests that exercise the accurate path opt back in explicitly.
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

os.environ["DEKKO_TOKENIZER"] = "chars4"

from dekko.core import resolver as _resolver_mod
from dekko.integrations import cli


@pytest.fixture(autouse=True)
def _reset_pool_mp_context_cache() -> Iterator[None]:
    """Clear ``resolver``'s process-wide pool-context verdict per test.

    Round 30 (c): the fork/spawn decision is cached at first pool
    build for the life of the process (see ``_pool_mp_context``).
    Correct for real dekko processes, but the pytest process runs
    thousands of tests in one process, some of which hold helper
    threads -- without a reset, whichever test builds the first pool
    would pin the verdict for every later test, making pool-context
    behavior depend on suite ordering.
    """
    _resolver_mod._pool_ctx_cache = None
    yield
    _resolver_mod._pool_ctx_cache = None


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
