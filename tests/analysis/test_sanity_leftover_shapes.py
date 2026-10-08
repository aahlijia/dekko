"""The leftover non-call shapes ``dekko sanity`` names.

Four index-backed causes (a type-declaration body, a recursive
self-call, a name bound to a different import, a recorded heritage
clause), two JS/TS file-state shapes (a line inside a template literal
or a block comment opened above) and the line shapes that rode in
with them (trailing comment, type positions, JSX attribute and text,
chained calls, signatures, declaration lines, bash strings), plus the
widened same-named-local pass.
"""

import json
from pathlib import Path

import pytest

from dekko import repo_ops
from dekko.analysis import sanity
from dekko.core.model import Param, Symbol
from dekko.integrations import cli
from conftest import RepoFactory

# --- the file-state lexer ------------------------------------------------


def test_lexer_carries_a_template_across_lines_with_an_apostrophe() -> None:
    lines = [
        "const prompt = `You don't call",
        "  warn() here",
        "`;",
        "warn()",
    ]
    assert sanity._js_line_states(lines) == [
        "code",
        "template",
        "template",
        "code",
    ]


def test_lexer_skips_an_escaped_backtick_and_a_nested_string() -> None:
    lines = [
        "const s = `a \\` ${x ? 'b`c' : \"\"} d",
        "  still template",
        "`",
        "code",
    ]
    assert sanity._js_line_states(lines) == [
        "code",
        "template",
        "template",
        "code",
    ]


def test_lexer_tracks_a_block_comment_and_ignores_a_line_comment() -> None:
    lines = [
        "/* opens",
        "  warn is mentioned",
        "*/ code(); // a ` in a line comment",
        "still code",
    ]
    assert sanity._js_line_states(lines) == [
        "code",
        "block",
        "block",
        "code",
    ]


def test_a_desynced_file_gets_no_state(tmp_path: Path) -> None:
    (tmp_path / "f.ts").write_text("const s = `open\nwarn()\n")
    sanity._reset_file_caches()
    assert sanity._js_line_state(tmp_path, "f.ts", 2) == "code"


def test_template_continuation_line_is_string_text(tmp_path: Path) -> None:
    src = "const p = `Please\n  call warn() when done\n`;\n"
    (tmp_path / "f.ts").write_text(src)
    sanity._reset_file_caches()
    hit = sanity.GrepHit(
        path="f.ts", line=2, snippet="  call warn() when done"
    )
    shapes = sanity._line_shapes(tmp_path, hit, "warn", target_is_type=False)
    assert shapes.template_text
    cause = sanity.classify_miss(
        hit.snippet,
        "warn",
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        in_template_text=True,
    )
    assert cause == sanity.CAUSE_STRING_MENTION


def test_block_comment_continuation_without_star_is_a_comment(
    tmp_path: Path,
) -> None:
    src = "/**\n  warn resets the counter\n*/\nfunction f() {}\n"
    (tmp_path / "f.ts").write_text(src)
    sanity._reset_file_caches()
    hit = sanity.GrepHit(
        path="f.ts", line=2, snippet="  warn resets the counter"
    )
    shapes = sanity._line_shapes(tmp_path, hit, "warn", target_is_type=False)
    assert shapes.block_comment


# --- string blanking -----------------------------------------------------


def test_js_code_only_blanks_a_string_nested_in_a_template_body() -> None:
    code = sanity._js_code_only(
        'className={`${isLoading ? "animate-x" : ""}`}'
    )
    assert "animate" not in code
    assert "isLoading" in code


def test_js_code_only_blanks_comments_unless_asked_to_keep_them() -> None:
    line = 'f("a"); // see warn("b")'
    assert "warn" not in sanity._js_code_only(line)
    kept = sanity._js_code_only(line, keep_comments=True)
    assert '// see warn("b")' in kept
    assert '("a")' not in kept


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('eval("warn()")', True),
        ('setTimeout("warn()", 1)', True),
        ('log("call warn() before exit")', False),
        ("const t = `constructor(...args) { warn() }`", False),
        ('label: "warn (.claude/agents/)",', False),
        ('describe("warn (provider-form config)", () => {', False),
    ],
)
def test_call_written_in_text_is_only_a_bare_call_string(
    line: str, expected: bool
) -> None:
    assert sanity._call_written_in_text(line, "warn") is expected


