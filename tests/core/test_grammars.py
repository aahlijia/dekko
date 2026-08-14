"""Grammar registry: Tier-1 offline resolution and Tier-2 fallback."""

import importlib.util

import pytest
from tree_sitter import Language

from dekko.core.grammars import (
    GrammarUnavailableError,
    get_grammar,
    is_grammar_unavailable_message,
)

TIER1 = [
    "c",
    "cpp",
    "go",
    "java",
    "javascript",
    "python",
    "rust",
    "typescript",
    "tsx",
]

_HAS_PACK = importlib.util.find_spec("tree_sitter_language_pack") is not None


@pytest.mark.parametrize("name", TIER1)
def test_tier1_resolves_offline(name: str) -> None:
    """Every Tier-1 grammar loads from its per-language package."""
    assert isinstance(get_grammar(name), Language)


def test_typescript_and_tsx_both_resolve() -> None:
    """One package supplies two distinct grammars."""
    assert isinstance(get_grammar("typescript"), Language)
    assert isinstance(get_grammar("tsx"), Language)


def test_result_is_cached() -> None:
    """Resolution is memoized, so a grammar loads once per process."""
    assert get_grammar("python") is get_grammar("python")


def test_grammar_unavailable_subclasses_value_error() -> None:
    """The per-file extractor's ``except ValueError`` tolerates it."""
    assert issubclass(GrammarUnavailableError, ValueError)


def test_unknown_grammar_raises() -> None:
    """An unresolvable name raises the tolerated error, not arbitrary."""
    with pytest.raises(GrammarUnavailableError):
        get_grammar("not-a-real-grammar-xyz")


@pytest.mark.skipif(_HAS_PACK, reason="pack installed; Tier-2 resolves")
def test_tier2_without_pack_raises() -> None:
    """Without ``dekko[all]``, a Tier-2 grammar is unavailable."""
    with pytest.raises(GrammarUnavailableError):
        get_grammar("ruby")


@pytest.mark.skipif(not _HAS_PACK, reason="requires dekko[all]")
def test_tier2_with_pack_resolves() -> None:
    """With the pack, a Tier-2 grammar resolves to a Language."""
    assert isinstance(get_grammar("ruby"), Language)


def test_is_grammar_unavailable_message_matches_both_raise_sites() -> None:
    """Round-12 master report §3.9/§3.10/§3.16: both
    ``GrammarUnavailableError`` messages ``get_grammar`` can raise
    (unknown name, and Tier-2-without-``dekko[all]``) must be
    recognized -- this is the single signal callers use to give
    "missing grammar" its own summary/heuristic bucket, separate from
    a genuine parse failure."""
    assert is_grammar_unavailable_message(
        "grammar 'kotlin' is not in the offline Tier-1 set; install "
        "the extras with `pip install dekko[all]`"
    )
    assert is_grammar_unavailable_message(
        "grammar 'not-a-real-grammar-xyz' is not available"
    )


def test_is_grammar_unavailable_message_rejects_unrelated_errors() -> None:
    """A genuine read/parse failure must not be misclassified as a
    missing grammar -- e.g. an ``OSError`` or tree-sitter parse
    exception's message, which never mentions Tier-1/availability."""
    assert not is_grammar_unavailable_message(
        "[Errno 13] Permission denied: 'src/locked.py'"
    )
    assert not is_grammar_unavailable_message("unexpected token at line 4")
