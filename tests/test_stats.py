"""Tests for the stats subcommand."""

import json
from lidar_map import cli

def test_stats_basic(make_mapped_repo, capsys):
    root = make_mapped_repo({
        "a.py": "def common(): pass",
        "b.py": "from a import common\ndef b(): common()",
        "c.py": "from a import common\ndef c(): common()",
    })
    
    assert cli.main(["stats", "--root", str(root)]) == 0
    out, _ = capsys.readouterr()
    assert "Call Graph Statistics" in out
    assert "a.py::common" in out
    assert "Language Breakdown" in out

def test_stats_json(make_mapped_repo, capsys):
    root = make_mapped_repo({
        "a.py": "def common(): pass",
        "b.py": "from a import common\ndef b(): common()",
    })
    
    assert cli.main(["stats", "--root", str(root), "--json"]) == 0
    out, _ = capsys.readouterr()
    data = json.loads(out)
    assert "top_fan_in" in data
    assert "largest_files" in data
    assert "languages" in data
