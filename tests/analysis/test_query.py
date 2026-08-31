"""Query subcommand: actions, target syntax, exit codes."""

import json

import pytest

from dekko.integrations import cli
from dekko.render import mapfile
from dekko.analysis import query
from dekko.core.model import Symbol

from conftest import RepoFactory

TWO_FILES = {
    "a.py": (
        "def helper(x: int) -> int:\n"
        "    return x + 1\n"
        "\n"
        "\n"
        "def entry() -> None:\n"
        "    helper(1)\n"
    ),
    "b.py": "def helper(x: int) -> int:\n    return x - 1\n",
}


def test_callers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "callers", "a.py:helper", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "entry() -> None" in out


def test_callees(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    assert cli.main(["query", "callees", "entry", "--root", str(root)]) == 0
    assert "helper(x: int) -> int" in capsys.readouterr().out


def test_file_json_carries_meta(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "file", "a.py", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["meta"]["total"] == len(doc["symbols"])
    assert doc["meta"]["truncated_by"] is None


def test_callees_budget_caps_and_footers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(
        ["query", "callees", "entry", "--root", str(root), "--budget", "1"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "tokens" in out.splitlines()[-1]


def test_ambiguous_bare_name(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "symbol", "helper", "--root", str(root)])
    assert code == 4
    err = capsys.readouterr().err
    # 3.3: candidate rows now carry a line number and signature, not
    # just path:qualname, so same-file/same-name overloads render as
    # visually distinct rows.
    assert "a.py:1  helper(x: int) -> int" in err
    assert "b.py:1  helper(x: int) -> int" in err


def test_ambiguous_candidates_truncated_past_cap(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # zed's bug (B10): 99 same-named ``fn main`` candidates dumped
    # unconditionally, ~1,110 tokens for a list an agent virtually
    # never reads past the first handful of before narrowing with a
    # ``file.py:`` qualifier — the candidate dump must truncate like
    # every other list this size in the tool.
    files = {
        f"mod_{i}.py": "def dup() -> int:\n    return 1\n" for i in range(25)
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "symbol", "dup", "--root", str(root)])
    assert code == 4
    err = capsys.readouterr().err
    candidate_rows = [ln for ln in err.splitlines() if ln.startswith("  mod_")]
    assert len(candidate_rows) == 20
    assert "+5 more (qualify with" in err
    # round-09 §2.5: the qualifier example must be built from a real
    # candidate's own path, not a hardcoded ``file.py`` placeholder —
    # confirmed on two 100%-non-Python monorepos (spring-boot, zed),
    # neither of which has any ``file.py`` anywhere in the tree.
    assert "`mod_0.py:dup`" in err
    assert "file.py" not in err


CONTROLLER_COLLISION = {
    # A top-level function whose bare name has no "." in its qualname
    # (qualname == name == "controller") coexists with several
    # unrelated nested methods that happen to share the same bare
    # name but never the same qualname (`Foo.controller`,
    # `Bar.controller`). Bug #1.4: the old ``or``-short-circuiting
    # lookup in ``_resolve_exact`` found the single qualname hit and
    # returned it immediately, never even consulting
    # ``symbols_by_name`` — silently picking the top-level function
    # while ignoring two real, same-named collisions.
    "top.py": "def controller() -> None:\n    pass\n",
    "foo.py": (
        "class Foo:\n    def controller(self) -> None:\n        pass\n"
    ),
    "bar.py": (
        "class Bar:\n    def controller(self) -> None:\n        pass\n"
    ),
}


def test_bare_name_ambiguity_not_masked_by_qualname_shortcut(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CONTROLLER_COLLISION)
    code = cli.main(["query", "symbol", "controller", "--root", str(root)])
    assert code == 4
    err = capsys.readouterr().err
    assert "top.py:1  controller() -> None" in err
    assert "foo.py:2  Foo.controller(self) -> None" in err
    assert "bar.py:2  Bar.controller(self) -> None" in err


def test_close_names_suppresses_short_fuzzy_junk(
    make_mapped_repo: RepoFactory,
) -> None:
    # 3.4b, mode (b): single-letter symbol names (zed's `B`/`t`/`A`/
    # `D`) must never win the fuzzy edit-distance tier just because
    # edit distance is biased toward short strings, even when nothing
    # else is close.
    assert query._close_names("zzznonsense", ["B", "t", "A", "D"]) == []


def test_close_names_suppresses_coincidental_single_char_substring(
    make_mapped_repo: RepoFactory,
) -> None:
    # round-13 claude-buddy.md: a totally unrelated, long query
    # (`totallyMadeUpSymbolXYZ123`) coincidentally contains a lowercase
    # "b" (from "...Symbol...") and "d" (from "...Made..."), so the
    # substring tier used to surface single-letter symbol names `B`/`D`
    # as "closest matches" even though neither has any real relation to
    # the query -- unlike `test_close_names_suppresses_short_fuzzy_junk`
    # above (a needle with no such coincidental substrings), this one
    # reproduces the actual reported shape.
    assert query._close_names("totallyMadeUpSymbolXYZ123", ["B", "D"]) == []


def test_close_names_keeps_two_char_substring_match(
    make_mapped_repo: RepoFactory,
) -> None:
    # The new floor targets single-character candidates specifically;
    # a genuine 2+ character substring match must stay eligible.
    assert query._close_names("checkOkStatus", ["ok"]) == ["ok"]


def test_close_names_still_surfaces_real_near_typo(
    make_mapped_repo: RepoFactory,
) -> None:
    # 3.4b, mode (a): a genuine near-typo of a real (non-trivially-
    # short) symbol name must still be suggested — the tightened
    # cutoff/floor must not blind the suggester to real answers
    # (claude-buddy's `buddyStateDr` case).
    assert query._close_names(
        "buddyStateDrx", ["buddyStateDr", "Q", "Z", "W"]
    ) == ["buddyStateDr"]


def test_close_names_raised_cutoff_excludes_marginal_fuzzy_match(
    make_mapped_repo: RepoFactory,
) -> None:
    # The edit-distance cutoff was raised from difflib's permissive
    # 0.6 to 0.72 specifically to trim marginal, not-actually-close
    # matches — this candidate scores 0.667 (passed under the old
    # cutoff, fails under the new one) and is long enough that the
    # separate length floor isn't what's excluding it.
    assert query._close_names("trix", ["tribe"]) == []
    assert query._close_names("trix", ["trib"]) == ["trib"]


def test_close_names_excludes_verbatim_self_match() -> None:
    # Round 23 §16: a needle that's a verbatim (case-sensitive) match
    # for a real candidate name offers nothing new as a "closest
    # match" suggestion -- it's just echoing the input back. A
    # case-differing match (still a real, different string) must stay
    # eligible, as must a genuinely different prefix match. Only
    # opt-in via exclude_verbatim=True: callers whose needle is a
    # *derived* substring (e.g. _suggest_symbols's bare qualname) rely
    # on a verbatim match being the useful "right name" suggestion.
    assert query._close_names(
        "Project",
        ["Project", "ProjectConfig", "project"],
        exclude_verbatim=True,
    ) == ["project", "ProjectConfig"]
    assert query._close_names(
        "Project", ["Project", "ProjectConfig", "project"]
    ) == ["Project", "project", "ProjectConfig"]


def test_qualname_near_miss_requires_segment_boundary() -> None:
    # ``_is_qualname_near_miss`` requires a preceding ``.`` before the
    # requested qualname, not a bare substring match — a qualname that
    # merely ends with the same trailing characters (no segment
    # boundary) must not count as a namespace-missing near-miss.
    real = Symbol(
        id="ns.cpp::tensorflow.ClientSession.Run",
        name="Run",
        qualname="tensorflow.ClientSession.Run",
        kind="method",
        path="ns.cpp",
        language="cpp",
    )
    unrelated_suffix = Symbol(
        id="other.cpp::NotClientSession.Run",
        name="Run",
        qualname="NotClientSession.Run",
        kind="method",
        path="other.cpp",
        language="cpp",
    )
    bare = Symbol(
        id="bare.cpp::Run",
        name="Run",
        qualname="Run",
        kind="function",
        path="bare.cpp",
        language="cpp",
    )
    assert query._is_qualname_near_miss("ClientSession.Run", real) is True
    assert (
        query._is_qualname_near_miss("ClientSession.Run", unrelated_suffix)
        is False
    )
    # A bare (no-dot) requested qualname has no container segment to
    # be "missing a prefix" from, so it never counts as a near-miss.
    assert query._is_qualname_near_miss("Run", bare) is False


AMBIGUOUS_CALL = {
    "a.py": "def target() -> int:\n    return 1\n",
    "b.py": "def target() -> int:\n    return 2\n",
    "c.py": ("def caller() -> int:\n    return target()\n"),
}


def test_ambiguous_counts_helper_returns_incoming_and_outgoing(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SYMBOL_CARD_BOTH_DIRECTIONS_AMBIGUOUS)
    index = mapfile.load_map(root)
    mid = next(s for s in index.symbols_by_qualname["mid"] if s.path == "a.py")
    assert query.ambiguous_counts(index, mid) == (2, 1)


def test_symbol_card_shows_ambiguous_in_count(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Performance #2: an ambiguous call (here, `target()` called from
    # c.py with two same-named repo-wide candidates) never becomes a
    # resolved edge, so a.py:target's fan-in reads as 0 even though a
    # real call site exists — the ambiguous_in count is what tells a
    # caller "there's more here than fan-in alone shows."
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(["query", "symbol", "a.py:target", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert (
        "fan-in: 0 (+1 additional call site(s) named 'target' "
        "resolved ambiguously — not counted), fan-out: 0" in out
    )


def test_symbol_card_json_ambiguous_in(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(
        ["query", "symbol", "a.py:target", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["fan_in"] == 0
    assert doc["ambiguous_in"] == 1


SYMBOL_CARD_BOTH_DIRECTIONS_AMBIGUOUS = {
    # a.py:mid's incoming side collides on the bare name "mid" (b.py
    # defines another), and its outgoing side calls a bare "shared"
    # that also collides (shared1.py/shared2.py) — two independent
    # ambiguous call sites named "mid" (entry1, entry2) vs. one
    # ambiguous outgoing call, so ambig_in (2) != ambig_out (1) and a
    # swapped fan-in/fan-out label would be caught by asserting exact
    # values, not just presence of a number (round23 issue 08).
    "a.py": "def mid() -> int:\n    return shared()\n",
    "b.py": "def mid() -> int:\n    return 1\n",
    "shared1.py": "def shared() -> int:\n    return 1\n",
    "shared2.py": "def shared() -> int:\n    return 2\n",
    "caller1.py": "def entry1() -> int:\n    return mid()\n",
    "caller2.py": "def entry2() -> int:\n    return mid()\n",
}


def test_symbol_card_labels_ambig_in_and_ambig_out_correctly(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SYMBOL_CARD_BOTH_DIRECTIONS_AMBIGUOUS)
    code = cli.main(["query", "symbol", "a.py:mid", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert (
        "fan-in: 0 (+2 additional call site(s) named 'mid' resolved "
        "ambiguously — not counted), fan-out: 0 (+1 outgoing "
        "call(s) resolved ambiguously — not counted)" in out
    )


def test_symbol_card_json_carries_both_ambiguous_directions(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(SYMBOL_CARD_BOTH_DIRECTIONS_AMBIGUOUS)
    code = cli.main(
        ["query", "symbol", "a.py:mid", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ambiguous_in"] == 2
    assert doc["ambiguous_out"] == 1


def test_get_callers_notes_ambiguous_call_sites(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(["query", "callers", "a.py:target", "--root", str(root)])
    assert code == 0
    captured = capsys.readouterr()
    assert "(no callers of" in captured.out
    assert "ambiguously" in captured.err


def test_get_callees_notes_ambiguous_call_sites(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # round-09 §2.1 part A: only the callers direction disclosed
    # ambiguous call sites (``ambig_in``) — a caller's own ambiguous
    # *outgoing* calls (here, ``caller()`` calling the ambiguous
    # ``target``) had no equivalent surfacing on ``query callees``, so
    # a genuinely ambiguous callee was silently indistinguishable from
    # "this function calls nothing else."
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(["query", "callees", "c.py:caller", "--root", str(root)])
    assert code == 0
    captured = capsys.readouterr()
    assert "(no callees of" in captured.out
    assert "ambiguously" in captured.err
    assert "1 outgoing call" in captured.err


def test_callees_json_carries_ambiguous_out(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(
        ["query", "callees", "c.py:caller", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ambiguous_out"] == 1


def test_callers_json_has_no_ambiguous_out_key(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # ``ambig_out`` is only ever computed for the callees direction —
    # a callers query must not carry a stray/zero key.
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(
        ["query", "callers", "a.py:target", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert "ambiguous_out" not in doc


TS_CALLBACK = {
    "handlers.ts": (
        "export function handleClick(): void {\n  console.log('clicked');\n}\n"
    ),
    "wire.ts": (
        "import { handleClick } from './handlers';\n"
        "\n"
        "export const config = {\n"
        "  onClick: handleClick,\n"
        "};\n"
    ),
}


def test_symbol_card_notes_zero_fan_for_unreferenced_type(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # B11: a struct/class used only as a field or return-type
    # annotation still reads fan-in/fan-out 0/0 (only call/reference
    # edges are tracked) — easy to misread as "unused." The card must
    # caveat this rather than let 0/0 stand unexplained. (A struct used
    # as a *parameter* type, or as a struct field's own declared type
    # (including anonymous embedding), is exercised separately below —
    # Track G/bug #1.1a and its follow-up gave Go a ``reference_query``
    # covering both positions, so they now report real referenced-by
    # evidence instead of falling back to this caveat. A type named
    # only in a ``switch v := x.(type) { case RepoMeta: ... }`` clause
    # is still outside that query's coverage — ``type_case``'s
    # ``type_identifier`` isn't a position any pattern targets — so it
    # remains a genuine no-evidence fixture for this caveat path.)
    files = {
        "types.go": (
            "package types\n\ntype RepoMeta struct {\n\tName string\n}\n"
        ),
        "user.go": (
            "package types\n\n"
            "func Show(x interface{}) {\n"
            "	switch v := x.(type) {\n"
            "	case RepoMeta:\n"
            "		_ = v\n"
            "	}\n"
            "}\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "symbol", "RepoMeta", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "fan-in: 0, fan-out: 0" in out
    assert "not evidence the type is unused" in out


def test_symbol_card_shows_referenced_by_for_go_field_type(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Follow-up to Track G/bug #1.1a: a struct used only as another
    # struct's field type (``Meta RepoMeta``, previously the
    # deliberately-uncovered case above) now has real referenced-by
    # evidence, since ``_GO_REFERENCE_QUERY`` gained a
    # ``field_declaration type:`` pattern.
    files = {
        "types.go": (
            "package types\n\ntype RepoMeta struct {\n\tName string\n}\n"
        ),
        "user.go": (
            "package types\n\ntype Wrapper struct {\n\tMeta RepoMeta\n}\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "symbol", "RepoMeta", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "fan-in: 0, fan-out: 0" in out
    assert "referenced-by: 1 (not called)" in out
    assert "not evidence the type is unused" not in out


def test_symbol_card_shows_referenced_by_for_go_param_type(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Track G / bug #1.1a's "free correctness bonus" (per the
    # implementation plan's own Verify section): a Go struct used only
    # as a parameter type now has real referenced-by evidence instead
    # of the generic zero-fan caveat, since Go gained a
    # ``reference_query`` covering parameter/return/var/composite-
    # literal type positions.
    files = {
        "types.go": (
            "package types\n\ntype RepoMeta struct {\n\tName string\n}\n"
        ),
        "user.go": (
            "package types\n\n"
            "func Show(m RepoMeta) string {\n\treturn m.Name\n}\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "symbol", "RepoMeta", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "fan-in: 0, fan-out: 0" in out
    assert "referenced-by: 1 (not called)" in out
    assert "not evidence the type is unused" not in out


def test_symbol_card_json_notes_zero_fan_for_type(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "types.go": (
            "package types\n\ntype RepoMeta struct {\n\tName string\n}\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(
        ["query", "symbol", "RepoMeta", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["fan_in"] == 0
    assert "fan_note" in doc


def test_symbol_card_no_zero_fan_note_for_function(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # A plain function with 0/0 (genuinely unused) must not get the
    # type-specific caveat — it only applies to TYPE_KINDS symbols.
    files = {"a.py": "def unused() -> None:\n    pass\n"}
    root = make_mapped_repo(files)
    code = cli.main(["query", "symbol", "unused", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "fan-in: 0, fan-out: 0" in out
    assert "not evidence the type is unused" not in out
    # C.1: fan-in of 0 has nothing to disambiguate, so the
    # distinct-callers-vs-call-sites note must not print either.
    assert "note: fan-in counts distinct callers" not in out


def test_symbol_card_fan_in_note_points_at_sites_and_sanity(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # C.1: a symbol with fan_in > 0 must disclose that fan-in counts
    # distinct callers (not call sites) and point at the two other
    # views of "how often is this used" — 'query callers --sites' and
    # 'sanity' — so a reader doesn't mistake one number's axis for
    # another's (round-24 claude-code.md friction #3).
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "symbol", "a.py:helper", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "fan-in: 1" in out
    assert "note: fan-in counts distinct callers" in out
    assert "query callers a.py::helper --sites" in out
    assert "sanity a.py::helper" in out


def test_symbol_card_json_omits_fan_in_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # C.1: the note is a text-mode-only disclosure — JSON consumers
    # already get fan_in as a bare integer and can request the other
    # views directly, so no new key is added to the JSON doc.
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(
        ["query", "symbol", "a.py:helper", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["fan_in"] == 1
    assert "fan_note" not in doc


def test_symbol_card_shows_referenced_by_count(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Bug #2(b): handleClick is never called, only wired up as a
    # value in wire.ts's object literal. fan-in stays 0 (correct —
    # nothing *calls* it), but referenced-by must be nonzero so a
    # reader doesn't misread "fan-in: 0" as "definitely unused."
    root = make_mapped_repo(TS_CALLBACK)
    code = cli.main(
        ["query", "symbol", "handlers.ts:handleClick", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "fan-in: 0, fan-out: 0" in out
    assert "referenced-by: 1 (not called)" in out


def test_symbol_card_json_referenced_by(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_CALLBACK)
    code = cli.main(
        [
            "query",
            "symbol",
            "handlers.ts:handleClick",
            "--root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["fan_in"] == 0
    assert doc["referenced_by"] == 1


def test_get_callers_shows_referenced_not_called_section(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # The exact bug #2(b) repro: get_callers on a reference-only
    # callback must not read as "nothing uses this."
    root = make_mapped_repo(TS_CALLBACK)
    code = cli.main(
        ["query", "callers", "handlers.ts:handleClick", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "(no callers of" not in out
    assert "referenced (not called):" in out
    assert "wire.ts" in out


def test_get_callers_json_referenced_not_called(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_CALLBACK)
    code = cli.main(
        [
            "query",
            "callers",
            "handlers.ts:handleClick",
            "--root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["results"] == []
    assert len(doc["referenced_not_called"]) == 1
    assert doc["referenced_not_called"][0]["path"] == "wire.ts"


TS_CALLBACK_NESTED = {
    "handlers.ts": (
        "export function handleClick(): void {\n  console.log('clicked');\n}\n"
    ),
    "wire.ts": (
        "import { handleClick } from './handlers';\n"
        "\n"
        "export function wireUp(): void {\n"
        "  const config = {\n"
        "    onClick: handleClick,\n"
        "  };\n"
        "  console.log(config);\n"
        "}\n"
    ),
}


def test_get_callers_sites_shows_reference_line_not_def_line(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Package B: the reference sits inside wireUp's body (line 5), so
    # wireUp has its own definition line (3) distinct from the
    # reference's actual line. Before the fix, `--sites` showed
    # wireUp's definition line instead of the real reference site.
    root = make_mapped_repo(TS_CALLBACK_NESTED)
    code = cli.main(
        [
            "query",
            "callers",
            "handlers.ts:handleClick",
            "--root",
            str(root),
            "--sites",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "wire.ts:5" in out
    assert "wire.ts:3" not in out


def test_get_callers_without_sites_shows_def_line(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Without --sites, the pre-existing (coarser) behavior is
    # unchanged: the referencer's own definition line is shown.
    root = make_mapped_repo(TS_CALLBACK_NESTED)
    code = cli.main(
        ["query", "callers", "handlers.ts:handleClick", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "wire.ts:3" in out


TS_MODULE_LEVEL_ANONYMOUS_CALLBACK = {
    "target.ts": (
        "export function buddyStateDir(): string {\n  return '/tmp';\n}\n"
    ),
    "index.ts": (
        "import { buddyStateDir } from './target';\n"
        "\n"
        "document.addEventListener('load', () => {\n"
        "  buddyStateDir();\n"
        "});\n"
    ),
}


def test_get_callers_module_level_shows_line_without_sites_flag(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Round 22 claude-buddy.md §2.3: a call from inside an anonymous
    # callback (no enclosing named function) has no real symbol to be
    # a caller, so it renders as a module-level pseudo-caller row. The
    # per-site line data is already recorded in edge_lines regardless
    # of --sites; the module-level row must show it unconditionally,
    # not only when --sites is passed (unlike the named-caller default
    # path, which stays coarser without --sites).
    root = make_mapped_repo(TS_MODULE_LEVEL_ANONYMOUS_CALLBACK)
    code = cli.main(
        ["query", "callers", "target.ts:buddyStateDir", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "index.ts:4" in out
    assert "(module level)" in out


def test_module_rows_pre_v3_map_falls_back_to_bare_form() -> None:
    # A map written before doc version 3 has no edge_lines at all —
    # _module_rows must still degrade to the bare "path  (module
    # level)" form rather than crashing or emitting a bogus line.
    sym = Symbol(
        id="target.py::target",
        name="target",
        qualname="target",
        kind="function",
        path="target.py",
        language="python",
        start_line=1,
        end_line=2,
    )
    index = mapfile.MapIndex(root_label="root")
    rows = query._module_rows(index, "callers", sym, "caller.py", False)
    assert rows == ["caller.py  (module level)"]


def test_get_callers_json_sites_shows_reference_line(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_CALLBACK_NESTED)
    code = cli.main(
        [
            "query",
            "callers",
            "handlers.ts:handleClick",
            "--root",
            str(root),
            "--sites",
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["results"] == []
    referenced = doc["referenced_not_called"]
    assert len(referenced) == 1
    assert referenced[0]["path"] == "wire.ts"
    assert referenced[0]["sites"] == [5]


def test_get_callers_json_module_level_carries_lines(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Round 23 §10: `--json --sites` previously dropped module-level
    # pseudo-callers to a flat list of bare paths, even though the
    # per-site line (already recorded in edge_lines) is exactly what
    # text output shows unconditionally. module_level entries must now
    # be structured dicts carrying the line when one was recorded.
    root = make_mapped_repo(TS_MODULE_LEVEL_ANONYMOUS_CALLBACK)
    code = cli.main(
        [
            "query",
            "callers",
            "target.ts:buddyStateDir",
            "--root",
            str(root),
            "--sites",
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["module_level"] == [{"path": "index.ts", "lines": [4]}]


def test_get_callers_json_module_level_lines_unconditional_on_sites(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Matches _module_rows's text-side convention: module-level lines
    # are always attempted regardless of --sites, so JSON must agree
    # rather than gating module_level on the flag the way per-symbol
    # "sites" entries do.
    root = make_mapped_repo(TS_MODULE_LEVEL_ANONYMOUS_CALLBACK)
    code = cli.main(
        [
            "query",
            "callers",
            "target.ts:buddyStateDir",
            "--root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["module_level"] == [{"path": "index.ts", "lines": [4]}]


def test_module_level_entries_pre_v3_map_omits_lines() -> None:
    # A map written before doc version 3 has no edge_lines at all —
    # _module_level_entries must degrade to a bare {"path": ...} entry
    # with no "lines" key, matching _module_rows's bare-form fallback.
    sym = Symbol(
        id="target.py::target",
        name="target",
        qualname="target",
        kind="function",
        path="target.py",
        language="python",
        start_line=1,
        end_line=2,
    )
    index = mapfile.MapIndex(root_label="root")
    entries = query._module_level_entries(index, "callers", sym, ["caller.py"])
    assert entries == [{"path": "caller.py"}]


def test_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "symbol", "nope", "--root", str(root)])
    assert code == 3
    assert "no symbol" in capsys.readouterr().err


def test_symbol_card_json(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(
        ["query", "symbol", "entry", "--root", str(root), "--json"]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["path"] == "a.py"
    assert doc["fan_out"] == 1
    assert doc["signature"] == "entry() -> None"


def test_file_action_and_limit(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(
        ["query", "file", "a.py", "--root", str(root), "--limit", "1"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "helper" in out
    assert "1 of 2 omitted" in out
    assert "raise --limit" in out


def test_file_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "file", "zzz.py", "--root", str(root)])
    assert code == 3


def test_double_colon_path_target_resolves(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Agents fall into Rust-style file.py::name; retried, not a dead end.
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "symbol", "a.py::helper", "--root", str(root)])
    assert code == 0
    assert "helper(x: int) -> int" in capsys.readouterr().out


def test_double_colon_qualname_target_resolves(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {"c.py": "class Cache:\n    def set(self, k):\n        return k\n"}
    )
    code = cli.main(["query", "symbol", "Cache::set", "--root", str(root)])
    assert code == 0
    assert "set(self, k)" in capsys.readouterr().out


def test_not_found_lists_closest_matches(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # A wrong path qualifier with a right name should point back into
    # the map instead of ejecting the caller to grep.
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(
        ["query", "symbol", "wrong/dir.py:helper", "--root", str(root)]
    )
    assert code == 3
    err = capsys.readouterr().err
    assert "no symbol matches" in err
    assert "closest matches:" in err
    assert "a.py:1  helper(x: int) -> int" in err
    assert "b.py:1  helper(x: int) -> int" in err


def test_not_found_ranks_namespace_missing_near_miss_first(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Master report #8 (round 11, tensorflow): a C++ namespace-
    # qualified target copied without its namespace prefix
    # (``ClientSession.Run`` instead of the real
    # ``tensorflow.ClientSession.Run``) used to rank unrelated
    # same-bare-name (``Run``) candidates ahead of the real match
    # purely by alphabetically-earliest path. File names are chosen so
    # the unrelated candidates would have sorted first under the old
    # pure-alphabetical ranking, proving the qualname-suffix near-miss
    # tier now wins instead.
    root = make_mapped_repo(
        {
            "aaa_csession.cpp": (
                "class CSession {\npublic:\n    void Run() {}\n};\n"
            ),
            "bbb_other.cpp": "void Run() {}\n",
            "zzz_session.cpp": (
                "namespace tensorflow {\n"
                "class ClientSession {\n"
                "public:\n"
                "    void Run() {}\n"
                "};\n"
                "}\n"
            ),
        }
    )
    code = cli.main(
        ["query", "symbol", "ClientSession.Run", "--root", str(root)]
    )
    assert code == 3
    err = capsys.readouterr().err
    assert "closest matches:" in err
    lines = [
        line
        for line in err.splitlines()
        if line.startswith("  ") and "Run" in line
    ]
    assert lines[0].startswith("  zzz_session.cpp:")


def test_not_found_with_no_close_names_stays_bare(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "symbol", "zzz_qqq", "--root", str(root)])
    assert code == 3
    err = capsys.readouterr().err
    assert "no symbol matches" in err
    assert "closest matches:" not in err


def test_uses_on_internal_symbol_suggests_callers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # ``uses``/``find_usages`` is scoped to external (out-of-repo)
    # names; asking it about a purely internal symbol used to fail
    # with "no external reference matches" and a list of near-miss
    # *external* names, never mentioning that ``query callers`` is
    # the right command (2026-07-31 eval, reproduced on two repos).
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "uses", "helper", "--root", str(root)])
    assert code == 3
    err = capsys.readouterr().err
    assert "internal symbol" in err
    assert "query callers helper" in err
    assert "no external reference" not in err


def test_uses_warns_when_in_repo_symbol_shares_the_name(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # tensorflow's bug (B4): an in-repo ``array`` function shadowed
    # ``np.array(...)`` external-reference resolution, and
    # ``find_usages("array")`` returned a small, confident-looking but
    # wrong result set with no signal anything was off. Whenever the
    # queried name also matches an in-repo symbol, the answer must
    # carry a caveat even when the tool *did* find external results
    # (previously the caveat only fired when zero results came back).
    files = {
        "caller.py": (
            "import numpy as np\n\n\ndef entry() -> None:\n    np.array([1])\n"
        ),
        "np_like.py": "def array(x: list) -> list:\n    return x\n",
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "uses", "array", "--root", str(root)])
    assert code == 0
    out, err = capsys.readouterr()
    assert "np.array" in out
    assert "also an in-repo symbol name" in err
    assert "query callers array" in err


def test_uses_json_carries_shadow_warning(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "caller.py": (
            "import numpy as np\n\n\ndef entry() -> None:\n    np.array([1])\n"
        ),
        "np_like.py": "def array(x: list) -> list:\n    return x\n",
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "uses", "array", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert "also an in-repo symbol name" in doc["shadow_warning"]


def test_uses_no_shadow_warning_when_name_is_unique(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # No in-repo symbol named "run": no shadow caveat should appear.
    files = {
        "caller.py": (
            "import subprocess\n\n\ndef entry() -> None:\n"
            "    subprocess.run(['x'])\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "uses", "run", "--root", str(root)])
    assert code == 0
    assert "also an in-repo symbol name" not in capsys.readouterr().err


def test_query_callers_default_budget_caps_many_callers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # The CLI must enforce the same default token budget as the MCP
    # tools: previously ``dekko query callers`` with no --budget had
    # no token cap at all (only --limit's 50-row cap), so a high-fan-in
    # symbol rendered thousands of tokens (2026-07-31 eval, ~3,524
    # tokens measured on a 469-caller symbol via the CLI).
    pad = "z" * 60
    files = {"target.py": "def shared() -> int:\n    return 1\n"}
    for i in range(30):
        files[f"caller_{i}.py"] = (
            "from target import shared\n\n\n"
            f"def caller_with_a_long_padded_name_{pad}_{i}() -> int:\n"
            "    return shared()\n"
        )
    root = make_mapped_repo(files)
    code = cli.main(["query", "callers", "shared", "--root", str(root)])
    assert code == 0
    assert "omitted" in capsys.readouterr().out


def test_query_uses_default_budget_caps_many_references(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    pad = "z" * 60
    files = {}
    for i in range(30):
        files[f"caller_{i}.py"] = (
            "import subprocess\n\n\n"
            f"def caller_with_a_long_padded_name_{pad}_{i}() -> None:\n"
            "    subprocess.run(['x'])\n"
        )
    root = make_mapped_repo(files)
    code = cli.main(["query", "uses", "run", "--root", str(root)])
    assert code == 0
    assert "omitted" in capsys.readouterr().out


def test_query_callers_explicit_budget_still_wins(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # An explicit --budget must still override the default, not stack
    # with it.
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(
        [
            "query",
            "callers",
            "a.py:helper",
            "--root",
            str(root),
            "--budget",
            "100000",
        ]
    )
    assert code == 0
    assert "entry() -> None" in capsys.readouterr().out


def test_query_callers_reports_unsupported_coverage_gap(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # A "no callers" answer on a repo with unparsed files must be
    # qualified, not presented as unconditional truth (2026-07-31
    # eval, gitaustin/Astro repo: a confident-but-wrong "no callers").
    root = make_mapped_repo(
        dict(TWO_FILES, **{"Card.astro": "---\nconst x = 1;\n---\n"})
    )
    code = cli.main(["query", "callers", "a.py:entry", "--root", str(root)])
    assert code == 0
    err = capsys.readouterr().err
    assert "no parser for: astro" in err
    assert "may be incomplete" in err


# Round-08 §2.5: Java-style overloads sharing one qualname in one file
# — `path:qualname` alone can never tell them apart, since that's
# exactly the key they collide on. `_make_symbol` (extractor.py)
# disambiguates the *id* with a `#N` suffix, but `qualname`/`path`
# stay identical across every overload, which is the real repro shape.
OVERLOADED_METHODS = {
    "Foo.java": (
        "class Foo {\n"
        "    void run(int x) {\n"
        "    }\n"
        "\n"
        "    void run(String x) {\n"
        "    }\n"
        "\n"
        "    void run(int x, int y) {\n"
        "    }\n"
        "}\n"
    ),
}


def test_overload_disambiguation_via_line_qualifier(
    make_mapped_repo: RepoFactory,
) -> None:
    """3.2: `path:qualname:line` picks the exact overload out of a set
    that shares (path, qualname) — the escape hatch `path:qualname`
    alone can't provide."""
    root = make_mapped_repo(OVERLOADED_METHODS)
    index = mapfile.load_map(root)
    assert index is not None

    match, candidates = query.resolve_target(index, "Foo.java:Foo.run")
    assert match is None
    assert len(candidates) == 3

    for sym in candidates:
        target = f"Foo.java:Foo.run:{sym.start_line}"
        resolved, _ = query.resolve_target(index, target)
        assert resolved is not None
        assert resolved.id == sym.id
        assert resolved.start_line == sym.start_line


def test_overload_line_qualifier_cli_end_to_end(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(OVERLOADED_METHODS)
    code = cli.main(
        ["query", "symbol", "Foo.java:Foo.run:5", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Foo.run(x: String) -> void" in out
    assert "Foo.run(x: int) -> void" not in out


def test_overload_stale_line_falls_back_to_ambiguous_report(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """A hand-typed/stale line number that matches zero or more than
    one candidate must not crash or silently pick the wrong one — it
    falls back to the ordinary ambiguous-candidates report."""
    root = make_mapped_repo(OVERLOADED_METHODS)
    code = cli.main(
        ["query", "symbol", "Foo.java:Foo.run:9999", "--root", str(root)]
    )
    assert code == 4
    err = capsys.readouterr().err
    assert "is ambiguous" in err
    assert "Foo.java:2" in err
    assert "Foo.java:5" in err
    assert "Foo.java:8" in err


def test_overload_ambiguous_report_hints_line_qualifier(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """report_unresolved's hint must point at the `:LINE` form
    specifically when every candidate shares (path, qualname) — a
    plain `file.py:{target}` qualifier (the pre-existing truncation
    hint) can't narrow an overload set at all."""
    root = make_mapped_repo(OVERLOADED_METHODS)
    code = cli.main(
        ["query", "symbol", "Foo.java:Foo.run", "--root", str(root)]
    )
    assert code == 4
    err = capsys.readouterr().err
    assert "can't disambiguate" in err
    assert "Foo.java:Foo.run:2" in err


def test_truncation_hint_not_duplicated_for_qualified_overload_target(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """round-15 (cline): querying an already ``path:qualname`` target
    that still resolves to a same-file/same-qualname overload set past
    the truncation cap must not build the "+N more" hint by prepending
    ``sample.path`` onto the already-qualified ``target`` string — that
    produced a duplicated ``path:path:qualname`` hint. The hint must be
    built from the candidate's own bare ``qualname`` instead."""
    overloads = "".join(
        f"    void run(int p{i}) {{\n    }}\n\n" for i in range(25)
    )
    root = make_mapped_repo({"Foo.java": f"class Foo {{\n{overloads}}}\n"})
    code = cli.main(
        ["query", "symbol", "Foo.java:Foo.run", "--root", str(root)]
    )
    assert code == 4
    err = capsys.readouterr().err
    assert "Foo.java:Foo.java:Foo.run" not in err
    assert "`Foo.java:Foo.run`" in err


def test_ambiguous_bare_name_no_overload_hint_for_distinct_paths(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """The overload-specific hint must not fire for the ordinary
    same-name-different-file ambiguity (TWO_FILES) — those candidates
    don't share (path, qualname), so `file.py:target` already narrows
    them and the line-qualifier hint would be noise."""
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(["query", "symbol", "helper", "--root", str(root)])
    assert code == 4
    err = capsys.readouterr().err
    assert "can't disambiguate" not in err


def test_json_flag_has_no_effect_on_ambiguous_error_output(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """Round-12 §3.15/§6: `--json` is a deliberate, documented no-op
    on every error path — `report_unresolved` always prints plain text
    to stderr regardless of `--json`, and the exit code is the only
    machine-readable signal for this case. This locks in the current,
    now-deliberate contract so any future accidental drift toward
    "sometimes emits JSON errors" is caught."""
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(
        ["query", "symbol", "helper", "--root", str(root), "--json"]
    )
    assert code == query.EXIT_AMBIGUOUS
    out, err = capsys.readouterr()
    assert out == ""
    assert "is ambiguous" in err
    with pytest.raises(json.JSONDecodeError):
        json.loads(err)


def test_json_flag_has_no_effect_on_not_found_error_output(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """Same contract as the ambiguous case, for the not-found path."""
    root = make_mapped_repo(TWO_FILES)
    code = cli.main(
        ["query", "symbol", "does_not_exist", "--root", str(root), "--json"]
    )
    assert code == query.EXIT_NOT_FOUND
    out, err = capsys.readouterr()
    assert out == ""
    assert "no symbol matches" in err
    with pytest.raises(json.JSONDecodeError):
        json.loads(err)


# --- plan 26: --sites footer/JSON self-reports both totals -----------

MULTI_SITE_CALLERS = {
    "target.py": ("def target(x: int) -> int:\n    return x + 1\n"),
    "caller.py": (
        "def caller_a() -> int:\n"
        "    a = target(1)\n"
        "    b = target(2)\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def caller_b() -> int:\n"
        "    return target(3)\n"
    ),
}


def test_sites_footer_shows_related_total_untruncated(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # 2 distinct callers (caller_a, caller_b), 3 call sites (caller_a
    # calls twice) — the load-bearing divergence this design closes.
    root = make_mapped_repo(MULTI_SITE_CALLERS)
    code = cli.main(
        [
            "query",
            "callers",
            "target.py:target",
            "--root",
            str(root),
            "--sites",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    footer = lines[-1]
    assert "· 2 callers" in footer
    assert "omitted" not in footer


def test_sites_footer_truncated_shows_both_totals(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(MULTI_SITE_CALLERS)
    code = cli.main(
        [
            "query",
            "callers",
            "target.py:target",
            "--root",
            str(root),
            "--sites",
            "--limit",
            "2",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    footer = lines[-1]
    assert "· 2 callers" in footer
    assert "1 of 3 sites omitted" in footer


def test_sites_footer_callees_uses_callees_label(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(MULTI_SITE_CALLERS)
    code = cli.main(
        [
            "query",
            "callees",
            "caller.py:caller_a",
            "--root",
            str(root),
            "--sites",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    footer = out.strip().splitlines()[-1]
    assert "callees" in footer


def test_plain_footer_unchanged_by_related_total(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Regression guard: plain mode (sites=False) footer is byte-for-
    # byte unchanged — no related-count clause, no "sites" row noun.
    root = make_mapped_repo(MULTI_SITE_CALLERS)
    code = cli.main(
        ["query", "callers", "target.py:target", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    footer = out.strip().splitlines()[-1]
    assert "callers" not in footer
    assert "callees" not in footer
    assert "sites" not in footer


def test_json_sites_total_present_and_independent_of_truncation(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(MULTI_SITE_CALLERS)
    code = cli.main(
        [
            "query",
            "callers",
            "target.py:target",
            "--root",
            str(root),
            "--sites",
            "--json",
            "--limit",
            "1",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["meta"]["sites_total"] == 3
    assert doc["meta"]["total"] == 2
    assert doc["meta"]["returned"] == 1


def test_json_sites_meta_related_total_populated(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Round 25 finding #7: --sites --json's meta.related_total/
    # related_label stayed 0/"" (the Meter defaults) because the JSON
    # path never forwarded them to _fit_entries, unlike the text-mode
    # footer, which already got them right.
    root = make_mapped_repo(MULTI_SITE_CALLERS)
    code = cli.main(
        [
            "query",
            "callers",
            "target.py:target",
            "--root",
            str(root),
            "--sites",
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["meta"]["related_total"] == 2
    assert doc["meta"]["related_label"] == "callers"


def test_json_without_sites_has_no_sites_total_key(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(MULTI_SITE_CALLERS)
    code = cli.main(
        [
            "query",
            "callers",
            "target.py:target",
            "--root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert "sites_total" not in doc["meta"]


def test_json_sites_total_fallback_counts_missing_lines_as_one(
    capsys: pytest.CaptureFixture,
) -> None:
    # A map with no recorded edge_lines (pre-v3 map semantics, or an
    # edge dekko genuinely couldn't line-locate) must still count each
    # entry/module toward sites_total as 1, not 0 -- otherwise JSON's
    # sites_total and text's site-row TOTAL could quietly diverge on
    # this specific edge, the same bug class as the off-by-one plans.
    target = Symbol(
        id="target.py::target",
        name="target",
        qualname="target",
        kind="function",
        path="target.py",
        language="python",
        start_line=1,
        end_line=2,
    )
    caller = Symbol(
        id="caller.py::caller",
        name="caller",
        qualname="caller",
        kind="function",
        path="caller.py",
        language="python",
        start_line=1,
        end_line=2,
    )
    index = mapfile.MapIndex(root_label="root")
    query._print_relation_json(
        index,
        "callers",
        target,
        [caller],
        ["module.py"],
        True,
        None,
        10,
        None,
        0,
        0,
    )
    doc = json.loads(capsys.readouterr().out)
    # 1 (caller, no recorded edge_lines) + 1 (module, no recorded
    # edge_lines) == 2, not 0.
    assert doc["meta"]["sites_total"] == 2
