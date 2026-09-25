"""``query uses`` match kinds.

``externals_by_name`` keys on the last callee segment, so ``uses chalk``
used to say "no external reference matches" while 283 ``chalk.*`` call
sites sat in the map under ``red``/``dim``/``bold``. Three match kinds
now, plus a summary header and a not-found message that explains a
receiver that is really a local variable.
"""

import json
from pathlib import Path

import pytest

from dekko.integrations import cli
from dekko.integrations import server
from dekko.render import mapfile

from conftest import RepoFactory

TS = {
    # default import used via member calls
    "ui.ts": (
        "import chalk from 'chalk'\n"
        "export function warn(m: string): string {\n"
        "  return chalk.bold(chalk.red(m))\n"
        "}\n"
    ),
    # namespace import
    "comp.tsx": (
        "import * as React from 'react'\n"
        "export function C(): number {\n"
        "  const [n] = React.useState(0)\n"
        "  React.useEffect(() => {}, [])\n"
        "  return n\n"
        "}\n"
    ),
    # named import from a node: module, called bare
    "io.ts": (
        "import { existsSync, readFileSync } from 'node:fs'\n"
        "export function load(p: string): string {\n"
        "  return existsSync(p) ? readFileSync(p, 'utf8') : ''\n"
        "}\n"
    ),
    # a local variable that shares a module's name and is NOT imported
    "local.ts": (
        "export function rel(path: string): boolean {\n"
        "  return path.startsWith('./')\n"
        "}\n"
    ),
    # aliased import
    "alias.ts": (
        "import * as p from 'path'\n"
        "export function j(a: string): string {\n"
        "  return p.join(a, 'x')\n"
        "}\n"
    ),
    # type-only usage: imports React but never calls it
    "types.tsx": (
        "import * as React from 'react'\n"
        "export const Fc: React.FC = () => null\n"
    ),
}

PY = {
    "a.py": (
        "import subprocess\nimport numpy as np\n\n"
        "def go():\n"
        "    subprocess.run(['x'])\n"
        "    return np.array([1])\n"
    ),
    "b.py": "import json\n\ndef dump(x):\n    return json.dumps(x)\n",
}


def _uses(root: Path, *args: str) -> int:
    return cli.main(["query", "uses", *args, "--root", str(root)])


def _json(capsys: pytest.CaptureFixture) -> dict:
    return json.loads(capsys.readouterr().out)


def test_binding_match_finds_member_calls_through_a_default_import(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS)
    assert _uses(root, "chalk", "--json") == 0
    doc = _json(capsys)
    callees = sorted(e["callee"] for e in doc["results"])
    assert callees == ["chalk.bold", "chalk.red"]
    assert {e["match"] for e in doc["results"]} == {"binding"}
    assert doc["summary"]["sites"] == 2
    assert doc["summary"]["files"] == 1
    assert doc["summary"]["members"] == [["bold", 1], ["red", 1]]


def test_namespace_import_and_the_honest_denominator(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS)
    assert _uses(root, "React") == 0
    out = capsys.readouterr().out
    assert "React: 2 call sites in 1 files" in out
    assert "top members: useEffect 1, useState 1" in out
    # types.tsx imports React but only uses it in type position: the
    # header must say 2 files import it so 2 sites isn't read as all.
    assert "imported by 2 files; type-position, JSX" in out


def test_module_match_finds_bare_named_imports(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS)
    assert _uses(root, "fs", "--json") == 0
    doc = _json(capsys)
    assert sorted(e["callee"] for e in doc["results"]) == [
        "existsSync",
        "readFileSync",
    ]
    assert {e["match"] for e in doc["results"]} == {"module"}


@pytest.mark.parametrize("spelling", ["fs", "node:fs"])
def test_node_prefix_is_normalized_both_ways(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture,
    spelling: str,
) -> None:
    root = make_mapped_repo(TS)
    assert _uses(root, spelling, "--json") == 0
    assert _json(capsys)["summary"]["sites"] == 2


def test_aliased_module_import(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS)
    assert _uses(root, "path", "--json") == 0
    doc = _json(capsys)
    assert [e["callee"] for e in doc["results"]] == ["p.join"]
    assert doc["results"][0]["match"] == "module"


