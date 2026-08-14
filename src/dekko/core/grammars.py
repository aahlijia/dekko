"""Grammar resolution: a grammar name to a ``tree_sitter.Language``.

This is the seam that lets the rest of dekko stay agnostic about *where*
a grammar comes from. The nine Tier-1 grammars resolve from individual,
pinned grammar packages (core dependencies), so a default install parses
them **fully offline** — no runtime grammar download. Every other
(Tier-2) grammar resolves through the optional
``tree-sitter-language-pack`` (``pip install dekko[all]``), which fetches
grammars on demand; without it, resolution raises
:class:`GrammarUnavailableError`, which the generic extractor tolerates per
file.

Resolution is cached, so each grammar's package is imported at most once
per process — the first time that grammar is actually needed.
"""

import importlib
from functools import lru_cache

from tree_sitter import Language

# Tier-1 grammar name -> (package to import, accessor returning a capsule).
# tree-sitter-typescript ships both `typescript` and `tsx`.
_TIER1: dict[str, tuple[str, str]] = {
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "python": ("tree_sitter_python", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
}


class GrammarUnavailableError(ValueError):
    """A grammar could not be resolved.

    Raised for an unknown grammar name, or a Tier-2 grammar when the
    optional ``dekko[all]`` pack is not installed. Subclasses
    ``ValueError`` so the per-file extractor try/except already tolerates
    it (degrading that one file rather than failing the run).
    """


# Substrings unique to this module's two ``GrammarUnavailableError``
# messages (below), used by ``is_grammar_unavailable_message`` to tell
# a missing-optional-grammar skip apart from a genuine parse failure
# after both have collapsed into a plain string on ``FileMap.error``/
# ``MapIndex.errors_by_path`` — there is no separate error-kind field
# on the wire (``extractor_generic.extract_file_generic`` catches
# ``GrammarUnavailableError`` alongside every other read/parse
# exception, and only ``str(exc)`` survives).
_UNAVAILABLE_MARKERS = (
    "is not in the offline Tier-1 set",
    "is not available",
)


def is_grammar_unavailable_message(error: str) -> bool:
    """Whether a ``FileMap.error``/``errors_by_path`` message names a
    missing optional grammar rather than a genuine parse failure.

    Round-12 master report §3.9/§3.10/§3.16: ``dekko map``'s top-line
    "skipped: parse error N" summary bucket, and ``outline``'s "few
    named symbols for its size" heuristic, both used to lump "this
    file genuinely failed to parse" together with "this optional
    grammar (``pip install dekko[all]``) isn't installed" — the
    per-file detail line already named the missing grammar
    accurately, but the terse top-line/heuristic wording read as
    alarming ("broken") for what is really just an opt-in dependency
    gap. This is a message-substring check (not a distinct exception
    type check) because the type information doesn't survive past
    ``extract_file_generic``'s broad ``except Exception`` -- see that
    function's docstring.

    Args:
        error: A ``FileMap.error`` or ``MapIndex.errors_by_path``
            value.

    Returns:
        True when the message came from ``get_grammar`` raising
        ``GrammarUnavailableError``, not from an unrelated
        read/parse failure.
    """
    return any(marker in error for marker in _UNAVAILABLE_MARKERS)


@lru_cache(maxsize=None)
def get_grammar(name: str) -> Language:
    """Resolve a grammar name to a ``tree_sitter.Language``.

    Tier-1 names resolve offline from their per-language packages. Any
    other name is delegated to the optional grammar pack.

    Args:
        name: Tree-sitter grammar name (e.g. ``"python"``, ``"tsx"``).

    Returns:
        The compiled ``Language`` for that grammar.

    Raises:
        GrammarUnavailableError: The name is unknown, or it is a Tier-2
            grammar and ``dekko[all]`` is not installed.
    """
    spec = _TIER1.get(name)
    if spec is not None:
        module_name, accessor = spec
        module = importlib.import_module(module_name)
        return Language(getattr(module, accessor)())

    try:
        from tree_sitter_language_pack import get_language
    except ImportError as exc:
        raise GrammarUnavailableError(
            f"grammar '{name}' is not in the offline Tier-1 set; "
            "install the extras with `pip install dekko[all]`"
        ) from exc
    try:
        return get_language(name)
    except Exception as exc:  # unknown grammar / load failure
        raise GrammarUnavailableError(
            f"grammar '{name}' is not available"
        ) from exc
