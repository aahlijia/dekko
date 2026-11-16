"""``sanity`` on a Java or Kotlin type and its constructors: a type
mention, a string mention, and a construction the map put on the
class or on another overload."""

import json
from collections import Counter
from pathlib import Path

import pytest

from dekko.analysis import sanity
from dekko.integrations import cli

from conftest import RepoFactory

TICKET_JAVA = (
    "package web;\n"
    "public class Ticket {\n"
    "    public Ticket(int a) { }\n"
    "    public Ticket(int a, String b) { }\n"
    "    public Ticket(int a, Object b) { }\n"
    "}\n"
)
OUTER_JAVA = (
    "package web;\n"
    "public class Outer {\n"
    "    public static class Inner { }\n"
    "}\n"
)
USE_JAVA = (
    "package app;\n"
    "import web.Ticket;\n"
    "import web.Outer;\n"
    "public class Use {\n"
    "    void run(Factory factory, Object o) {\n"
    "        Ticket x = factory.get();\n"
    '        Ticket.of("a");\n'
    "        Class<?> c = Ticket.class;\n"
    "        Object y = (Ticket) o;\n"
    "        boolean z = o instanceof Ticket;\n"
    "        Ticket[] arr = new Ticket[3];\n"
    '        log("see Ticket");\n'
    "        IntFunction<Ticket> s = Ticket::new;\n"
    "        new Ticket(1);\n"
    "        new Ticket(1, factory.get());\n"
    "        // Ticket is a thing\n"
    "        Object w = new Outer.Inner();\n"
    "    }\n"
    "    void f(Ticket n, List<Ticket> all) { }\n"
    "}\n"
)
JAVA_REPO = {
    "web/Ticket.java": TICKET_JAVA,
    "web/Outer.java": OUTER_JAVA,
    "app/Use.java": USE_JAVA,
}
USE = "app/Use.java"
TICKET_CLASS = "web/Ticket.java::Ticket"
# The (int, String) overload, which ties with (int, Object) on
# ``new Ticket(1, factory.get())`` (a call result has no type) and
# loses ``new Ticket(1)`` to the (int) one.
TICKET_CTOR = "web/Ticket.java:Ticket.Ticket:4"
TICKET_INT_CTOR = "web/Ticket.java:Ticket.Ticket:3"


