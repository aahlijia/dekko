"""Round 33 Track 5: ``sanity``'s explanations for same-named locals.

Tier 1: the ``x: Name`` colon template is a type annotation only when
the target *is* a type; for a function target ``error: errorMessage,``
is an object-literal value. Tier 2: a value-position use of a local
declared earlier in the enclosing function is explained as such,
under four guards, instead of landing in "unexplained".
"""

import json
from pathlib import Path

import pytest

from dekko.analysis import sanity
from dekko.core.model import Symbol
from dekko.integrations import cli

from conftest import RepoFactory

# The claude-code repro (ide.ts:40/508/613/617), condensed. The import
# is real, line 4 calls it, the catch block declares a local of the
# same name and line 9 uses that local in an object literal.
SHADOW_REPO = {
    "errors.ts": (
        "export function errorMessage(e: unknown): string {\n"
        "  return String(e)\n"
        "}\n"
    ),
    "ide.ts": (
        "import { errorMessage } from './errors'\n"
        "export function probe(e: unknown): { error: string } {\n"
        "  try {\n"
        "    return { error: errorMessage(e) }\n"
        "  } catch (err) {\n"
        "    const errorMessage = err instanceof Error ? err.message : 'x'\n"
        "    return {\n"
        "      kind: 'fail',\n"
        "      error: errorMessage,\n"
        "    }\n"
        "  }\n"
        "}\n"
    ),
}


# --- tier 1 ------------------------------------------------------------


def test_colon_shape_is_not_a_type_annotation_for_a_function_target() -> None:
    assert not sanity._looks_like_type_annotation(
        "      error: errorMessage,",
        "errorMessage",
        "a.ts",
        target_is_type=False,
    )


def test_colon_shape_still_is_for_a_type_target() -> None:
    assert sanity._looks_like_type_annotation(
        "function run(output: Output): void {",
        "Output",
        "a.ts",
        target_is_type=True,
    )


@pytest.mark.parametrize(
    "snippet",
    [
        "const x: Map<Output, string> = new Map()",
        "import type Output from './o'",
    ],
)
def test_generic_and_import_type_shapes_stay_ungated(snippet: str) -> None:
    assert sanity._looks_like_type_annotation(
        snippet, "Output", "a.ts", target_is_type=False
    )


def test_rust_path_value_still_wins_over_the_colon_shape() -> None:
    # round 32: ``.map(Prompt::as_str)`` matched the colon template on
    # the second ``:`` of ``::`` for a function target.
    assert not sanity._looks_like_type_annotation(
        ".map(Prompt::as_str)", "as_str", "a.rs", target_is_type=False
    )


# --- tier 2 ------------------------------------------------------------


def _sym(
    name: str, start: int, end: int, params: tuple[str, ...] = ()
) -> Symbol:
    from dekko.core.model import Param

    return Symbol(
        id=f"f.ts::{name}",
        name=name,
        qualname=name,
        kind="function",
        path="f.ts",
        language="typescript",
        params=[Param(name=p) for p in params],
        start_line=start,
        end_line=end,
    )


def _classify(
    tmp_path: Path, source: str, hit_line: int, sym: Symbol, bare: str
) -> str:
    (tmp_path / "f.ts").write_text(source)
    lines = source.splitlines()
    hit = sanity.GrepHit(
        path="f.ts", line=hit_line, snippet=lines[hit_line - 1]
    )
    causes = {("f.ts", hit_line): sanity.CAUSE_UNEXPLAINED}
    sanity._explain_shadowing_locals(
        causes, [hit], bare, tmp_path, frozenset(), {"f.ts": [sym]}
    )
    return causes[("f.ts", hit_line)]


def test_value_use_below_a_const_in_the_same_function(tmp_path: Path) -> None:
    src = "function f() {\n  const count = 3\n  if (count >= 3) return\n}\n"
    cause = _classify(tmp_path, src, 3, _sym("f", 1, 4), "count")
    assert cause == sanity.CAUSE_SHADOWING_LOCAL
    assert sanity._shadow_decl_lines[("f.ts", 3)] == 2


def test_guard_1_never_on_a_call_shaped_line(tmp_path: Path) -> None:
    src = "function f() {\n  const count = () => 1\n  return count()\n}\n"
    cause = _classify(tmp_path, src, 3, _sym("f", 1, 4), "count")
    assert cause == sanity.CAUSE_UNEXPLAINED


