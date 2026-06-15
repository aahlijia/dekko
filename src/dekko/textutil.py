"""Small shared text helpers for the read-command renderers."""

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


def estimate_tokens(text: str) -> int:
    """Crude token estimate (~4 characters per token)."""
    return len(text) // 4


def token_footer(text: str) -> str:
    """Self-metering footer line for text-mode output."""
    return f"(~{estimate_tokens(text)} tokens)"
