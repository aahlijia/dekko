"""Tier-2 shell call extraction (round 31 tensorflow coverage pass).

tree-sitter-bash's call node is ``command``, which the generic
extractor's call heuristic never matched: zero calls were extracted
from any bash file, so every bash function had fan-in 0 and was
listed by ``dekko unused`` with no caveat.
"""

import importlib.util
from pathlib import Path

import pytest

from dekko.core.extractor_generic import extract_file_generic

_HAS_PACK = importlib.util.find_spec("tree_sitter_language_pack") is not None
pytestmark = pytest.mark.skipif(
    not _HAS_PACK, reason="Tier-2 grammar pack not installed (dekko[all])"
)

SCRIPT = (
    "tfrun() {\n"
    '  echo "$@"\n'
    "}\n"
    "\n"
    "helper() {\n"
    "  out=$(tfrun build)\n"
    '  ./other.sh -f "$out"\n'
    '  "$TOOL" --version\n'
    "}\n"
    "\n"
    "tfrun test\n"
    "helper\n"
)


def _calls(tmp_path: Path) -> list[tuple[str | None, str, int]]:
    (tmp_path / "run.sh").write_text(SCRIPT)
    fm = extract_file_generic(tmp_path, "run.sh", "bash")
    assert fm.error is None
    return [(c.caller_id, c.name, c.line) for c in fm.calls]


def test_bash_function_calls_are_extracted(tmp_path: Path) -> None:
    calls = _calls(tmp_path)
    # Inside a function body, including inside $(...), and at top level.
    assert ("run.sh::helper", "tfrun", 6) in calls
    assert (None, "tfrun", 11) in calls
    assert (None, "helper", 12) in calls


def test_bash_non_identifier_commands_are_dropped(tmp_path: Path) -> None:
    # A path or an expansion in command position can never be a call to
    # a repo-defined function; keeping them would add one `external`
    # entry per script path and flag in the repo.
    names = {name for _, name, _ in _calls(tmp_path)}
    assert names == {"tfrun", "helper", "echo"}