def test_guard_2_the_targets_own_definition_is_not_a_shadow(
    tmp_path: Path,
) -> None:
    # A nested recursive ``function visit`` matched itself in the
    # first probe. Its own definition line is in own_def_locs.
    src = (
        "function outer() {\n"
        "  const visit = (n) => {\n"
        "    return n ? visit : null\n"
        "  }\n"
        "}\n"
    )
    (tmp_path / "f.ts").write_text(src)
    hit = sanity.GrepHit(path="f.ts", line=3, snippet=src.splitlines()[2])
    causes = {("f.ts", 3): sanity.CAUSE_UNEXPLAINED}
    sanity._explain_shadowing_locals(
        causes,
        [hit],
        "visit",
        tmp_path,
        frozenset({("f.ts", 2)}),  # line 2 IS the target
        {"f.ts": [_sym("outer", 1, 5)]},
    )
    assert causes[("f.ts", 3)] == sanity.CAUSE_UNEXPLAINED


def test_guard_3_a_deeper_nested_decl_is_not_in_scope(tmp_path: Path) -> None:
    src = (
        "function f() {\n"
        "  if (x) {\n"
        "    const count = 1\n"
        "  }\n"
        "  return { n: count }\n"
        "}\n"
    )
    cause = _classify(tmp_path, src, 5, _sym("f", 1, 6), "count")
    assert cause == sanity.CAUSE_UNEXPLAINED


def test_guard_4_a_parameter_is_in_scope_for_the_whole_body(
    tmp_path: Path,
) -> None:
    src = "function f(action) {\n  return { kind: action }\n}\n"
    cause = _classify(tmp_path, src, 2, _sym("f", 1, 3, ("action",)), "action")
    assert cause == sanity.CAUSE_SHADOWING_LOCAL
    assert sanity._shadow_decl_lines[("f.ts", 2)] == 1


def test_a_specific_cause_is_never_rewritten(tmp_path: Path) -> None:
    src = "function f() {\n  const count = 3\n  return { n: count }\n}\n"
    (tmp_path / "f.ts").write_text(src)
    hit = sanity.GrepHit(path="f.ts", line=3, snippet=src.splitlines()[2])
    causes = {("f.ts", 3): sanity.CAUSE_COMMENT_MENTION}
    sanity._explain_shadowing_locals(
        causes,
        [hit],
        "count",
        tmp_path,
        frozenset(),
        {"f.ts": [_sym("f", 1, 4)]},
    )
    assert causes[("f.ts", 3)] == sanity.CAUSE_COMMENT_MENTION


def test_python_files_are_untouched(tmp_path: Path) -> None:
    src = "def f():\n    count = 3\n    return {'n': count}\n"
    (tmp_path / "f.py").write_text(src)
    hit = sanity.GrepHit(path="f.py", line=3, snippet=src.splitlines()[2])
    causes = {("f.py", 3): sanity.CAUSE_UNEXPLAINED}
    sym = Symbol(
        id="f.py::f",
        name="f",
        qualname="f",
        kind="function",
        path="f.py",
        language="python",
        start_line=1,
        end_line=3,
    )
    sanity._explain_shadowing_locals(
        causes, [hit], "count", tmp_path, frozenset(), {"f.py": [sym]}
    )
    assert causes[("f.py", 3)] == sanity.CAUSE_UNEXPLAINED


def test_module_level_hit_has_no_enclosing_symbol(tmp_path: Path) -> None:
    src = "const count = 3\nexport const n = { count }\n"
    cause = _classify(tmp_path, src, 2, _sym("unrelated", 5, 9), "count")
    assert cause == sanity.CAUSE_UNEXPLAINED


# --- end to end: the claude-code repro -----------------------------------


def test_ide_ts_617_reads_as_a_shadowing_local(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SHADOW_REPO)
    code = cli.main(
        ["sanity", "errors.ts:errorMessage", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    rows = {r["line"]: r for r in doc["grep_only"]}
    assert 9 in rows, rows
    assert rows[9]["cause"] == sanity.CAUSE_SHADOWING_LOCAL
    assert rows[9]["decl_line"] == 6
    assert "type position" not in rows[9]["cause"]
    # the declaration itself keeps its own, older label
    assert "local variable/parameter declaration" in rows[6]["cause"]
    assert "decl_line" not in rows[6]

    assert (
        cli.main(["sanity", "errors.ts:errorMessage", "--root", str(root)])
        == 0
    )
    text = capsys.readouterr().out
    assert "same-named local declared earlier" in text
    assert "(declared at line 6)" in text