# --- the trailing comment ------------------------------------------------


@pytest.mark.parametrize(
    ("line", "path", "expected"),
    [
        ("since: number; // timestamp of last change", "a.ts", True),
        ("x = 1 /* timestamp */", "a.ts", True),
        ('fetch("http://x/timestamp")', "a.ts", False),
        ("timestamp(); // timestamp", "a.ts", False),
        ("x = 1  # timestamp", "a.py", True),
        ('x = "timestamp"  # note', "a.py", False),
        ("let a = 1; // timestamp", "a.rs", True),
        ('let a = "http://timestamp";', "a.rs", False),
    ],
)
def test_trailing_comment(line: str, path: str, expected: bool) -> None:
    assert (
        sanity._looks_like_trailing_comment(line, "timestamp", path)
        is expected
    )


# --- type positions ------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "is_type", "expected"),
    [
        ("Promise<Svc | undefined>", False, True),
        ("x: string | Svc", False, True),
        ("const t = typeof Svc", False, True),
        ("export class A extends Svc {", False, True),
        ("const s = x as Svc", True, True),
        ("const s = x as Svc", False, False),
        ("field: ns.Svc", True, True),
        ("cb: () => Svc", True, True),
        ("get(): Svc {", True, True),
        ("ok ? a() : Svc", True, False),
        ("type Alias = Base & Svc", True, True),
        ('Parameters<Svc["start"]>[0]', True, True),
        ("Svc()", False, False),
    ],
)
def test_js_type_shapes(code: str, is_type: bool, expected: bool) -> None:
    assert (
        sanity._looks_like_js_type_shape(code, "Svc", target_is_type=is_type)
        is expected
    )


# --- the other shape rules -----------------------------------------------


def test_alias_only_reexport_is_an_import_statement() -> None:
    assert sanity._looks_like_import_statement("export { a as warn };", "warn")
    assert sanity._looks_like_import_statement("export { warn }", "warn")
    assert not sanity._looks_like_import_statement("export { warn }()", "warn")


def test_export_list_member_walks_up_to_the_opener() -> None:
    lines = [
        "export {",
        "  // comment",
        "  type A,",
        "  b as c,",
        "  warn,",
        "}",
    ]
    assert sanity._export_list_member(lines, 5, "warn")
    assert not sanity._export_list_member(
        ["const x = {", "  warn,"], 2, "warn"
    )


def test_bash_name_inside_quotes_is_string_text() -> None:
    line = "jq -r '.timestamp' state.json"
    assert sanity._js_shapes(line, "timestamp", "a.sh", False).string_mention
    line = "timestamp=$(date)"
    assert not sanity._js_shapes(
        line, "timestamp", "a.sh", False
    ).string_mention


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("foo().warn(1)", True),
        ("items[0].warn()", True),
        ("x!.warn()", True),
        ("x?.warn()", True),
        (".warn(1)", True),
        ("warn(1)", False),
    ],
)
def test_expression_call(code: str, expected: bool) -> None:
    hit = sanity.GrepHit(path="a.go", line=1, snippet=code)
    shapes = sanity._line_shapes(Path("."), hit, "warn", target_is_type=False)
    assert shapes.expression_call is expected


