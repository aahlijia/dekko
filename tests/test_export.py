"""The export command: mermaid/dot rendering, scope, size guard."""

import pytest

from dekko import cli

from conftest import RepoFactory

SRC = {
    "a.py": "def f() -> int:\n    return 1\n",
    "b.py": "from a import f\n\n\ndef g() -> int:\n    return f()\n",
}


def test_export_mermaid_symbol_scope(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    args = ["export", "--format", "mermaid", "--root", str(root)]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith("flowchart LR")
    assert '["g"]' in out and '["f"]' in out
    assert "-->" in out


def test_export_dot_file_scope(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    code = cli.main(
        [
            "export",
            "--format",
            "dot",
            "--scope",
            "file",
            "--root",
            str(root),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("digraph dekko {")
    assert 'label="b.py"' in out
    assert "->" in out and out.rstrip().endswith("}")


def test_export_max_nodes_guard(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SRC)
    code = cli.main(
        [
            "export",
            "--format",
            "mermaid",
            "--max-nodes",
            "1",
            "--root",
            str(root),
        ]
    )
    assert code == 2
    assert "use --scope file" in capsys.readouterr().err


def test_export_requires_format(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["export"])
    assert exc.value.code == 2
