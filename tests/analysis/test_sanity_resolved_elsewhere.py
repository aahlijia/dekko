"""A grep-only row the map attributes to a different same-named symbol.

The map records which ``(path, line)`` sites it resolved to each
symbol sharing a bare name, as call sites and as value references.
A grep-only row for one target that sits on a site attributed to a
*different* symbol with that name is not a miss on the target: the
row gets an exact cause naming the sibling, in single-target and
``--all`` mode alike, and a row the map also attributes to the target
itself keeps the target's own reference cause.
"""

import json
from pathlib import Path

import pytest

from dekko.analysis import sanity
from dekko.integrations import cli
from conftest import RepoFactory

_ICON_A = "def icon():\n    return 1\n"
_ICON_B = "def icon():\n    return 2\n"
_CALL_B = "from b import icon\n\n\ndef use_b():\n    return icon()\n"
_CALL_A = "from a import icon\n\n\ndef use_a():\n    return icon()\n"


def _sanity_json(
    root: Path, target: str, capsys: pytest.CaptureFixture
) -> dict:
    code = cli.main(["sanity", target, "--root", str(root), "--json"])
    assert code == 0

    return json.loads(capsys.readouterr().out)


def _grep_only(doc: dict) -> dict[tuple[str, int], dict]:
    return {(row["file"], row["line"]): row for row in doc["grep_only"]}


def test_third_file_call_to_the_sibling_is_resolved_elsewhere(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {"a.py": _ICON_A, "b.py": _ICON_B, "c.py": _CALL_B}
    )
    doc = _sanity_json(root, "a.py:icon", capsys)
    row = _grep_only(doc)[("c.py", 5)]
    assert row["cause"] == sanity.CAUSE_RESOLVED_ELSEWHERE
    assert row["resolved_to"] == ["b.py::icon"]

    doc = _sanity_json(root, "b.py:icon", capsys)
    assert ("c.py", 5) in {(r["file"], r["line"]) for r in doc["matches"]}


def test_sibling_value_reference_is_resolved_elsewhere(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    svc = "export class Svc {\n  static instance: Svc;\n}\n"
    root = make_mapped_repo(
        {
            "a.ts": svc,
            "b.ts": svc,
            "c.ts": (
                "import { Svc } from './b';\n"
                "export function get() {\n"
                "  return Svc.instance;\n"
                "}\n"
            ),
        }
    )
    doc = _sanity_json(root, "a.ts:Svc", capsys)
    row = _grep_only(doc)[("c.ts", 3)]
    assert row["cause"] == sanity.CAUSE_RESOLVED_ELSEWHERE
    assert row["resolved_to"] == ["b.ts::Svc"]


def test_own_value_reference_beside_a_sibling_call_keeps_its_cause(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "a.ts": "export function icon() {\n  return 1;\n}\n",
            "b.ts": "export function icon(f: unknown) {\n  return 2;\n}\n",
            "c.ts": (
                "import { icon } from './a';\n"
                "import { icon as other } from './b';\n"
                "export const x = other(icon);\n"
            ),
        }
    )
    doc = _sanity_json(root, "a.ts:icon", capsys)
    row = _grep_only(doc)[("c.ts", 3)]
    assert row["cause"] == sanity.CAUSE_VALUE_REFERENCE
    assert "resolved_to" not in row


def test_test_file_site_keeps_the_test_filter_cause(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "a.py": _ICON_A,
            "b.py": _ICON_B,
            "test_c.py": (
                "from b import icon\n\n\ndef test_icon():\n    assert icon()\n"
            ),
        }
    )
    doc = _sanity_json(root, "a.py:icon", capsys)
    assert (
        _grep_only(doc)[("test_c.py", 5)]["cause"] == sanity.CAUSE_TEST_FILTER
    )


def test_text_report_names_the_sibling(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {"a.py": _ICON_A, "b.py": _ICON_B, "c.py": _CALL_B}
    )
    assert cli.main(["sanity", "a.py:icon", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "c.py:5" in out
    assert "(resolved to b.py::icon)" in out


def test_all_mode_labels_each_siblings_callers_for_the_other(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {"a.py": _ICON_A, "b.py": _ICON_B, "c.py": _CALL_B, "d.py": _CALL_A}
    )
    assert cli.main(["sanity", "--all", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["aggregate_causes"].get(sanity.CAUSE_RESOLVED_ELSEWHERE) == 2
    assert doc["aggregate_causes"].get(sanity.CAUSE_UNEXPLAINED) is None
    assert doc["flagged"] == []
