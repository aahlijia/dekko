"""No read command may print a row that outruns the budget.

``fit_to_budget`` can only drop whole rows, and always keeps one, so
``--budget`` is only a promise while rows are small. This is the audit
that found a 122,327-character ``query uses`` row, turned into CI: a
fixture repo seeded with a 5,000-character chained call, a long
``called by`` list, and a wide import cluster, run through every read
command, asserting the longest printed line stays under
``ROW_CHAR_CAP``. Commands whose rows are real signatures are
exempted explicitly so the exemption is a visible decision.
"""

import json
from pathlib import Path

import pytest

from dekko.integrations import cli
from dekko.textutil import ROW_CHAR_CAP

from conftest import RepoFactory

_CHAIN = (
    "program.name('x')"
    + "".join(f".option('--flag{i}', 'help text {i}')" for i in range(160))
    + ".version"
)
assert len(_CHAIN) > 5000

_CALLERS = 40


# Enough ordinary lines that the walker's minified-content heuristic
# (average line length over the first 50 lines) doesn't skip the file:
# a real repo's builder chain is one call in a big file, not the file.
_PADDING = "".join(f"// line {i}\n" for i in range(60))


def _repo() -> dict[str, str]:
    files = {
        "cli.ts": (
            f"{_PADDING}"
            "import { program } from 'commander'\n"
            "export function run(): void {\n"
            f"  {_CHAIN}('1.0')\n"
            "}\n"
        ),
        "hub.ts": "export function hub(): number {\n  return 1\n}\n",
    }
    for i in range(_CALLERS):
        files[f"c{i}.ts"] = (
            "import { hub } from './hub'\n"
            f"export function caller{i}(): number {{\n  return hub()\n}}\n"
        )
    return files


READ_COMMANDS = [
    ["query", "uses", "version"],
    ["query", "callers", "hub.ts:hub", "--sites"],
    ["query", "callees", "cli.ts:run"],
    ["query", "symbol", "hub.ts:hub"],
    ["query", "throws", "cli.ts:run"],
    ["context", "hub.ts:hub", "--hops", "1"],
    ["search", "run the program"],
    ["unused"],
    ["ambiguous"],
    ["deps", "--cycles"],
    ["deps"],
    ["summary"],
    ["stats"],
]


@pytest.mark.parametrize("argv", READ_COMMANDS, ids=lambda a: " ".join(a))
def test_no_read_command_prints_an_oversized_row(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    root = make_mapped_repo(_repo())
    cli.main([*argv, "--root", str(root)])
    out = capsys.readouterr().out
    longest = max((len(line) for line in out.splitlines()), default=0)
    assert longest <= ROW_CHAR_CAP, out[:400]


def test_uses_row_shows_the_bounded_chain_and_still_matches(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    # The chain is stored canonically (arguments elided) and, past the
    # extractor's hop cap, as head, ellipsis and name; the row is
    # found by its base name and needs no render-time clipping. The
    # render clip (``textutil.clip_middle``) stays for maps written
    # before canonical storage and has its own unit tests.
    root = make_mapped_repo(_repo())
    assert cli.main(["query", "uses", "version", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "[program.….version]" in out
    assert "top members: version 1" in out
    assert "chars]…" not in out
    assert "over --budget" not in out

    assert (
        cli.main(["query", "uses", "version", "--root", str(root), "--json"])
        == 0
    )
    doc = json.loads(capsys.readouterr().out)
    entry = doc["results"][0]
    assert entry["callee"] == "program.….version"
    assert "callee_truncated" not in entry
    assert doc["meta"]["over_budget"] is False


def test_map_pages_cap_inline_link_lists(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_repo())
    pages = list((root / ".dekko").glob("**/*.md"))
    text = "\n".join(p.read_text() for p in pages)
    line = next(ln for ln in text.splitlines() if "**called by:**" in ln)
    assert line.count("](") == 25
    assert f"+{_CALLERS - 25} more (`dekko query callers hub.ts:hub`)" in line


def test_map_pages_do_not_cap_short_link_lists(
    make_mapped_repo: RepoFactory,
) -> None:
    files = {"hub.py": "def hub():\n    return 1\n"}
    for i in range(25):
        files[f"c{i}.py"] = (
            f"from hub import hub\ndef caller{i}():\n    return hub()\n"
        )
    root = make_mapped_repo(files)
    text = "\n".join(p.read_text() for p in (root / ".dekko").glob("**/*.md"))
    line = next(ln for ln in text.splitlines() if "**called by:**" in ln)
    assert line.count("](") == 25
    assert "more" not in line


def test_long_signatures_are_the_allowed_exception(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    # A 600-character destructured-props signature is content, not a
    # producer bug; ``outline`` prints it whole and this test pins that
    # the exemption is deliberate rather than an oversight.
    params = ", ".join(f"prop{i}: string" for i in range(60))
    root = make_mapped_repo(
        {
            "big.ts": (
                f"{_PADDING}export function Big({{ {params} }}): void {{}}\n"
            )
        }
    )
    assert cli.main(["outline", "big.ts", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert max(len(line) for line in out.splitlines()) > ROW_CHAR_CAP
    assert "prop59: string" in out


def test_fixture_chain_is_stored_bounded(
    make_mapped_repo: RepoFactory,
) -> None:
    # The 5,000-character source chain never reaches ``map.json``: the
    # extractor stores callee text without arguments and caps a chain
    # of many hops, so the longest interned id is a symbol id or a
    # path, never a call chain. The read-command rows above are short
    # because the data is, not because a renderer rescued them.
    root = make_mapped_repo(_repo())
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    ids = doc.get("ids") or []
    longest = max(len(s) for s in ids) if ids else 0
    assert longest < 200, "an unbounded callee id reached map.json"
    assert Path(root / ".dekko" / "map.json").exists()