def test_jsx_attribute_reads_as_a_key_but_a_bare_prop_falls_through(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.tsx").write_text("x\n")
    sanity._reset_file_caches()
    word = sanity._word("action")
    assert sanity._looks_like_jsx_attribute(
        sanity._js_code_only('<Hint action="copy" />'), "action", word
    )
    assert not sanity._looks_like_jsx_attribute(
        sanity._js_code_only("<Hint action={action} />"), "action", word
    )
    hit = sanity.GrepHit(
        path="a.tsx", line=1, snippet='<Hint action="copy" />'
    )
    assert sanity._line_shapes(
        tmp_path, hit, "action", target_is_type=False
    ).declaration


def test_jsx_text_content_versus_an_attribute() -> None:
    word = sanity._word("debug")
    assert sanity._looks_like_jsx_text("<span>debug this</span>", word)
    assert sanity._looks_like_jsx_text("<b>{x}</b> debug<br />", word)
    assert not sanity._looks_like_jsx_text('<Hint debug="on">x</Hint>', word)
    assert not sanity._looks_like_jsx_text("debug related issues{' '}", word)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("abstract warn(): void", True),
        ("warn(x: number): string;", True),
        ("protected async warn<T>(x: T): Promise<void>;", True),
        ("function warn(a: string): void;", True),
        ("warn(x);", False),
        ("const r = warn(x): number", False),
    ],
)
def test_signature_lines(code: str, expected: bool) -> None:
    assert sanity._looks_like_signature(code, "warn") is expected


def test_property_access_shape_refuses_a_bare_call() -> None:
    hit = sanity.GrepHit(path="a.ts", line=1, snippet="if (x.warn) warn()")
    assert not sanity._line_shapes(
        Path("."), hit, "warn", target_is_type=False
    ).property_access
    hit = sanity.GrepHit(path="a.ts", line=1, snippet="if (x.warn) y()")
    assert sanity._line_shapes(
        Path("."), hit, "warn", target_is_type=False
    ).property_access


@pytest.mark.parametrize(
    "code",
    [
        "const [state, warn] = useReducer(r)",
        "export const { warn } = pkg",
        "let warn",
        "for (const warn of items) {",
        "items.map((x, warn) => x)",
        "warn => 1",
        "function f(a, warn) {",
        "readonly warn: string",
    ],
)
def test_declaration_line_shapes(code: str) -> None:
    assert sanity._looks_like_js_declaration(code, "warn", [], 1)


def test_multiline_destructuring_member_finds_its_opener() -> None:
    lines = [
        "function f(t0) {",
        "  const {",
        "    a,",
        "    warn,",
        "  } = t0",
    ]
    assert sanity._destructuring_opener(lines, 4, "warn") == 2
    lines = ["const x = {", "  warn,", "}"]
    assert sanity._destructuring_opener(lines, 2, "warn") is None


def test_rust_let_mut_is_a_local_declaration() -> None:
    assert sanity._looks_like_local_binding_or_literal(
        "let mut warn = 1;", "warn"
    )


# --- the ladder ----------------------------------------------------------


def _miss(snippet: str, name: str, **flags: bool) -> str:
    return sanity.classify_miss(
        snippet,
        name,
        is_test_file=False,
        unsupported_language=False,
        tests_excluded=True,
        **flags,
    )


def test_type_context_sits_below_comments_and_recorded_references() -> None:
    assert (
        _miss("// warn", "warn", looks_like_comment=True, in_type_context=True)
        == sanity.CAUSE_COMMENT_ELSEWHERE
    )
    assert (
        _miss("x", "warn", is_recorded_reference=True, in_type_context=True)
        == sanity.CAUSE_VALUE_REFERENCE
    )
    assert (
        _miss(
            "x", "warn", in_type_context=True, looks_like_type_annotation=True
        )
        == sanity.CAUSE_TYPE_CONTEXT
    )


def test_trailing_comment_sits_above_the_string_rung() -> None:
    cause = _miss(
        "x",
        "warn",
        looks_like_trailing_comment=True,
        looks_like_string_mention=True,
    )
    assert cause == sanity.CAUSE_TRAILING_COMMENT


def test_signature_and_property_shape_sit_above_the_local_binding() -> None:
    assert (
        _miss(
            "x",
            "warn",
            looks_like_signature=True,
            looks_like_local_binding_or_literal=True,
        )
        == sanity.CAUSE_SIGNATURE
    )
    assert (
        _miss(
            "x",
            "warn",
            is_recorded_read=True,
            looks_like_property_access=True,
        )
        == sanity.CAUSE_PROPERTY_READ
    )
    assert (
        _miss("x", "warn", looks_like_property_access=True)
        == sanity.CAUSE_PROPERTY_ACCESS_SHAPE
    )
    assert (
        _miss("x", "warn", looks_like_jsx_text=True) == sanity.CAUSE_JSX_TEXT
    )


