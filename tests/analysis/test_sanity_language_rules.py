"""``sanity`` rules that follow from a language itself: a line in
another language can't call the target, and (below) a bare identifier
can't be a method, a Rust type position doesn't construct the type."""

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


OTHER_LANGUAGE_REPO = {
    "src/Error.java": "public class Error {\n}\n",
    "src/Main.java": ("public class Main {\n    Object e = new Error();\n}\n"),
    "static/js/app.js": 'throw new Error("x");\n',
    "src/Use.kt": "fun f() {\n    val e = Error()\n}\n",
    "build.gradle.kts": "val boom = Error()\n",
    "tools/check.py": "# Error is raised here\n",
}


def test_a_line_in_another_language_says_so(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(OTHER_LANGUAGE_REPO), "src/Error.java::Error", capsys
    )
    row = rows[("static/js/app.js", 1)]
    assert row["cause"] == sanity.CAUSE_OTHER_LANGUAGE
    assert row["languages"] == {"line": "javascript", "target": "java"}


def test_same_family_lines_are_never_another_language(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(OTHER_LANGUAGE_REPO), "src/Error.java::Error", capsys
    )
    for loc in (("src/Use.kt", 2), ("build.gradle.kts", 1)):
        assert _cause(rows, loc) != sanity.CAUSE_OTHER_LANGUAGE


def test_a_non_call_label_in_another_language_stays(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(OTHER_LANGUAGE_REPO), "src/Error.java::Error", capsys
    )
    assert _cause(rows, ("tools/check.py", 1)) in {
        sanity.CAUSE_COMMENT_MENTION,
        sanity.CAUSE_COMMENT_ELSEWHERE,
    }


def test_typescript_variants_are_one_family(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(
            {
                "src/a.ts": "export function boot(): void {}\n",
                "src/b.mts": "boot();\n",
                "src/c.d.ts": "declare function wrap(f: typeof boot): void;\n",
                "src/d.py": "boot()\n",
            }
        ),
        "src/a.ts::boot",
        capsys,
    )
    assert _cause(rows, ("src/b.mts", 1)) != sanity.CAUSE_OTHER_LANGUAGE
    assert _cause(rows, ("src/c.d.ts", 1)) != sanity.CAUSE_OTHER_LANGUAGE
    assert _cause(rows, ("src/d.py", 1)) == sanity.CAUSE_OTHER_LANGUAGE


def test_an_unknown_grammar_on_either_side_is_skipped(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(
            {
                "lib/util.lua": "function helper()\n  return 1\nend\n",
                "lib/main.lua": "helper()\n",
                "notes/todo.xyz": "call helper() later\n",
            }
        ),
        "lib/util.lua::helper",
        capsys,
    )
    assert _cause(rows, ("notes/todo.xyz", 1)) == (
        sanity.CAUSE_UNSUPPORTED_LANGUAGE
    )


