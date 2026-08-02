"""Outline subcommand: structure, nesting, size framing, budget."""

import json

import pytest

from dekko import cli

from conftest import RepoFactory

PY = {
    "a.py": (
        '"""Module A does things."""\n'
        "def helper(x: int) -> int:\n"
        '    """Add one."""\n'
        "    return x + 1\n"
        "\n"
        "\n"
        "class Thing:\n"
        '    """A thing."""\n'
        "    def go(self) -> None:\n"
        "        helper(1)\n"
    ),
    "b.py": "def lone() -> None:\n    pass\n",
}


def test_outline_renders_structure(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["outline", "a.py", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "outline: a.py  [python]" in out
    assert "Module A does things" in out
    assert "helper(x: int) -> int" in out
    assert "Add one" in out
    assert "class Thing" in out


def test_members_indented_and_bare_named(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    cli.main(["outline", "a.py", "--root", str(root)])
    out = capsys.readouterr().out
    assert "go(self) -> None" in out
    # Nesting is shown by indent, so the container prefix is dropped.
    assert "Thing.go" not in out
    go_row = next(ln for ln in out.splitlines() if "go(self)" in ln)
    assert go_row.startswith("    ")


def test_variable_symbol_renders_as_bare_name(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"data.ts": "export const jobs = [1, 2, 3];\n"})
    code = cli.main(["outline", "data.ts", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    # A "variable"-kind symbol renders as its bare name, never as a
    # zero-arg function call (``jobs()``).
    assert "jobs" in out
    assert "jobs()" not in out


def test_interface_symbol_renders_as_bare_name(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # F2: outline.py::_outline_sig() already special-cases
    # sym.kind in TYPE_KINDS (interface/enum/struct/record/trait) with
    # no trailing parens, mirroring the "variable" case above — this
    # closes the regression-coverage gap the report flagged, since only
    # the "variable" half had a test before this.
    root = make_mapped_repo(
        {"data.ts": "export interface Item {\n  id: number;\n}\n"}
    )
    code = cli.main(["outline", "data.ts", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "interface Item" in out
    assert "Item()" not in out


def test_size_framing_present(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    cli.main(["outline", "a.py", "--root", str(root)])
    out = capsys.readouterr().out
    assert "full ≈" in out
    assert "outline ≈" in out


def test_docless_file_has_no_emdash(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    cli.main(["outline", "b.py", "--root", str(root)])
    out = capsys.readouterr().out
    assert "lone() -> None" in out
    assert "—" not in out


def test_budget_trims_and_footers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["outline", "a.py", "--root", str(root), "--budget", "1"])
    assert code == 0
    out = capsys.readouterr().out
    assert "omitted" in out
    assert "raise --budget" in out


def test_directory_rollup(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["outline", ".", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "2 files" in out
    assert "helper(x: int) -> int" in out
    assert "lone() -> None" in out


def test_outline_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["outline", "a.py", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["files"][0]["path"] == "a.py"
    assert doc["files"][0]["full_tokens"] > 0
    sigs = [s["signature"] for s in doc["files"][0]["symbols"]]
    assert "helper(x: int) -> int" in sigs
    assert doc["meta"]["total"] == 3


def test_outline_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["outline", "zzz.py", "--root", str(root)])
    assert code == 3
    assert "no mapped file or directory" in capsys.readouterr().err


# A large, mostly-comment file with a single named symbol — stands in
# for a callback-heavy MCP-server/route-handler file (claude-buddy's
# real repro used anonymous arrow-function handlers, which this
# extractor also never turns into named symbols; padding comments are
# a simpler way to reach the same "big file, ~1 named symbol" shape
# without depending on any one language's callback grammar).
SPARSE_FILE = {
    "server_index.ts": (
        "export function setup(): void {\n  console.log('setup');\n}\n\n"
        + "\n".join(
            f"// registered handler {i}: does something important here"
            for i in range(80)
        )
        + "\n"
    )
}


def test_sparse_file_gets_a_caveat(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # B9: a callback-heavy file's outline can look like an extreme
    # (and perfectly legitimate-looking) savings ratio while actually
    # hiding nearly all of the file's real content. A large file with
    # very few named symbols must carry a caveat, not read as complete.
    root = make_mapped_repo(SPARSE_FILE)
    code = cli.main(["outline", "server_index.ts", "--root", str(root)])
    assert code == 0
    err = capsys.readouterr().err
    assert "very few named symbols for its size" in err
    assert "anonymous callbacks" in err


def test_sparse_file_json_carries_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SPARSE_FILE)
    code = cli.main(
        ["outline", "server_index.ts", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert "very few named symbols" in doc["files"][0]["sparse_note"]


def test_normal_small_file_has_no_sparse_caveat(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY)
    code = cli.main(["outline", "a.py", "--root", str(root)])
    assert code == 0
    assert "very few named symbols" not in capsys.readouterr().err
