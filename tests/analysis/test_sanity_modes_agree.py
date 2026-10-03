"""``sanity <target>`` and ``sanity --all`` classify from the same
facts, and ``query callers`` lists the value references the
``sanity`` pointer sends you to."""

import json
from collections import Counter
from pathlib import Path

import pytest

from dekko.analysis import sanity
from dekko.integrations import cli
from dekko.render import mapfile

from conftest import RepoFactory

REPO = {
    "src/widget.ts": (
        "export class Widget {\n"
        "  active(n: number): boolean {\n"
        "    return n > 0;\n"
        "  }\n"
        "}\n"
    ),
    "src/use.ts": (
        'import { Widget } from "./widget";\n'
        "export function run(w: Widget): boolean {\n"
        "  return w.active(1);\n"
        "}\n"
    ),
    "src/local.ts": (
        "export function check(flag: boolean): number {\n"
        "  const active = flag;\n"
        "  if (active) {\n"
        "    return 1;\n"
        "  }\n"
        "  return 0;\n"
        "}\n"
    ),
    "src/other.ts": (
        "export function go(x: any): void {\n  x.other(); active(1);\n}\n"
    ),
    "test/widget.test.ts": (
        "function active(): boolean {\n"
        "  return true;\n"
        "}\n"
        'it("works", () => {\n'
        "  expect(active()).toBe(true);\n"
        "});\n"
    ),
    "src/refresh.ts": (
        "export function doRefresh(): void {\n"
        "  setTimeout(doRefresh, 1000);\n"
        "}\n"
        "export function start(): void {\n"
        "  doRefresh();\n"
        "  setTimeout(doRefresh, 10);\n"
        "}\n"
    ),
}
ACTIVE = "src/widget.ts::Widget.active"
REFRESH = "src/refresh.ts::doRefresh"


def _single(
    root: Path, target: str, capsys: pytest.CaptureFixture
) -> dict[tuple[str, int], str]:
    assert cli.main(["sanity", target, "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    return {(r["file"], r["line"]): r["cause"] for r in doc["grep_only"]}


def _all(root: Path, capsys: pytest.CaptureFixture) -> dict[str, Counter]:
    assert cli.main(["sanity", "--all", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    return {r["target"]: Counter(r["causes"]) for r in doc["symbols"]}


def test_query_callers_lists_references_after_callers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    code = cli.main(
        ["query", "callers", REFRESH, "--sites", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "src/refresh.ts:5" in out
    head, _, tail = out.partition("referenced (not called):")
    assert "src/refresh.ts:5" in head
    assert "src/refresh.ts:6" in tail


def test_query_callers_json_carries_callers_and_references(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    code = cli.main(
        ["query", "callers", REFRESH, "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in doc["results"]] == ["src/refresh.ts::start"]
    assert [r["id"] for r in doc["referenced_not_called"]] == [
        "src/refresh.ts::start"
    ]
    assert doc["referenced_meta"]["returned"] == 1


def test_referenced_section_obeys_the_row_cap(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "src/cb.ts": "export function cb(): void {}\n",
        **{
            f"src/w{i}.ts": (
                'import { cb } from "./cb";\n'
                f"export function w{i}(): void {{\n"
                "  setTimeout(cb, 1);\n"
                "}\n"
            )
            for i in range(5)
        },
    }
    root = make_mapped_repo(files)
    code = cli.main(
        [
            "query",
            "callers",
            "src/cb.ts::cb",
            "--limit",
            "2",
            "--root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["referenced_not_called"]) == 2
    assert doc["referenced_meta"]["total"] == 5
    code = cli.main(
        [
            "query",
            "callers",
            "src/cb.ts::cb",
            "--limit",
            "2",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert out.count("  src/w") == 2
    assert "3 of 5" in out


def test_value_reference_cause_points_at_query_callers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    rows = _single(root, REFRESH, capsys)
    assert rows[("src/refresh.ts", 6)] == sanity.CAUSE_VALUE_REFERENCE
    assert "dekko query callers" in sanity.CAUSE_VALUE_REFERENCE
    assert "query uses" not in sanity.CAUSE_VALUE_REFERENCE


def test_uses_refusal_points_at_query_callers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    code = cli.main(["query", "uses", "doRefresh", "--root", str(root)])
    assert code != 0
    err = capsys.readouterr().err
    assert "query callers doRefresh" in err
    assert "value references" in err


def test_mention_inside_own_body_is_a_self_edge(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    rows = _single(root, REFRESH, capsys)
    assert rows[("src/refresh.ts", 2)] == sanity.CAUSE_SELF_MENTION


def test_method_rows_read_the_same_in_both_modes(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    rows = _single(root, ACTIVE, capsys)
    # A same-named local is a local, not a library method.
    assert rows[("src/local.ts", 3)] == sanity.CAUSE_SHADOWING_LOCAL
    # A test file's own helper belongs to the test filter.
    assert rows[("test/widget.test.ts", 5)] == sanity.CAUSE_TEST_FILTER
    # A call in a file that never names the class is the one row the
    # receiver label is for.
    assert rows[("src/other.ts", 2)] == sanity.CAUSE_LIKELY_EXTERNAL_COLLISION
    swept = _all(root, capsys)
    assert swept["src/widget.ts:Widget.active"] == Counter(rows.values())


def test_every_fan_in_symbol_agrees_across_modes(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    swept = _all(root, capsys)
    index = mapfile.load_map(root)
    assert index is not None
    query_index = index.without_tests()
    checked = 0
    for syms in sanity._group_fan_in_symbols(query_index).values():
        for sym in syms:
            single = Counter(_single(root, sym.id, capsys).values())
            assert swept[f"{sym.path}:{sym.qualname}"] == single, sym.id
            checked += 1
    assert checked >= 2


def test_cross_file_collision_is_decided_per_target(
    make_mapped_repo: RepoFactory,
) -> None:
    # Two files each declare ``load``. A call in a.ts is a collision
    # for b.ts's ``load``, never for a.ts's own.
    root = make_mapped_repo(
        {
            "a.ts": "export function load(): number {\n  return 1;\n}\n",
            "b.ts": "export function load(): number {\n  return 2;\n}\n",
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    loads = {s.path: s for s in index.symbols_by_name["load"]}
    for target, expected in (
        ("a.ts", None),
        ("b.ts", sanity.CAUSE_CROSS_FILE_COLLISION),
    ):
        facts = sanity._target_facts(index, loads[target])
        assert (
            sanity._shape_target_cause(
                root,
                facts,
                ("a.ts", 9),
                "  load();",
                sanity.CAUSE_UNEXPLAINED,
            )
            == expected
        )
