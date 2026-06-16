"""Small shared text helpers for the read-command renderers."""

from dataclasses import dataclass

from .model import Symbol


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


def estimate_tokens(text: str) -> int:
    """Crude token estimate (~4 characters per token)."""
    return len(text) // 4


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
    """

    tokens: int
    returned: int
    total: int
    budget: int | None = None
    limit: int | None = None

    @property
    def omitted(self) -> int:
        """Rows dropped to satisfy the caps."""
        return max(0, self.total - self.returned)

    @property
    def truncated_by(self) -> str | None:
        """Which cap bit: ``"budget"``, ``"limit"``, or ``None``."""
        if self.omitted == 0:
            return None
        if self.limit is not None and self.returned >= self.limit:
            return "limit"
        return "budget"

    def footer(self) -> str:
        """One-line text footer, stable enough to parse."""
        if self.omitted == 0:
            return f"(~{self.tokens} tokens)"
        raise_hint = f"raise --{self.truncated_by}"
        return (
            f"(~{self.tokens} tokens · {self.omitted} of "
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