def test_expression_call_sits_below_the_import_rungs() -> None:
    assert (
        _miss("x", "warn", looks_like_expression_call=True)
        == sanity.CAUSE_QUALIFIED_CALL
    )
    assert (
        _miss(
            "x",
            "warn",
            looks_like_import_member=True,
            looks_like_expression_call=True,
        )
        == sanity.CAUSE_IMPORT_STATEMENT
    )


def test_file_state_beats_every_line_shape_but_not_mapped() -> None:
    assert (
        _miss("x", "warn", in_template_text=True, looks_like_comment=True)
        == sanity.CAUSE_STRING_MENTION
    )
    assert (
        _miss("x", "warn", in_block_comment=True, not_mapped=True)
        == sanity.CAUSE_NOT_MAPPED
    )


# --- the widened shadow pass ---------------------------------------------


def _sym(
    name: str,
    start: int,
    end: int,
    params: tuple[str, ...] = (),
    path: str = "f.ts",
) -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind="function",
        path=path,
        language="typescript",
        params=[Param(name=p) for p in params],
        start_line=start,
        end_line=end,
    )


def _shadow(
    tmp_path: Path, source: str, hit_line: int, syms: list[Symbol], bare: str
) -> str:
    (tmp_path / "f.ts").write_text(source)
    sanity._reset_file_caches()
    lines = source.splitlines()
    hit = sanity.GrepHit(
        path="f.ts", line=hit_line, snippet=lines[hit_line - 1]
    )
    causes = {("f.ts", hit_line): sanity.CAUSE_UNEXPLAINED}
    sanity._explain_shadowing_locals(
        causes, [hit], bare, tmp_path, frozenset(), {"f.ts": syms}
    )
    return causes[("f.ts", hit_line)]


def test_destructured_and_optional_parameters_bind_the_name(
    tmp_path: Path,
) -> None:
    src = "function f({ slots, activeSlot }) {\n  use(activeSlot)\n}\n"
    sym = _sym("f", 1, 3, ("{ slots, activeSlot }",))
    assert _shadow(tmp_path, src, 2, [sym], "activeSlot") == (
        sanity.CAUSE_SHADOWING_LOCAL
    )
    src = "function f(count?) {\n  use(count)\n}\n"
    sym = _sym("f", 1, 3, ("count?",))
    assert _shadow(tmp_path, src, 2, [sym], "count") == (
        sanity.CAUSE_SHADOWING_LOCAL
    )


def test_a_parameter_declared_on_the_targets_own_line_still_binds(
    tmp_path: Path,
) -> None:
    src = "function command(command: string) {\n  use(`${command}`)\n}\n"
    (tmp_path / "f.ts").write_text(src)
    sanity._reset_file_caches()
    hit = sanity.GrepHit(path="f.ts", line=2, snippet=src.splitlines()[1])
    causes = {("f.ts", 2): sanity.CAUSE_UNEXPLAINED}
    sanity._explain_shadowing_locals(
        causes,
        [hit],
        "command",
        tmp_path,
        frozenset({("f.ts", 1)}),
        {"f.ts": [_sym("command", 1, 3, ("command",))]},
    )
    assert causes[("f.ts", 2)] == sanity.CAUSE_SHADOWING_LOCAL


@pytest.mark.parametrize(
    ("src", "hit_line"),
    [
        (
            "function f() {\n  xs.map(count => {\n    return count + 1\n"
            "  })\n}\n",
            3,
        ),
        (
            "function f() {\n  for (const count of xs) {\n    use(count)\n"
            "  }\n}\n",
            3,
        ),
        (
            "function f() {\n  const {\n    a,\n    count,\n  } = t0\n"
            "  use(count)\n}\n",
            6,
        ),
    ],
)
def test_arrow_parameters_for_of_and_multiline_members_bind(
    tmp_path: Path, src: str, hit_line: int
) -> None:
    end = src.count("\n")
    assert _shadow(tmp_path, src, hit_line, [_sym("f", 1, end)], "count") == (
        sanity.CAUSE_SHADOWING_LOCAL
    )


