"""Guard the script's declared requires-python floor.

The dev venv runs a newer Python, so syntax that needs 3.12+ (e.g.
PEP 701 multi-line f-strings) passes the rest of the suite even though
it breaks on the 3.10 floor declared in lidar.py's PEP 723 header.
``ast.parse(feature_version=...)`` does not catch f-string grammar
changes, so the sources must be compiled by a real interpreter at the
floor; the test skips when none is installed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent / "tool"
FLOOR_VERSIONS = ("3.10", "3.11")


def _floor_python() -> str | None:
    """Find a pre-3.12 interpreter on PATH or via uv, or ``None``."""
    for version in FLOOR_VERSIONS:
        exe = shutil.which(f"python{version}")
        if exe is not None:
            return exe
        try:
            proc = subprocess.run(
                ["uv", "python", "find", version],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return None


def test_tool_sources_compile_on_floor_python() -> None:
    python = _floor_python()
    if python is None:
        pytest.skip("no Python 3.10/3.11 interpreter available")
    sources = sorted(TOOL_DIR.glob("*.py"))
    assert sources, f"no tool sources found under {TOOL_DIR}"
    proc = subprocess.run(
        [python, "-m", "py_compile", *map(str, sources)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
