"""Context packs: neighborhood building, hops, budget trimming."""

import json

import pytest

from dekko import cli, contextpack, mapfile

from conftest import RepoFactory

CHAIN3 = {
    "chain.py": (
        "def low() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def mid() -> int:\n"
        "    return low()\n"
        "\n"
        "\n"
        "def top() -> int:\n"
        "    return mid()\n"
    )
}


def _resolved(root, name):  # noqa: ANN001, ANN202
    index = mapfile.load_map(root)
    return index, index.symbols_by_qualname[name][0]


def test_hop1_pack(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN3)
    index, mid = _resolved(root, "mid")
    pack = contextpack.build_pack(index, mid, hops=1)
    names = {(e.sym.qualname, e.direction) for e in pack.entries}
    assert names == {("top", "caller"), ("low", "callee")}


def test_hops2_grows_pack(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN3)
    index, top = _resolved(root, "top")
    pack1 = contextpack.build_pack(index, top, hops=1)
    pack2 = contextpack.build_pack(index, top, hops=2)
    assert len(pack2.entries) > len(pack1.entries)
    assert {e.sym.qualname for e in pack2.entries} == {"mid", "low"}
    assert {e.hop for e in pack2.entries} == {1, 2}


def test_budget_trims_but_keeps_target(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CHAIN3)
    code = cli.main(
        [
            "context",
            "mid",
            "--root",
            str(root),
            "--hops",
            "2",
            "--budget",
            "30",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "context: chain.py:mid" in out
    assert "mid() -> int" in out
    assert "trimmed" in out


def test_file_mode_pack(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = dict(
        CHAIN3,
        **{
            "user.py": (
                "from chain import top\n"
                "\n"
                "\n"
                "def run() -> int:\n"
                "    return top()\n"
            )
        },
    )
    root = make_mapped_repo(files)
    code = cli.main(["context", "chain.py", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["target"] is None
    own = {s["signature"] for s in doc["file_symbols"]}
    assert "top() -> int" in own
    callers = {n["path"] for n in doc["neighbors"]}
    assert callers == {"user.py"}


def test_context_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CHAIN3)
    assert cli.main(["context", "ghost", "--root", str(root)]) == 3