def test_an_inner_arrow_sees_the_outer_functions_local(tmp_path: Path) -> None:
    src = (
        "function outer() {\n"
        "  const count = 1\n"
        "  const inner = () => {\n"
        "    return { count }\n"
        "  }\n"
        "}\n"
    )
    syms = [_sym("outer", 1, 6), _sym("inner", 3, 5)]
    assert (
        _shadow(tmp_path, src, 4, syms, "count")
        == sanity.CAUSE_SHADOWING_LOCAL
    )
    assert sanity._shadow_decl_lines[("f.ts", 4)] == 2


# --- end to end ----------------------------------------------------------

_TYPE_REPO = {
    "a.ts": "export class TerminalEvent {\n  x = 1\n}\n",
    "b.ts": (
        "import { TerminalEvent } from './a'\n"
        "export class FocusEvent extends TerminalEvent {\n"
        "  y = 2\n"
        "}\n"
        "export function make() {\n"
        "  return new TerminalEvent()\n"
        "}\n"
    ),
    "c.ts": "export class TerminalEvent {\n  z = 3\n}\n",
    "d.ts": (
        "import { TerminalEvent } from './c'\n"
        "export class BlurEvent extends TerminalEvent {\n"
        "  w = 4\n"
        "}\n"
    ),
    "e.ts": (
        "export interface Row {\n"
        "  TerminalEvent?: number\n"
        "}\n"
        "export enum Kind {\n"
        "  TerminalEvent = 1,\n"
        "}\n"
    ),
}

_CALL_REPO = {
    "walk.ts": (
        "export function walk(n: number): number {\n"
        "  if (n <= 0) return 0\n"
        "  return walk(n - 1)\n"
        "}\n"
    ),
    "other.ts": (
        "export function walk(n: number): number {\n"
        "  return n ? walk(n - 1) : 0\n"
        "}\n"
    ),
    "use.ts": "import { walk } from './walk'\nexport const r = walk(3)\n",
    "fs_user.ts": (
        "import { appendFileSync } from 'fs'\n"
        "export function log(s: string) {\n"
        "  appendFileSync('log.txt', s)\n"
        "}\n"
    ),
    "append.ts": "export function appendFileSync(p: string, s: string) {}\n",
    "append_user.ts": (
        "import { appendFileSync } from './append'\n"
        "export const go = () => appendFileSync('a', 'b')\n"
    ),
    "append2.ts": "export function appendFileSync(p: string, s: string) {}\n",
    "append2_user.ts": (
        "import { appendFileSync } from './append2'\n"
        "export const go2 = () => appendFileSync('a', 'b')\n"
    ),
}


