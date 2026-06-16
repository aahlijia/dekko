"""Small shared text helpers for the read-command renderers."""

import os
from dataclasses import dataclass
from functools import lru_cache

from .model import Symbol

# Token-counting backend (Q2). The accurate path uses ``tiktoken`` when
# it is installed (``pip install dekko[tokenizer]``); otherwise, and in
# the default install, counting falls back to a ~4-chars/token estimate.
# ``DEKKO_TOKENIZER=chars4`` forces the cheap path for reproducible,
# byte-stable output even when tiktoken is present.
_TIKTOKEN_ENCODING = "o200k_base"


def signature(sym: Symbol) -> str:
    """Format a symbol as a one-line signature."""
    if sym.kind == "class":
        return f"class {sym.qualname}"
    parts = [f"{p.name}: {p.type}" if p.type else p.name for p in sym.params]
    sig = f"{sym.qualname}({', '.join(parts)})"
    if sym.returns:
        sig += f" -> {sym.returns}"
    return sig


def oneline(text: str, limit: int = 80) -> str:
    """Collapse text to its first non-empty line, truncated to ``limit``.

    Args:
        text: Source text (e.g. a docstring first line).
        limit: Maximum length; longer lines are cut and suffixed
            with an ellipsis.

    Returns:
        A single-line string of at most ``limit`` characters, or the
        empty string when ``text`` has no content.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    first = stripped.splitlines()[0].strip()
    if len(first) > limit:
        first = first[: limit - 1].rstrip() + "…"
    return first


def dir_of(path: str) -> str:
    """Directory portion of a repo-relative path (``.`` for the root).

    Args:
        path: Repo-relative POSIX path, e.g. ``src/dekko/cli.py``.

    Returns:
        The directory portion (``src/dekko``), or ``.`` for a file at
        the repository root.
    """
    head, _, _ = path.rpartition("/")
    return head or "."


def _tokenizer_mode() -> str:
    """Resolve the backend mode from ``DEKKO_TOKENIZER`` (default auto)."""
    mode = os.environ.get("DEKKO_TOKENIZER", "auto").strip().lower()
    return mode if mode in ("auto", "chars4") else "auto"


@lru_cache(maxsize=1)
def _encoder() -> object | None:
    """Lazily build the tiktoken encoder, or ``None`` to use chars/4.

    Returns ``None`` when the mode is ``chars4``, tiktoken is not
    installed, or the encoding cannot be constructed — so the counter
    always has a working fallback and never raises.
    """
    if _tokenizer_mode() == "chars4":
        return None
    try:
        import tiktoken

        return tiktoken.get_encoding(_TIKTOKEN_ENCODING)
    except Exception:
        return None


def tokenizer_backend() -> str:
    """The active counting backend: ``"tiktoken"`` or ``"chars4"``."""
    return "tiktoken" if _encoder() is not None else "chars4"


@lru_cache(maxsize=16384)
def _count_fragment(text: str) -> int:
    """Token count of one text fragment: accurate, else chars/4.

    Cached so the hot fit loops (which re-measure recurring lines) stay
    cheap. Any encoder failure degrades to the chars/4 estimate.
    """
    enc = _encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return len(text) // 4


def estimate_tokens(text: str) -> int:
    """Estimate the token cost of ``text``.

    Accurate when a tokenizer backend is active (``tiktoken`` via the
    ``dekko[tokenizer]`` extra), and a ~4-chars/token estimate
    otherwise. The canonical one-shot count used by footers and size
    framing.
    """
    return _count_fragment(text)


def count_lines(lines: list[str]) -> int:
    """Sum cached per-line token counts; for hot budget loops.

    A fast, stable proxy for ``estimate_tokens("\\n".join(lines))``:
    each line is measured once (with its trailing newline) and cached,
    so re-measuring a mostly-unchanged document — as the lean map's
    degradation ladder does on every shed step — costs a sum of
    memoized integers rather than re-encoding the whole text. May differ
    slightly from the joined count at line boundaries; used for budget
    decisions, not the final reported figure.
    """
    return sum(_count_fragment(line + "\n") for line in lines)


def token_footer(text: str) -> str:
    """Self-metering footer line for text-mode output."""
    return f"(~{estimate_tokens(text)} tokens)"


@dataclass
class Meter:
    """Cost/omission summary for a budget-capped tool response.

    Projected to two surfaces from one object so they never drift:
    ``footer()`` for text output and ``as_dict()`` for JSON ``meta``.

    Attributes:
        tokens: Estimated token cost of the kept output (prefix + rows).
        returned: Rows kept after capping.
        total: Rows before any cap.
        budget: Token budget in effect, or ``None``.
        limit: Count limit in effect, or ``None``.
        signals: Distinct pieces of information covered (files + symbols),
            for the FR-D3 density view; ``0`` disables the density line.
    """

    tokens: int
    returned: int
    total: int
    budget: int | None = None
    limit: int | None = None
    signals: int = 0

    @property
    def omitted(self) -> int:
        """Rows dropped to satisfy the caps."""
        return max(0, self.total - self.returned)

    @property
    def per_signal(self) -> float | None:
        """Tokens spent per signal covered (FR-D3), or ``None``."""
        if self.signals <= 0:
            return None
        return round(self.tokens / self.signals, 1)

    @property
    def truncated_by(self) -> str | None:
        """Which cap bit: ``"budget"``, ``"limit"``, or ``None``."""
        if self.omitted == 0:
            return None
        if self.limit is not None and self.returned >= self.limit:
            return "limit"
        return "budget"

    def _density(self) -> str:
        """The optional ``· N signals`` density suffix (FR-D3)."""
        return f" · {self.signals} signals" if self.signals > 0 else ""

    def footer(self) -> str:
        """One-line text footer, stable enough to parse."""
        if self.omitted == 0:
            return f"(~{self.tokens} tokens{self._density()})"
        raise_hint = f"raise --{self.truncated_by}"
        return (
            f"(~{self.tokens} tokens{self._density()} · {self.omitted} of "
            f"{self.total} omitted · {raise_hint})"
        )

    def as_dict(self) -> dict:
        """Structured ``meta`` object for JSON output."""
        return {
            "tokens": self.tokens,
            "returned": self.returned,
            "total": self.total,
            "budget": self.budget,
            "limit": self.limit,
            "truncated_by": self.truncated_by,
            "signals": self.signals,
            "tokens_per_signal": self.per_signal,
        }


def fit_to_budget(
    lines: list[str], budget: int | None, limit: int | None, prefix: str = ""
) -> tuple[list[str], Meter]:
    """Cap lines by count then token budget, returning kept + Meter.

    Lines are assumed already ordered most- to least-relevant. The count
    ``limit`` applies first (cheap pre-filter); then lines are kept
    greedily until adding the next would push the estimated token cost
    of ``prefix`` plus the kept rows past ``budget``. At least one row is
    always kept when any exist, even under a tiny budget. ``None`` for
    either bound disables it. Deterministic for a fixed input order.

    Args:
        lines: Candidate output rows, most-relevant first.
        budget: Approximate token budget for prefix + rows, or ``None``.
        limit: Maximum row count, or ``None``.
        prefix: Non-droppable leading text (headers) counted against the
            budget and the reported token cost.

    Returns:
        ``(kept_lines, meter)``.
    """
    total = len(lines)
    capped = lines if limit is None else lines[:limit]
    kept: list[str] = []
    running = prefix
    for line in capped:
        candidate = f"{running}\n{line}" if running else line
        if budget is not None and kept and estimate_tokens(candidate) > budget:
            break
        running = candidate
        kept.append(line)
    return kept, Meter(
        tokens=estimate_tokens(running),
        returned=len(kept),
        total=total,
        budget=budget,
        limit=limit,
    )
