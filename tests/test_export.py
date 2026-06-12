"""Tests for the export subcommand."""

from lidar_map import cli

def test_export_mermaid(make_mapped_repo, capsys):
    root = make_mapped_repo({
        "a.py": "def common(): pass",
        "b.py": "from a import common\ndef b(): common()",
    })
    
    assert cli.main(["export", "--root", str(root), "--format", "mermaid"]) == 0
    out = (root / ".lidar" / "GRAPH-mermaid.md").read_text()
    assert "graph TD" in out
    assert "b.py::b" in out
    assert "-->" in out

def test_export_dot(make_mapped_repo, capsys):
    root = make_mapped_repo({
        "a.py": "def common(): pass",
        "b.py": "from a import common\ndef b(): common()",
    })
    
    assert cli.main(["export", "--root", str(root), "--format", "dot"]) == 0
    out = (root / ".lidar" / "GRAPH-dot.md").read_text()
    assert "digraph G" in out
    assert "b.py::b" in out
    assert "->" in out

def test_export_file_scope(make_mapped_repo, capsys):
    root = make_mapped_repo({
        "a.py": "def common(): pass\ndef common2(): pass",
        "b.py": "from a import common, common2\ndef b(): common()\ndef c(): common2()",
    })
    
    assert cli.main(["export", "--root", str(root), "--format", "mermaid", "--scope", "file"]) == 0
    out = (root / ".lidar" / "GRAPH-mermaid.md").read_text()
    assert "graph TD" in out
    assert "b.py" in out
    assert "a.py" in out
    # It should not contain individual symbols
    assert "b.py::b" not in out
