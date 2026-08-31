"""Context packs: neighborhood building, hops, budget trimming."""

import json

import pytest

from dekko.integrations import cli
from dekko.analysis import contextpack
from dekko.render import mapfile
from dekko.core.model import Symbol

from conftest import RepoFactory

CHAIN3 = {
    "chain.py": (
        "def low() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def mid() -> int:\n"
        "    return low()\n"
        "\n"
        "\n"
        "def top() -> int:\n"
        "    return mid()\n"
    )
}


def _resolved(root, name):  # noqa: ANN001, ANN202
    index = mapfile.load_map(root)
    return index, index.symbols_by_qualname[name][0]


def test_hop1_pack(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN3)
    index, mid = _resolved(root, "mid")
    pack = contextpack.build_pack(index, mid, hops=1)
    names = {(e.sym.qualname, e.direction) for e in pack.entries}
    assert names == {("top", "caller"), ("low", "callee")}


def test_hops2_grows_pack(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(CHAIN3)
    index, top = _resolved(root, "top")
    pack1 = contextpack.build_pack(index, top, hops=1)
    pack2 = contextpack.build_pack(index, top, hops=2)
    assert len(pack2.entries) > len(pack1.entries)
    assert {e.sym.qualname for e in pack2.entries} == {"mid", "low"}
    assert {e.hop for e in pack2.entries} == {1, 2}


# Round 26: a hop-1 caller of the target also calls an unrelated
# third function for its own reasons. Before the direction-lock fix,
# _neighbors() expanded *both* calls_in and calls_out from every
# frontier node at hop >= 2, so `side_effect` (a callee of `caller`,
# with no relationship to `target`) leaked in mislabeled as a hop-2
# "callee" of `target`.
CALLER_SIDE_CONTAMINATION = {
    "chain.py": (
        "def target() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def caller() -> int:\n"
        "    side_effect()\n"
        "    return target()\n"
        "\n"
        "\n"
        "def side_effect() -> int:\n"
        "    return 2\n"
    )
}

# Symmetric case: a hop-1 callee of the target has an unrelated extra
# caller. Before the fix, that extra caller leaked in mislabeled as a
# hop-2 "caller" of `target`.
CALLEE_SIDE_CONTAMINATION = {
    "chain.py": (
        "def target() -> int:\n"
        "    return helper()\n"
        "\n"
        "\n"
        "def helper() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def other_caller() -> int:\n"
        "    return helper()\n"
    )
}


def test_hop2_excludes_hop1_callers_unrelated_callees(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CALLER_SIDE_CONTAMINATION)
    index, target = _resolved(root, "target")
    pack = contextpack.build_pack(index, target, hops=2)
    names = {e.sym.qualname for e in pack.entries}
    assert "caller" in names
    assert "side_effect" not in names


def test_hop2_excludes_hop1_callees_unrelated_callers(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CALLEE_SIDE_CONTAMINATION)
    index, target = _resolved(root, "target")
    pack = contextpack.build_pack(index, target, hops=2)
    names = {e.sym.qualname for e in pack.entries}
    assert "helper" in names
    assert "other_caller" not in names


ANON_CALLER = {
    "a.py": "def helper() -> int:\n    return 1\n",
    "b.py": "from a import helper\n\nhelper()\n",
}


def test_module_caller_promoted_to_pack_entry(
    make_mapped_repo: RepoFactory,
) -> None:
    # Bug #4: a call site with no named enclosing function (here,
    # b.py's top-level `helper()`) must land in pack.entries with a
    # real line number, not only the terser, line-number-less
    # module_callers bucket — otherwise it's easy to miss when
    # skimming and undercounts what looks like the complete caller
    # list.
    root = make_mapped_repo(ANON_CALLER)
    index, helper = _resolved(root, "helper")
    pack = contextpack.build_pack(index, helper, hops=1)

    # Backward-compatible bucket is still populated.
    assert pack.module_callers == ["b.py"]

    anon = [e for e in pack.entries if e.sym.kind == "module"]
    assert len(anon) == 1
    entry = anon[0]
    assert entry.direction == "caller"
    assert entry.hop == 1
    assert entry.sym.path == "b.py"
    assert entry.sym.start_line == 3

    text = contextpack.render_text(pack)
    assert "callers:" in text
    assert "b.py:3  <anonymous> (b.py)" in text
    # b.py's only call site was fully promoted into a PackEntry above,
    # so the terser, line-number-less trailing line would just repeat
    # the same file with strictly less precision — it must not appear.
    assert "module-level callers:" not in text


def test_file_mode_module_caller_promoted_to_pack_entry(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(ANON_CALLER)
    index = mapfile.load_map(root)
    pack = contextpack.build_file_pack(index, "a.py")

    assert pack.module_callers == ["b.py"]
    anon = [e for e in pack.entries if e.sym.kind == "module"]
    assert len(anon) == 1
    assert anon[0].sym.path == "b.py"
    assert anon[0].sym.start_line == 3

    # Same fully-covered case as the symbol-mode pack above: b.py is
    # already represented by the promoted entry, so it must not also
    # show up in the redundant trailing summary line.
    text = contextpack.render_text(pack)
    assert "module-level callers:" not in text


def test_residual_module_caller_shown_when_not_promoted() -> None:
    # Pre-v3 maps have no edge_lines, so _anonymous_entries() returns
    # [] and nothing gets promoted into pack.entries — the trailing
    # summary line is the only place a module-level caller's presence
    # survives, so it must still be printed for this path.
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
    pack = contextpack.Pack(
        label="target.py:target",
        target=target,
        file_path="target.py",
        module_callers=["c.py"],
    )
    text = contextpack.render_text(pack)
    assert "module-level callers: c.py" in text


def test_file_mode_self_caller_never_surfaced(
    make_mapped_repo: RepoFactory,
) -> None:
    # build_file_pack deliberately treats a file's own top-level code
    # calling its own function as not worth surfacing ("outside
    # callers" framing): it is excluded from promotion to a PackEntry
    # *and* filtered out of pack.module_callers itself, so it must not
    # appear anywhere in the rendered text either, before or after
    # this fix.
    files = {
        "a.py": "def helper() -> int:\n    return 1\n\n\nhelper()\n",
    }
    root = make_mapped_repo(files)
    index = mapfile.load_map(root)
    pack = contextpack.build_file_pack(index, "a.py")

    assert pack.module_callers == []
    assert pack.entries == []
    text = contextpack.render_text(pack)
    assert "module-level callers:" not in text


def test_budget_trims_but_keeps_target(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CHAIN3)
    code = cli.main(
        [
            "context",
            "mid",
            "--root",
            str(root),
            "--hops",
            "2",
            "--budget",
            "30",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "context: chain.py:mid" in out
    assert "mid() -> int" in out
    assert "omitted" in out
    assert "raise --budget" in out


def test_file_mode_pack(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = dict(
        CHAIN3,
        **{
            "user.py": (
                "from chain import top\n"
                "\n"
                "\n"
                "def run() -> int:\n"
                "    return top()\n"
            )
        },
    )
    root = make_mapped_repo(files)
    code = cli.main(["context", "chain.py", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["target"] is None
    own = {s["signature"] for s in doc["file_symbols"]}
    assert "top() -> int" in own
    callers = {n["path"] for n in doc["neighbors"]}
    assert callers == {"user.py"}


def test_context_not_found(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CHAIN3)
    assert cli.main(["context", "ghost", "--root", str(root)]) == 3


# A file whose only relevant import is `Path` (used by the target's
# own signature) alongside 20 unrelated stdlib imports never touched
# by the target or its one neighbor — mirrors the manual eval's
# ~36-import cli.py case (eval/manual_token_test.md, Task 3).
_UNRELATED_IMPORTS = "\n".join(
    f"import {name}"
    for name in (
        "os", "sys", "json", "re", "io", "csv", "math", "time",
        "uuid", "shutil", "socket", "struct", "base64", "hashlib",
        "logging", "platform", "tempfile", "threading", "traceback",
        "functools",
    )
)  # fmt: skip
MANY_IMPORTS = {
    "app.py": (
        f"{_UNRELATED_IMPORTS}\n"
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def helper(root: Path) -> Path:\n"
        '    """Resolve root as a Path."""\n'
        "    return root\n"
        "\n"
        "\n"
        "def caller() -> Path:\n"
        "    return helper(Path('.'))\n"
    )
}


def test_build_pack_filters_irrelevant_imports(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(MANY_IMPORTS)
    index, helper = _resolved(root, "helper")
    all_imports = index.imports_by_path["app.py"]
    assert len(all_imports) == 21  # 20 unrelated + Path

    pack = contextpack.build_pack(index, helper, hops=1)

    kept = {imp.name for imp in pack.imports}
    assert kept == {"Path"}
    assert pack.imports_dropped == 20


def test_build_pack_import_filter_shrinks_output(
    make_mapped_repo: RepoFactory,
) -> None:
    """Filtering cuts the rendered import block by a large margin."""
    root = make_mapped_repo(MANY_IMPORTS)
    index, helper = _resolved(root, "helper")
    all_imports = index.imports_by_path["app.py"]

    def rendered_len(imports: list) -> int:
        return sum(
            len(f"  {imp.name}  (from {imp.source})\n") for imp in imports
        )

    unfiltered_chars = rendered_len(all_imports)
    pack = contextpack.build_pack(index, helper, hops=1)
    filtered_chars = rendered_len(pack.imports)

    assert filtered_chars < unfiltered_chars * 0.2

    text = contextpack.render_text(pack)
    assert "Path  (from pathlib.Path)" in text
    assert "os  (from os)" not in text
    assert "+20 more imports" in text
    assert "rerun with --all-imports to include them" in text


def test_build_pack_all_imports_skips_relevance_filter(
    make_mapped_repo: RepoFactory,
) -> None:
    # C.2: --all-imports opts out of _relevant_imports filtering
    # entirely, so every import in the target's file survives and
    # imports_dropped reads 0.
    root = make_mapped_repo(MANY_IMPORTS)
    index, helper = _resolved(root, "helper")
    all_imports = index.imports_by_path["app.py"]

    pack = contextpack.build_pack(index, helper, hops=1, all_imports=True)

    assert len(pack.imports) == len(all_imports)
    assert pack.imports_dropped == 0
    assert {imp.name for imp in pack.imports} >= {"Path", "os"}


def test_build_pack_default_still_filters_imports(
    make_mapped_repo: RepoFactory,
) -> None:
    # C.2: default behavior (all_imports omitted) must be unchanged.
    root = make_mapped_repo(MANY_IMPORTS)
    index, helper = _resolved(root, "helper")
    pack = contextpack.build_pack(index, helper, hops=1)
    kept = {imp.name for imp in pack.imports}
    assert kept == {"Path"}
    assert pack.imports_dropped == 20


def test_cli_context_all_imports_includes_dropped_import(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # C.2: end-to-end, dekko context TARGET --all-imports includes an
    # import that plain dekko context TARGET drops.
    root = make_mapped_repo(MANY_IMPORTS)
    code = cli.main(["context", "app.py:helper", "--root", str(root)])
    assert code == 0
    default_out = capsys.readouterr().out
    assert "os  (from os)" not in default_out

    code = cli.main(
        [
            "context",
            "app.py:helper",
            "--root",
            str(root),
            "--all-imports",
        ]
    )
    assert code == 0
    all_out = capsys.readouterr().out
    assert "os  (from os)" in all_out
    assert "more imports" not in all_out


JS_MULTI_NAME_IMPORT = {
    "server/index.ts": (
        "import { generateBones, generatePersonality } "
        'from "./engine";\n\n'
        "export function ensureCompanion(): void {\n"
        "  generateBones();\n"
        "  generatePersonality();\n"
        "}\n"
    ),
    "server/engine.ts": (
        "export function generateBones(): void {}\n\n"
        "export function generatePersonality(): void {}\n"
    ),
}


def test_render_text_strips_js_named_import_suffix(
    make_mapped_repo: RepoFactory,
) -> None:
    # I2/round-22 item 6: a JS/TS multi-name import (`import { A, B }
    # from "./engine";`) stores each name's Import.source as
    # "./engine/A"/"./engine/B" for resolver-internal disambiguation —
    # but that's not a real submodule path on disk, and must not be
    # displayed as one (claude-buddy.md §2.2).
    root = make_mapped_repo(JS_MULTI_NAME_IMPORT)
    index, ensure = _resolved(root, "ensureCompanion")
    pack = contextpack.build_pack(index, ensure, hops=1)
    text = contextpack.render_text(pack)
    assert "(from ./engine)" in text
    assert "(from ./engine/generateBones)" not in text
    assert "(from ./engine/generatePersonality)" not in text


def test_trim_to_budget_drops_imports_before_source(
    make_mapped_repo: RepoFactory,
) -> None:
    """A tight budget trims imports even with no neighbors to cut."""
    files = {
        "solo.py": (
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "\n"
            "def lonely(root: Path) -> Path:\n"
            "    return root\n"
        )
    }
    root = make_mapped_repo(files)
    index, lonely = _resolved(root, "lonely")
    pack = contextpack.build_pack(index, lonely, hops=1)
    assert not pack.entries  # no callers/callees to trim first
    assert [imp.name for imp in pack.imports] == ["Path"]

    contextpack.trim_to_budget(index, pack, budget=1)

    assert pack.imports == []
    assert pack.trimmed >= 1
    # The target's signature always survives, budget or not.
    assert "lonely(root: Path) -> Path" in contextpack.render_text(pack)


def test_trim_to_budget_protects_callers_over_imports(
    make_mapped_repo: RepoFactory,
) -> None:
    # B5: four evaluators hit a context pack that spent its entire
    # default budget on the import list and returned 0% of the
    # callers/callees actually asked about. Imports must be the first
    # thing dropped under a tight budget, not the last — a caller's
    # real callers/callees must never be zeroed out while a (still
    # relevant) import list survives untouched.
    files = {
        "app.py": (
            "from pathlib import Path\n"
            "from typing import Optional\n"
            "\n\n"
            "def helper(root: Path, extra: Optional[str]) -> Path:\n"
            '    """Resolve root as a path."""\n'
            "    return root\n"
            "\n\n"
            "def caller_one() -> Path:\n"
            "    return helper(Path('.'), None)\n"
        )
    }
    root = make_mapped_repo(files)
    index, helper = _resolved(root, "helper")
    pack = contextpack.build_pack(index, helper, hops=1)
    assert pack.imports  # both imports survived relevance filtering
    assert pack.entries  # caller_one is a hop-1 caller

    contextpack.trim_to_budget(index, pack, budget=45)

    assert pack.imports == []
    assert any(e.sym.qualname == "caller_one" for e in pack.entries)


def test_run_applies_default_pack_budget(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    """Omitting --budget still caps a pack via DEFAULT_PACK_BUDGET."""
    root = make_mapped_repo(MANY_IMPORTS)
    code = cli.main(["context", "helper", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["meta"]["budget"] == contextpack.DEFAULT_PACK_BUDGET


# round23 issue 07: get_context_pack silently dropped the "N
# ambiguous, not counted" disclosure that query callers/callees and
# the CLI both show — a.py:target's fan-in looks exhaustive (no
# calls_in edge) even though c.py's call to the bare name "target"
# exists but resolved ambiguously against a.py/b.py's same-named
# definitions.
AMBIGUOUS_CALL = {
    "a.py": "def target() -> int:\n    return 1\n",
    "b.py": "def target() -> int:\n    return 2\n",
    "c.py": "def caller() -> int:\n    return target()\n",
}


def test_build_pack_carries_ambiguous_in_count(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    index, target = _resolved(root, "target")
    pack = contextpack.build_pack(index, target, hops=1)
    assert pack.ambig_in == 1
    assert pack.ambig_out == 0


def test_render_text_notes_ambiguous_in(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    index, target = _resolved(root, "target")
    pack = contextpack.build_pack(index, target, hops=1)
    text = contextpack.render_text(pack)
    assert (
        "note: 1 additional call site(s) named 'target' resolved "
        "ambiguously — not counted here" in text
    )


def test_render_text_notes_ambiguous_out(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    index, caller = _resolved(root, "caller")
    pack = contextpack.build_pack(index, caller, hops=1)
    text = contextpack.render_text(pack)
    assert (
        "note: 1 outgoing call(s) from this symbol resolved "
        "ambiguously (name matched 2+ candidates) — not counted here" in text
    )


def test_render_text_no_ambiguous_note_when_zero(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(CHAIN3)
    index, mid = _resolved(root, "mid")
    pack = contextpack.build_pack(index, mid, hops=1)
    assert pack.ambig_in == 0
    assert pack.ambig_out == 0
    assert "resolved ambiguously" not in contextpack.render_text(pack)


def test_context_pack_json_carries_ambiguous_in(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(AMBIGUOUS_CALL)
    code = cli.main(["context", "a.py:target", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ambiguous_in"] == 1
    assert "ambiguous_out" not in doc


def test_context_pack_json_no_ambiguous_key_when_zero(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(CHAIN3)
    code = cli.main(["context", "mid", "--root", str(root), "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert "ambiguous_in" not in doc
    assert "ambiguous_out" not in doc