def _sanity_json(
    root: Path, target: str, capsys: pytest.CaptureFixture
) -> dict[tuple[str, int], dict]:
    code = cli.main(["sanity", target, "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)

    return {(row["file"], row["line"]): row for row in doc["grep_only"]}


def test_heritage_clause_is_tier_1_and_a_siblings_clause_resolved_elsewhere(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(_TYPE_REPO), "a.ts:TerminalEvent", capsys
    )
    assert rows[("b.ts", 2)]["cause"] == sanity.CAUSE_HERITAGE
    assert rows[("d.ts", 2)]["cause"] == sanity.CAUSE_RESOLVED_ELSEWHERE
    assert rows[("d.ts", 2)]["resolved_to"] == ["c.ts::TerminalEvent"]


def test_interface_and_enum_members_are_a_type_context(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(_TYPE_REPO), "a.ts:TerminalEvent", capsys
    )
    assert rows[("e.ts", 2)]["cause"] == sanity.CAUSE_TYPE_CONTEXT
    assert rows[("e.ts", 5)]["cause"] == sanity.CAUSE_TYPE_CONTEXT


def test_recursive_self_call_is_tier_1_and_a_plain_call_for_a_sibling(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(_CALL_REPO)
    rows = _sanity_json(root, "walk.ts:walk", capsys)
    assert rows[("walk.ts", 3)]["cause"] == sanity.CAUSE_SELF_RECURSION
    assert rows[("other.ts", 2)]["cause"] != sanity.CAUSE_SELF_RECURSION
    rows = _sanity_json(root, "other.ts:walk", capsys)
    assert rows[("other.ts", 2)]["cause"] == sanity.CAUSE_SELF_RECURSION
    assert rows[("walk.ts", 3)]["cause"] != sanity.CAUSE_SELF_RECURSION


def test_name_bound_to_an_external_import_names_the_module(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(_CALL_REPO)
    rows = _sanity_json(root, "append.ts:appendFileSync", capsys)
    row = rows[("fs_user.ts", 3)]
    assert row["cause"] == sanity.CAUSE_IMPORT_BOUND_ELSEWHERE
    assert row["bound_to"] == "fs"
    # A call the map attributed to the other repo declaration keeps the
    # resolved-elsewhere cause; the import-bound fact only fills rows
    # the ladder left open.
    assert (
        rows[("append2_user.ts", 2)]["cause"]
        == sanity.CAUSE_RESOLVED_ELSEWHERE
    )

    assert (
        cli.main(["sanity", "append.ts:appendFileSync", "--root", str(root)])
        == 0
    )
    text = capsys.readouterr().out
    assert "fs_user.ts:3" in text
    assert "(bound to fs)" in text


def test_import_bound_to_a_repo_file_names_its_path(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_CALL_REPO)
    index, _ = repo_ops.load_or_regen(root, no_regen=True)
    assert (
        sanity._import_bound_to(
            index, "append2_user.ts", "appendFileSync", "append.ts"
        )
        == "append2.ts"
    )
    assert (
        sanity._import_bound_to(
            index, "append2_user.ts", "appendFileSync", "append2.ts"
        )
        is None
    )
    assert (
        sanity._import_bound_to(
            index, "fs_user.ts", "appendFileSync", "append.ts"
        )
        == "fs"
    )


def test_all_mode_aggregates_the_new_causes(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({**_TYPE_REPO, **_CALL_REPO})
    assert cli.main(["sanity", "--all", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    agg = doc["aggregate_causes"]
    # ``other.ts:walk`` has no fan-in (a self edge is never recorded),
    # so only ``walk.ts:walk`` is swept.
    assert agg.get(sanity.CAUSE_SELF_RECURSION) == 1
    # ``fs_user.ts:3`` is import-bound for both repo ``appendFileSync``s.
    assert agg.get(sanity.CAUSE_IMPORT_BOUND_ELSEWHERE) == 2
    assert agg.get(sanity.CAUSE_HERITAGE, 0) >= 1
    assert agg.get(sanity.CAUSE_TYPE_CONTEXT, 0) >= 2
    assert agg.get(sanity.CAUSE_UNEXPLAINED) is None
    assert doc["flagged"] == []


_BARREL_REPO = {
    "lib/base.ts": "export function helper(): number {\n  return 1\n}\n",
    "lib/mid.ts": "export * from './base'\n",
    "lib/index.ts": "export { helper } from './mid'\n",
    "other/base.ts": "export function helper(): number {\n  return 2\n}\n",
    "app.ts": (
        "import { helper } from './lib/index'\nexport const r = helper()\n"
    ),
}


def test_an_import_through_a_barrel_is_not_a_different_declaration(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(_BARREL_REPO)
    index, _ = repo_ops.load_or_regen(root, no_regen=True)
    # Two barrels deep, the second a star: the import is the target's
    # own declaration and explains no missing edge.
    assert (
        sanity._import_bound_to(index, "app.ts", "helper", "lib/base.ts")
        is None
    )
    # For the namesake the barrel never reaches, it still does.
    assert (
        sanity._import_bound_to(index, "app.ts", "helper", "other/base.ts")
        == "lib/index.ts"
    )


def test_a_barrel_cycle_ends_the_walk(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(
        {
            "a.ts": "export * from './b'\n",
            "b.ts": "export * from './a'\n",
            "target.ts": "export function ghost() {}\n",
            "app.ts": (
                "import { ghost } from './a'\nexport const r = ghost()\n"
            ),
        }
    )
    index, _ = repo_ops.load_or_regen(root, no_regen=True)
    assert (
        sanity._import_bound_to(index, "app.ts", "ghost", "target.ts")
        == "a.ts"
    )


_TS_RESIDUE_REPO = {
    "ink/r.ts": (
        "export default function createRenderer(): void {}\n"
        "export type Renderer = number;\n"
    ),
    "ink/ink.ts": (
        "import createRenderer, { type Renderer } from './r.js'\n"
        "export const r: Renderer = 1\n"
        "createRenderer()\n"
    ),
    "src/exec.ts": (
        "export function execSync_DEPRECATED(\n"
        "  cmd: string,\n"
        "): string\n"
        "export function execSync_DEPRECATED(\n"
        "  cmd: string,\n"
        "  opts: object,\n"
        "): string\n"
        "export function execSync_DEPRECATED(cmd: string, opts?: object)"
        ": string {\n"
        "  return cmd\n"
        "}\n"
    ),
    "src/err.ts": "export class LimitError extends Error {}\n",
    "src/guard.ts": (
        "import { LimitError } from './err'\n"
        "export function isLimit(e: unknown): e is LimitError {\n"
        "  return e instanceof LimitError\n"
        "}\n"
    ),
    "src/tool.ts": "export type Output = number;\n",
    "src/use2.ts": (
        "import type { Output } from './tool'\n"
        "export const t = {} satisfies ToolDef<In, Output, Prog>\n"
    ),
    "src/act.ts": (
        "export let activityCallback: (() => void) | null = null\n"
        "export function set(cb: () => void): void {\n"
        "  activityCallback = cb\n"
        "}\n"
        "export function check(cb: any): boolean {\n"
        "  return activityCallback == cb\n"
        "}\n"
    ),
}


def test_default_and_named_import_line_names_both(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(_TS_RESIDUE_REPO)
    for target in ("ink/r.ts::createRenderer", "ink/r.ts::Renderer"):
        rows = _sanity_json(root, target, capsys)
        assert rows[("ink/ink.ts", 1)]["cause"] == (
            sanity.CAUSE_IMPORT_STATEMENT
        ), target
    rows = _sanity_json(root, "ink/r.ts::createRenderer", capsys)
    assert ("ink/ink.ts", 3) not in rows


def test_overload_heads_are_signatures(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(_TS_RESIDUE_REPO),
        "src/exec.ts::execSync_DEPRECATED",
        capsys,
    )
    for line in (1, 4):
        assert rows[("src/exec.ts", line)]["cause"] == (
            sanity.CAUSE_SIGNATURE
        ), line


def test_type_guard_and_mid_list_generic_are_type_positions(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(_TS_RESIDUE_REPO)
    rows = _sanity_json(root, "src/err.ts::LimitError", capsys)
    assert rows[("src/guard.ts", 2)]["cause"] == sanity.CAUSE_TYPE_ANNOTATION
    rows = _sanity_json(root, "src/tool.ts::Output", capsys)
    assert rows[("src/use2.ts", 2)]["cause"] == sanity.CAUSE_TYPE_ANNOTATION


def test_assignment_to_the_name_is_a_write(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    rows = _sanity_json(
        make_mapped_repo(_TS_RESIDUE_REPO),
        "src/act.ts::activityCallback",
        capsys,
    )
    assert rows[("src/act.ts", 3)]["cause"] == sanity.CAUSE_ASSIGNMENT
    assert rows[("src/act.ts", 6)]["cause"] != sanity.CAUSE_ASSIGNMENT


def test_assignment_shape_refuses_a_comparison_or_call(tmp_path: Path) -> None:
    for snippet in ("activityCallback == cb", "activityCallback(cb)"):
        (tmp_path / "a.ts").write_text(snippet + "\n")
        hit = sanity.GrepHit(path="a.ts", line=1, snippet=snippet)
        causes = sanity._classify_grep_hits(
            [hit],
            "activityCallback",
            tmp_path,
            own_def_locs=frozenset(),
            tests_excluded=True,
        )
        assert causes[("a.ts", 1)] != sanity.CAUSE_ASSIGNMENT, snippet
