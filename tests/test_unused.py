"""Tests for the unused symbol finder."""

import json
from lidar_map import cli

def test_unused_basic(make_mapped_repo, capsys):
    root = make_mapped_repo({
        "a.py": "def used_func(): pass\ndef unused_func(): pass",
        "b.py": "from a import used_func\ndef main(): used_func()",
    })
    
    assert cli.main(["unused", "--root", str(root)]) == 0
    out, _ = capsys.readouterr()
    assert "unused_func" in out
    assert "a.py::used_func" not in out
    assert "main" not in out  # main is a root

def test_unused_roots(make_mapped_repo, capsys):
    root = make_mapped_repo({
        "a.py": "def keep_me(): pass\ndef discard_me(): pass",
    })
    
    assert cli.main(["unused", "--root", str(root), "--roots", "*keep*"]) == 0
    out, _ = capsys.readouterr()
    assert "discard_me" in out
    assert "keep_me" not in out

def test_unused_json(make_mapped_repo, capsys):
    root = make_mapped_repo({
        "a.py": "def unused_func(): pass",
    })
    
    assert cli.main(["unused", "--root", str(root), "--json"]) == 0
    out, _ = capsys.readouterr()
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["name"] == "unused_func"