def test_import_gate_excludes_a_same_named_local(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # local.ts has a parameter named ``path`` and calls
    # ``path.startsWith``; it never imports ``path``. Not a use.
    root = make_mapped_repo(TS)
    assert _uses(root, "path", "--json") == 0
    doc = _json(capsys)
    assert all("startsWith" not in e["callee"] for e in doc["results"])


def test_not_found_explains_a_receiver_that_is_a_local(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    only_local = {"local.ts": TS["local.ts"]}
    root = make_mapped_repo(only_local)
    assert _uses(root, "path") == 3
    err = capsys.readouterr().err
    assert "no external reference matches 'path'" in err
    assert "appears as a receiver in 1 call site(s)" in err
    assert "local variables, not a module" in err


def test_close_names_include_import_sources(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS)
    assert _uses(root, "Chalk") == 3
    err = capsys.readouterr().err
    assert "chalk" in err.split("closest external names:", 1)[1]


def test_python_module_and_alias(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # The claude-code report asked whether this was TS-specific. No.
    root = make_mapped_repo(PY)
    assert _uses(root, "subprocess", "--json") == 0
    doc = _json(capsys)
    assert [e["callee"] for e in doc["results"]] == ["subprocess.run"]
    assert doc["results"][0]["match"] == "binding"

    assert _uses(root, "numpy", "--json") == 0
    doc = _json(capsys)
    assert [e["callee"] for e in doc["results"]] == ["np.array"]
    assert doc["results"][0]["match"] == "module"

    # base match is unchanged
    assert _uses(root, "dumps", "--json") == 0
    assert _json(capsys)["results"][0]["match"] == "base"


def test_a_row_matched_two_ways_appears_once_as_base(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # ``json.dumps`` under ``uses json``: binding match. A single-
    # segment callee whose head and base are the same string must
    # land once, labelled base.
    root = make_mapped_repo(
        {
            "c.py": (
                "from pathlib import Path\n\ndef mk():\n    return Path('.')\n"
            )
        }
    )
    assert _uses(root, "Path", "--json") == 0
    doc = _json(capsys)
    assert len(doc["results"]) == 1
    assert doc["results"][0]["match"] == "base"


def test_whitespace_in_a_stored_chain_does_not_hide_the_head(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "z.ts": (
                "import { z } from 'zod'\n"
                "export const S = z\n"
                "  .object({ a: z.string() })\n"
            )
        }
    )
    index = mapfile.load_map(root)
    assert index is not None
    stored = [
        e.callee for exts in index.externals_by_name.values() for e in exts
    ]
    # The chain is stored canonically, with the line break and its
    # indentation gone (``z.object``, not ``z .object``), so the head
    # is found without any whitespace stripping at lookup time.
    assert "z.object" in stored, stored
    assert not any(" " in c for c in stored), stored
    assert _uses(root, "z", "--json") == 0
    doc = _json(capsys)
    assert doc["summary"]["sites"] == 2


def test_without_tests_view_rebuilds_the_head_index(
    make_mapped_repo: RepoFactory,
) -> None:
    files = dict(TS)
    files["ui.test.ts"] = (
        "import chalk from 'chalk'\nexport function t() { chalk.dim('x') }\n"
    )
    root = make_mapped_repo(files)
    index = mapfile.load_map(root)
    assert index is not None
    assert len(index.externals_by_head["chalk"]) == 3
    assert len(index.without_tests().externals_by_head["chalk"]) == 2


def test_mcp_find_usages_round_trip(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(TS)
    ctx = server.Context(default_root=root, no_regen=False)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "find_usages", "arguments": {"name": "chalk"}},
    }
    result = server.handle(ctx, msg)["result"]
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "chalk: 2 call sites in 1 files" in text
    assert "[chalk.red]" in text


# --- canonical receiver text in rows and in the not-found hint ------


def test_uses_rows_show_canonical_chain_text(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "t.ts": (
                "import chalk from 'chalk'\n"
                "export function paint() {\n"
                "  return chalk.hex('#fff').bold('x')\n"
                "}\n"
            )
        }
    )
    assert _uses(root, "chalk") == 0
    out = capsys.readouterr().out
    assert "[chalk.hex().bold]" in out
    assert "#fff" not in out


def test_uses_not_found_hint_skips_markers_and_fragments(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "t.ts": (
                "import { expect } from 'vitest'\n"
                "export function check(result: { ok: boolean }) {\n"
                "  expect(result.ok).toBe(true)\n"
                "  expectation(result)\n"
                "}\n"
            )
        }
    )
    # ``expect()`` (the canonical head of ``expect().toBe``) would be
    # the closest match for ``expec``; the floor drops it and leaves
    # the real names.
    assert _uses(root, "expec") == 3
    err = capsys.readouterr().err
    hint = next(
        (line for line in err.splitlines() if "closest external" in line),
        "",
    )
    assert "expect" in hint
    assert "expect()" not in hint
    assert "(" not in hint


def test_suggestable_floor() -> None:
    from dekko.analysis.query import _suggestable

    assert _suggestable("node:fs")
    assert _suggestable("@scope/pkg")
    assert _suggestable("./local")
    assert _suggestable("chalk")
    assert not _suggestable("expect()")
    assert not _suggestable("expect(result")
    assert not _suggestable("[]")
    assert not _suggestable('""')
    assert not _suggestable("z .object")
    assert not _suggestable("a" * 41)
