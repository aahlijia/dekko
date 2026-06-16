"""Pillar D: dense encoding and the FR-D3 density metric.

Covers Meter.signals (tokens-per-signal), the lean map's --dense skin
(FR-D1), and its seen/delta omission (FR-D2).
"""

from pathlib import Path

from dekko import cli, contextpack, query, render_lean
from dekko.mapfile import MapIndex, load_map
from dekko.textutil import Meter

from conftest import RepoFactory

# 40 symbols (> LEAN_DENSE_SIGNATURES), each with a realistically wide
# signature so dropping it to a bare name is a real per-atom saving.
_BIG = "".join(
    f"def fn_{i}(alpha: int, beta: str, gamma: int) -> dict[str, int]:\n"
    "    return {}\n\n"
    for i in range(40)
)
_FILES = {"src/big.py": '"""Module with many symbols."""\n' + _BIG}


def _index(make_mapped_repo: RepoFactory) -> tuple[Path, MapIndex]:
    root = make_mapped_repo(_FILES)
    index = load_map(root)
    assert index is not None
    return root, index


# --- FR-D3: Meter.signals --------------------------------------------


def test_meter_per_signal_and_footer() -> None:
    m = Meter(tokens=100, returned=5, total=5, signals=10)
    assert m.per_signal == 10.0
    assert "10 signals" in m.footer()
    doc = m.as_dict()
    assert doc["signals"] == 10 and doc["tokens_per_signal"] == 10.0


def test_meter_without_signals_is_unchanged() -> None:
    m = Meter(tokens=100, returned=5, total=5)
    assert m.per_signal is None
    assert "signals" not in m.footer()
    assert m.footer() == "(~100 tokens)"


def test_context_pack_reports_signals(
    make_mapped_repo: RepoFactory,
) -> None:
    _root, index = _index(make_mapped_repo)
    pack = contextpack.build_pack(
        index, query.resolve_target(index, "fn_0")[0], 1
    )
    text = contextpack.render_text(pack)
    meter = contextpack._pack_meter(pack, text, None)
    # target itself counts as a signal even with no neighbours.
    assert meter.signals >= 1


# --- FR-D1: --dense --------------------------------------------------


def test_dense_drops_signatures_off_the_tail(
    make_mapped_repo: RepoFactory,
) -> None:
    root, index = _index(make_mapped_repo)
    _, normal = render_lean.generate(index, root)
    _, dense = render_lean.generate(index, root, dense=True)
    # 40 symbols, keep sigs on the top 30 -> ~10 forced to names.
    assert dense.signatures_dropped > normal.signatures_dropped
    assert dense.tokens < normal.tokens
    # Same coverage: density improves, signals do not shrink.
    assert dense.signals == normal.signals


def test_dense_noop_below_threshold(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(
        {"src/few.py": '"""Few."""\ndef one() -> None:\n    pass\n'}
    )
    index = load_map(root)
    assert index is not None
    _, normal = render_lean.generate(index, root)
    _, dense = render_lean.generate(index, root, dense=True)
    assert dense.tokens == normal.tokens   # nothing to shed


def test_cli_lean_dense_smoke(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(_FILES)
    assert cli.main(["lean", "--root", str(root), "--dense"]) == 0


# --- FR-D2: seen / delta ---------------------------------------------


def test_seen_omits_and_counts_symbols(
    make_mapped_repo: RepoFactory,
) -> None:
    root, index = _index(make_mapped_repo)
    target = query.resolve_target(index, "fn_0")[0]
    assert target is not None
    lines, report = render_lean.generate(index, root, seen={target.id})
    body = "\n".join(lines)
    assert report.already_seen >= 1
    assert "fn_0(" not in body                  # omitted from the map
    assert "already in context" in report.footer()


def test_seen_empty_is_unchanged(make_mapped_repo: RepoFactory) -> None:
    root, index = _index(make_mapped_repo)
    a, _ = render_lean.generate(index, root)
    b, rep = render_lean.generate(index, root, seen=set())
    assert a == b and rep.already_seen == 0
