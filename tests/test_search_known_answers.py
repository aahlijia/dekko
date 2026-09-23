"""The ``dekko search`` known-answer benchmark runs and scores correctly."""

import json
import sys
from pathlib import Path

from conftest import RepoFactory

# benchmarks/ is intentionally outside the package; reach it via path.
_BENCH = Path(__file__).parent.parent / "benchmarks"
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))

import search_known_answers as bench  # noqa: E402


def test_committed_fixture_is_well_formed() -> None:
    fixture = json.loads(bench.FIXTURE.read_text())
    repos = fixture["repos"]
    assert len(fixture["queries"]) >= 10
    assert sum(q["hub"] for q in fixture["queries"]) >= 3
    for q in fixture["queries"]:
        assert q["repo"] in repos
        assert q["answers"]
        assert all(":" in a for a in q["answers"])


def test_run_queries_ranks_the_known_answer(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(
        {
            "billing.py": (
                "def compute_invoice_total(items: list) -> int:\n"
                '    """Sum line items into an invoice total."""\n'
                "    return sum(items)\n"
                "\n"
                "\n"
                "def send_email(to: str) -> None:\n"
                '    """Deliver a message."""\n'
            ),
        }
    )
    fixture = root / "known.json"
    fixture.write_text(
        json.dumps(
            {
                "repos": {root.name: {"commit": "x"}},
                "queries": [
                    {
                        "repo": root.name,
                        "query": "invoice total from line items",
                        "answers": ["billing.py:compute_invoice_total"],
                        "hub": False,
                    },
                    {
                        "repo": root.name,
                        "query": "invoice total from line items",
                        "answers": ["billing.py:no_such_symbol"],
                        "hub": True,
                    },
                ],
            }
        )
    )

    results = bench.run_queries(root.parent, fixture_path=fixture)

    assert [r.rank for r in results] == [1, None]
    assert bench.summarize(results) == {
        "queries": 2,
        "top1": 1,
        "mrr": 0.5,
    }
