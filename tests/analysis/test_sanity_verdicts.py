"""``sanity`` labels a qualified call by what dekko knows about the
line: a comment, a string or an unparsed file never reaches the
resolver, and a call it did see carries the resolver's own verdict."""

import json
from pathlib import Path

import pytest

from dekko.analysis import sanity
from dekko.integrations import cli

from conftest import RepoFactory

QUALIFIED_FAMILY = {
    sanity.CAUSE_QUALIFIED_CALL,
    sanity.CAUSE_RECORDED_EXTERNAL,
    sanity.CAUSE_RECORDED_AMBIGUOUS,
}

ORDER_REPO = {
    "src/conn.ts": "export class Conn {\n  close(): void {}\n}\n",
    # A second ``close`` keeps ``x.close()`` from resolving to Conn's.
    "src/file.ts": "export class File {\n  close(): void {}\n}\n",
    "src/use.ts": (
        "// ccr.close() ran first\n"
        'const s = "call x.close() first";\n'
        "export function go(x: any): void {\n"
        "  x.close();\n"
        "}\n"
    ),
    "docs/guide.md": "Call `conn.close()` when done.\n",
}


def _sanity_json(
    root: Path, target: str, capsys: pytest.CaptureFixture
) -> dict[tuple[str, int], dict]:
    code = cli.main(["sanity", target, "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)

    return {(row["file"], row["line"]): row for row in doc["grep_only"]}


def test_comment_string_and_unparsed_lines_are_not_blind_spots(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(ORDER_REPO), "src/conn.ts::Conn.close", capsys
    )
    assert rows[("src/use.ts", 1)]["cause"] in {
        sanity.CAUSE_COMMENT_MENTION,
        sanity.CAUSE_COMMENT_ELSEWHERE,
    }
    assert rows[("src/use.ts", 2)]["cause"] == sanity.CAUSE_STRING_MENTION
    assert rows[("docs/guide.md", 1)]["cause"] == (
        sanity.CAUSE_UNSUPPORTED_LANGUAGE
    )


def test_a_real_qualified_call_keeps_a_call_label(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(ORDER_REPO), "src/conn.ts::Conn.close", capsys
    )
    assert rows[("src/use.ts", 4)]["cause"] in QUALIFIED_FAMILY


VERDICT_REPO = {
    "a.ts": "export class A {\n  add(x: number): void {}\n}\n",
    "b.ts": "export class B {\n  add(x: number): void {}\n}\n",
    "c.ts": (
        "export class C {\n  entries(): number[] {\n    return [];\n  }\n}\n"
    ),
    "use.ts": (
        "export function go(seen: any, o: any): void {\n"
        "  seen.add(1);\n"
        "  Object.entries(o);\n"
        "}\n"
    ),
    # Typed callers, so ``--all`` sweeps both targets.
    "typed.ts": (
        'import { A } from "./a";\n'
        'import { C } from "./c";\n'
        "export function h(a: A, c: C): void {\n"
        "  a.add(2);\n"
        "  c.entries();\n"
        "}\n"
    ),
}


def test_ambiguous_call_says_the_resolver_picked_none(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(make_mapped_repo(VERDICT_REPO), "a.ts::A.add", capsys)
    assert rows[("use.ts", 2)]["cause"] == sanity.CAUSE_RECORDED_AMBIGUOUS


def test_external_call_names_its_callee(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(VERDICT_REPO), "c.ts::C.entries", capsys
    )
    assert rows[("use.ts", 3)]["cause"] == sanity.CAUSE_RECORDED_EXTERNAL
    assert rows[("use.ts", 3)]["external_callee"] == "Object.entries"


def test_verdicts_agree_across_modes(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(VERDICT_REPO)
    assert cli.main(["sanity", "--all", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    causes = {r["target"]: r["causes"] for r in doc["symbols"]}
    for target, cause in (
        ("a.ts::A.add", sanity.CAUSE_RECORDED_AMBIGUOUS),
        ("c.ts::C.entries", sanity.CAUSE_RECORDED_EXTERNAL),
    ):
        single = _sanity_json(root, target, capsys)
        assert causes[target].get(cause) == 1
        assert sum(r["cause"] == cause for r in single.values()) == 1


CURSOR_REPO = {
    "cursor.ts": (
        "export class Cursor {\n"
        "  static fromText(s: string): Cursor {\n"
        "    return new Cursor();\n"
        "  }\n"
        "}\n"
    ),
    "use.ts": (
        'import { Cursor } from "./cursor";\n'
        'export const c = Cursor.fromText("a");\n'
        "export const d = new Cursor();\n"
    ),
}


def test_static_member_access_names_the_class_as_receiver(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(CURSOR_REPO), "cursor.ts::Cursor", capsys
    )
    assert rows[("use.ts", 2)]["cause"] == (
        sanity.CAUSE_STATIC_MEMBER_REFERENCE
    )
    assert ("use.ts", 3) not in rows


def test_text_mode_names_the_external_callee(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(VERDICT_REPO)
    assert cli.main(["sanity", "c.ts::C.entries", "--root", str(root)]) == 0
    assert "(external: Object.entries)" in capsys.readouterr().out