def _rows(
    root: Path, target: str, capsys: pytest.CaptureFixture
) -> dict[tuple[str, int], str]:
    assert cli.main(["sanity", target, "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    return {(r["file"], r["line"]): r["cause"] for r in doc["grep_only"]}


def _in(rows: dict[tuple[str, int], str], path: str) -> dict[int, str]:
    return {line: cause for (p, line), cause in rows.items() if p == path}


@pytest.mark.parametrize("target", [TICKET_CLASS, TICKET_CTOR])
def test_java_type_mentions_are_explained_for_class_and_constructor(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture,
    target: str,
) -> None:
    root = make_mapped_repo(JAVA_REPO)
    rows = _in(_rows(root, target, capsys), USE)
    for line in (6, 7, 8, 9, 10, 11, 19):
        assert rows[line] == sanity.CAUSE_TYPE_MENTION, line
    assert rows[12] == sanity.CAUSE_STRING_MENTION
    # A constructor reference constructs: never a mention.
    assert rows[13] not in (
        sanity.CAUSE_TYPE_MENTION,
        sanity.CAUSE_STRING_MENTION,
    )
    assert rows[16] in (
        sanity.CAUSE_COMMENT_MENTION,
        sanity.CAUSE_COMMENT_ELSEWHERE,
    )


def test_java_nested_type_construction_is_a_mention_of_the_outer(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(JAVA_REPO)
    rows = _in(_rows(root, "web/Outer.java::Outer", capsys), USE)
    assert rows[17] == sanity.CAUSE_TYPE_MENTION


def test_constructor_tie_and_sibling_overload_rows(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(JAVA_REPO)
    rows = _in(_rows(root, TICKET_CTOR, capsys), USE)
    assert rows[13] == sanity.CAUSE_CONSTRUCTOR_REFERENCE
    assert rows[14] == sanity.CAUSE_SIBLING_CONSTRUCTOR
    assert rows[15] == sanity.CAUSE_CONSTRUCTOR_TIE


def test_single_target_and_all_agree_on_class_and_constructor(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(JAVA_REPO)
    single = {
        target: Counter(_rows(root, target, capsys).values())
        for target in (TICKET_CLASS, TICKET_INT_CTOR)
    }
    assert cli.main(["sanity", "--all", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    swept: dict[str, list[Counter]] = {}
    for r in doc["symbols"]:
        swept.setdefault(r["target"], []).append(Counter(r["causes"]))
    assert swept[TICKET_CLASS] == [single[TICKET_CLASS]]
    # Each overload is its own row; only the (int) one has a caller,
    # so only it is swept.
    ctors = [
        causes
        for target, rows in swept.items()
        if target.startswith("web/Ticket.java::Ticket.Ticket")
        for causes in rows
    ]
    assert ctors == [single[TICKET_INT_CTOR]]


def test_type_mention_is_off_for_a_name_shared_with_a_method(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "web/Status.java": (
                "package web;\npublic class Status {\n    int code;\n}\n"
            ),
            "web/Util.java": (
                "package web;\n"
                "public class Util {\n"
                "    static int Status() { return 1; }\n"
                "}\n"
            ),
            "app/Use.java": (
                "package app;\n"
                "import web.Status;\n"
                "public class Use {\n"
                "    void run() {\n"
                "        Status s = null;\n"
                "        Object t = new Status();\n"
                "    }\n"
                "}\n"
            ),
        }
    )
    # ``Status`` is a class and a method, so the mention rule stands
    # down in both modes.
    rows = _in(_rows(root, "web/Status.java::Status", capsys), "app/Use.java")
    assert rows[5] != sanity.CAUSE_TYPE_MENTION
    assert cli.main(["sanity", "--all", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    swept = {r["target"]: Counter(r["causes"]) for r in doc["symbols"]}
    assert sum(swept["web/Status.java::Status"].values()) >= 1
    assert sanity.CAUSE_TYPE_MENTION not in swept["web/Status.java::Status"]


KOTLIN_REPO = {
    "demo/Ticket.kt": (
        "package demo\n\nclass Ticket(val a: Int) {\n    fun x() = a\n}\n"
    ),
    "demo/Use.kt": (
        "package demo\n"
        "\n"
        "fun f(): Ticket {\n"
        "    val g = ::Ticket\n"
        "    val h = Ticket { }\n"
        '    println("${Ticket(2)}")\n'
        '    println("a Ticket")\n'
        "    return g(1)\n"
        "}\n"
    ),
}


def test_kotlin_type_mention_and_construction_shapes(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(KOTLIN_REPO)
    rows = _in(_rows(root, "demo/Ticket.kt::Ticket", capsys), "demo/Use.kt")
    assert rows[3] == sanity.CAUSE_TYPE_MENTION
    assert rows[4] == sanity.CAUSE_UNEXPLAINED
    # ``Ticket { }`` is a construction the map records as a call.
    assert rows.get(5) != sanity.CAUSE_TYPE_MENTION
    # A string template is code: neither a mention nor a string.
    assert rows.get(6) not in (
        sanity.CAUSE_TYPE_MENTION,
        sanity.CAUSE_STRING_MENTION,
    )
    assert rows[7] == sanity.CAUSE_STRING_MENTION


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("Ticket x = f();", True),
        ("List<Map<String, Ticket>> all;", True),
        ("@Ticket(value = 1) int x;", True),
        ("new Ticket(1);", False),
        ("new Ticket<>(1);", False),
        ("Supplier<Ticket> s = Ticket::new;", False),
        ('String s = "Ticket(";', False),
        ("int x; // Ticket", False),
    ],
)
def test_jvm_type_mention_shapes(snippet: str, expected: bool) -> None:
    assert (
        sanity._looks_like_jvm_type_mention(
            snippet, "Ticket", "A.java", target_is_type=True
        )
        is expected
    )


def test_jvm_type_mention_needs_a_type_target() -> None:
    assert not sanity._looks_like_jvm_type_mention(
        "Ticket x = f();", "Ticket", "A.java", target_is_type=False
    )
    assert not sanity._looks_like_jvm_type_mention(
        "Ticket x = f();", "Ticket", "a.ts", target_is_type=True
    )
