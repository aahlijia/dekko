"""Q2 token-counting seam: fallback, backend selection, determinism.

The suite pins ``DEKKO_TOKENIZER=chars4`` (see conftest), so these
assert the cheap path's exact behavior and the seam's contract; the
accurate path is exercised behind an explicit opt-in + importorskip.
"""

import os

import pytest

from dekko import textutil


def _reset_caches() -> None:
    # Robust to a monkeypatched _encoder (a plain function has no cache).
    for fn in (textutil._encoder, textutil._count_fragment):
        clear = getattr(fn, "cache_clear", None)
        if clear is not None:
            clear()


def test_chars4_is_the_pinned_backend() -> None:
    assert textutil.tokenizer_backend() == "chars4"


def test_estimate_tokens_chars4_formula() -> None:
    # ~4 chars per token, exactly len // 4 in the fallback.
    assert textutil.estimate_tokens("abcd") == 1
    assert textutil.estimate_tokens("a" * 40) == 10
    assert textutil.estimate_tokens("") == 0


def test_count_lines_sums_per_line() -> None:
    # Each line counted with its trailing newline, in chars4 mode.
    lines = ["x" * 8, "y" * 8]
    # (8+1)//4 == 2 each.
    assert textutil.count_lines(lines) == 4


def test_count_lines_empty() -> None:
    assert textutil.count_lines([]) == 0


def test_estimate_tokens_deterministic() -> None:
    text = "def f(x):\n    return x + 1\n"
    assert textutil.estimate_tokens(text) == textutil.estimate_tokens(text)


def test_mode_resolution_rejects_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEKKO_TOKENIZER", "nonsense")
    assert textutil._tokenizer_mode() == "auto"
    monkeypatch.setenv("DEKKO_TOKENIZER", "chars4")
    assert textutil._tokenizer_mode() == "chars4"


def test_encoder_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # An encoder that raises must degrade to chars/4, never propagate.
    class _Boom:
        def encode(self, text: str) -> list[int]:
            raise RuntimeError("boom")

    _reset_caches()
    monkeypatch.setattr(textutil, "_encoder", lambda: _Boom())
    assert textutil._count_fragment("abcd") == 1
    _reset_caches()


def test_accurate_path_when_tiktoken_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("tiktoken")
    monkeypatch.setenv("DEKKO_TOKENIZER", "auto")
    _reset_caches()
    try:
        assert textutil.tokenizer_backend() == "tiktoken"
        code = "def add(a, b):\n    return a + b\n"
        # A real BPE differs from the chars/4 estimate on code.
        assert textutil.estimate_tokens(code) != len(code) // 4
    finally:
        _reset_caches()  # restore the pinned chars4 for the next test


def test_chars4_env_forces_fallback_even_if_tiktoken_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEKKO_TOKENIZER", "chars4")
    _reset_caches()
    try:
        assert textutil.tokenizer_backend() == "chars4"
        assert textutil.estimate_tokens("abcd") == 1
    finally:
        _reset_caches()


def test_suite_env_is_pinned() -> None:
    assert os.environ.get("DEKKO_TOKENIZER") == "chars4"