def test_text_mode_names_both_languages(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(OTHER_LANGUAGE_REPO)
    code = cli.main(
        [
            "sanity",
            "src/Error.java::Error",
            "--root",
            str(root),
            "--limit",
            "50",
        ]
    )
    assert code == 0
    assert "(javascript line, java target)" in capsys.readouterr().out


MAY_STILL_MISS = sanity._REMAINING_CAUSES | sanity._QUALIFIED_FAMILY

RUST_METHOD_REPO = {
    "src/lib.rs": (
        "pub struct B;\n"
        "impl B {\n"
        "    pub fn builder(&self) -> u8 {\n"
        "        1\n"
        "    }\n"
        "}\n"
    ),
    "src/use.rs": (
        "pub fn run(x: Thing) {\n"
        "    let builder = make();\n"
        '    builder.push_str("x");\n'
        "    let v = Foo { builder };\n"
        "    x.builder();\n"
        "    let f = B::builder;\n"
        "    x.builder(builder);\n"
        "}\n"
        "pub fn harden(h: builder) {}\n"
    ),
}


def test_rust_bare_identifier_is_not_the_method(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(RUST_METHOD_REPO), "src/lib.rs::B.builder", capsys
    )
    for line in (3, 4, 9):
        assert _cause(rows, ("src/use.rs", line)) == (
            sanity.CAUSE_BARE_IDENTIFIER_NOT_METHOD
        ), line
    assert _cause(rows, ("src/use.rs", 2)) not in MAY_STILL_MISS


def test_rust_method_call_or_path_is_never_a_bare_identifier(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(RUST_METHOD_REPO), "src/lib.rs::B.builder", capsys
    )
    for line in (5, 6, 7):
        assert _cause(rows, ("src/use.rs", line)) != (
            sanity.CAUSE_BARE_IDENTIFIER_NOT_METHOD
        ), line


PY_METHOD_REPO = {
    "pkg/w.py": (
        "class W:\n"
        "    @property\n"
        "    def name(self):\n"
        "        return 1\n"
        "\n"
        "    @name.setter\n"
        "    def name(self, v):\n"
        "        pass\n"
    ),
    "pkg/use.py": (
        "def go(w):\n    name = 3\n    print(name)\n    return w.name\n"
    ),
}


def test_python_bare_name_outside_the_class_is_not_the_method(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(PY_METHOD_REPO), "pkg/w.py::W.name", capsys
    )
    assert _cause(rows, ("pkg/use.py", 3)) == (
        sanity.CAUSE_BARE_IDENTIFIER_NOT_METHOD
    )
    assert _cause(rows, ("pkg/use.py", 4)) != (
        sanity.CAUSE_BARE_IDENTIFIER_NOT_METHOD
    )
    assert _cause(rows, ("pkg/w.py", 6)) != (
        sanity.CAUSE_BARE_IDENTIFIER_NOT_METHOD
    )


def test_java_bare_call_can_be_the_method(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(
            {
                "J.java": (
                    "class J {\n"
                    "    void name() {}\n"
                    "    void g() { name(); }\n"
                    "}\n"
                ),
                "K.java": "class K {\n    void h() { name(1); }\n}\n",
            }
        ),
        "J.java::J.name",
        capsys,
    )
    assert _cause(rows, ("K.java", 2)) != (
        sanity.CAUSE_BARE_IDENTIFIER_NOT_METHOD
    )


RUST_TYPE_REPO = {
    "src/sched.rs": "pub struct Task<T>(T);\n",
    "src/use.rs": (
        "fn f() -> Option<Task<()>> { None }\n"
        "pub fn g() { let t = Task::ready(1); }\n"
        "type Item = Task<u8>;\n"
        "pub fn h() { let t = Task(3); }\n"
        "pub fn k(t: Task<u8>) -> u8 { match t { Task(x) => x } }\n"
    ),
    "src/m.rs": "pub struct Marker;\n",
    "src/use_m.rs": "pub fn a() { let m = Marker; }\n",
}


def test_rust_type_positions_are_type_mentions(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(RUST_TYPE_REPO), "src/sched.rs::Task", capsys
    )
    for line in (1, 2):
        assert _cause(rows, ("src/use.rs", line)) == (
            sanity.CAUSE_TYPE_MENTION
        ), line
    assert _cause(rows, ("src/use.rs", 3)) not in MAY_STILL_MISS


def test_rust_construction_is_never_a_type_mention(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(
            {
                "src/sched.rs": "pub struct Task<T>(T);\n",
                "src/a.rs": (
                    "pub fn h(u: Unknown) { let t = u.wrap(Task(3)); }\n"
                ),
                "src/b.rs": "pub fn k() { let t = Task { 0: 1 }; }\n",
            }
        ),
        "src/sched.rs::Task",
        capsys,
    )
    for loc in (("src/a.rs", 1), ("src/b.rs", 1)):
        assert _cause(rows, loc) != sanity.CAUSE_TYPE_MENTION, loc


def test_rust_unit_struct_value_is_not_judged(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(RUST_TYPE_REPO), "src/m.rs::Marker", capsys
    )
    assert _cause(rows, ("src/use_m.rs", 1)) != sanity.CAUSE_TYPE_MENTION


def test_an_unparsed_file_keeps_its_label_under_every_language_rule(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    repo = dict(RUST_TYPE_REPO)
    repo["docs/notes.md"] = "Return `Task<()>` from every spawn.\n"
    repo["docs/more.md"] = "Pass `builder` along.\n"
    repo.update(RUST_METHOD_REPO)
    root = make_mapped_repo(repo)
    rows = _sanity_json(root, "src/sched.rs::Task", capsys)
    assert _cause(rows, ("docs/notes.md", 1)) == (
        sanity.CAUSE_UNSUPPORTED_LANGUAGE
    )
    rows = _sanity_json(root, "src/lib.rs::B.builder", capsys)
    assert _cause(rows, ("docs/more.md", 1)) == (
        sanity.CAUSE_UNSUPPORTED_LANGUAGE
    )
