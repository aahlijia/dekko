"""File discovery tests."""

from pathlib import Path

from lidar_map.walker import discover


def _touch(path: Path, content: str = "x = 1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_discover_filters(tmp_path: Path) -> None:
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "src" / "core.rs")
    _touch(tmp_path / "node_modules" / "pkg" / "x.py")
    _touch(tmp_path / "notes.txt")
    _touch(tmp_path / "gen" / "schema_pb2.py")
    _touch(tmp_path / "big.py", "x = 1\n" * 100)

    files, skipped = discover(tmp_path, max_file_size=50)
    assert files == ["src/app.py", "src/core.rs"]
    reasons = dict(skipped)
    assert reasons["gen/schema_pb2.py"] == "generated"
    assert reasons["big.py"] == "too large"


def test_discover_subpath_and_excludes(tmp_path: Path) -> None:
    _touch(tmp_path / "src" / "app.py")
    _touch(tmp_path / "src" / "skip_me.py")
    _touch(tmp_path / "other" / "b.py")

    files, skipped = discover(
        tmp_path,
        subpath="src",
        excludes=("skip_*.py",),
    )
    assert files == ["src/app.py"]
    assert ("src/skip_me.py", "excluded") in skipped
