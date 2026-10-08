"""``sanity`` reads a name that only sits in string text as a string
mention in Rust, Python and Go, as it already did for JS/TS and the
JVM languages; interpolated names stay code."""

import json
from pathlib import Path

import pytest

from dekko.analysis import sanity
from dekko.integrations import cli

from conftest import RepoFactory


def _sanity_json(
    root: Path, target: str, capsys: pytest.CaptureFixture
) -> dict[tuple[str, int], dict]:
    code = cli.main(
        ["sanity", target, "--root", str(root), "--json", "--include-tests"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)

    return {(row["file"], row["line"]): row for row in doc["grep_only"]}


def _cause(rows: dict[tuple[str, int], dict], loc: tuple[str, int]) -> str:
    return rows[loc]["cause"] if loc in rows else "(not grep-only)"


STRING = sanity.CAUSE_STRING_MENTION

RUST_REPO = {
    "src/sched.rs": "pub struct Task(u8);\n",
    "src/use.rs": (
        'pub fn a() { log::info!("Task trace saved"); }\n'
        'pub fn b() -> &\'static str { r#"the Task"# }\n'
        'pub fn c() -> &\'static [u8] { b"Task" }\n'
        'pub fn d() -> String { format!("{Task}") }\n'
        "fn e<'a>(s: &'a str) -> &'a str { r#\"Task\"# }\n"
        "pub fn f() -> &'static str { r#\"spans\n"
        'a Task line"# }\n'
    ),
}


def test_rust_string_text_is_a_string_mention(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(RUST_REPO), "src/sched.rs::Task", capsys
    )
    for line in (1, 2, 3, 5, 7):
        assert _cause(rows, ("src/use.rs", line)) == STRING, line


def test_rust_format_argument_is_code(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(RUST_REPO), "src/sched.rs::Task", capsys
    )
    assert _cause(rows, ("src/use.rs", 4)) != STRING


PY_REPO = {
    "pkg/cmd.py": "def command():\n    return 1\n",
    "pkg/run.py": (
        "def go(e, command):\n"
        '    log(f"Running command: {e}")\n'
        '    log(f"{command}")\n'
        '    """Run a command\n'
        "    safely, the command way.\n"
        '    """\n'
        "    return command\n"
    ),
}


def test_python_string_text_and_docstring_lines_are_string_mentions(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(PY_REPO), "pkg/cmd.py::command", capsys
    )
    for line in (2, 5):
        assert _cause(rows, ("pkg/run.py", line)) == STRING, line
    # A docstring's opening line already reads as a comment.
    assert _cause(rows, ("pkg/run.py", 4)) in {
        STRING,
        sanity.CAUSE_COMMENT_MENTION,
        sanity.CAUSE_COMMENT_ELSEWHERE,
    }


def test_python_f_string_field_and_code_after_a_docstring_stay_code(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(PY_REPO), "pkg/cmd.py::command", capsys
    )
    for line in (3, 7):
        assert _cause(rows, ("pkg/run.py", line)) != STRING, line


GO_REPO = {
    "main.go": "package main\n\ntype Link struct{}\n",
    "other.go": (
        "package main\n"
        'var s = " **Link consistency**: ok"\n'
        "var r = `raw Link`\n"
        "var l Link\n"
    ),
}


def test_go_string_and_raw_string_text_are_string_mentions(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(make_mapped_repo(GO_REPO), "main.go::Link", capsys)
    assert _cause(rows, ("other.go", 2)) == STRING
    assert _cause(rows, ("other.go", 3)) == STRING
    assert _cause(rows, ("other.go", 4)) != STRING
