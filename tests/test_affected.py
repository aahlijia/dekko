"""The affected subcommand: impacted test selection and exit codes."""

import json
import subprocess
from pathlib import Path

import pytest

from dekko import affected, cli, mapfile
from dekko import server
from dekko.model import Import, Symbol

# core() is called directly by one test, transitively (via wrapper) by
# another, only imported by a third, and unrelated to a fourth.
BASE = {
    "src/app.py": (
        "def core() -> int:\n"
        "    return 1\n"
        "\n"
        "\n"
        "def wrapper() -> int:\n"
        "    return core()\n"
    ),
    "src/other.py": "def helper() -> int:\n    return 9\n",
    "tests/test_direct.py": (
        "from src.app import core\n"
        "\n"
        "\n"
        "def test_core():\n"
        "    assert core() == 1\n"
    ),
    "tests/test_transitive.py": (
        "from src.app import wrapper\n"
        "\n"
        "\n"
        "def test_wrapper():\n"
        "    assert wrapper() == 1\n"
    ),
    "tests/test_import_only.py": (
        "from src.app import core\n"
        "\n"
        "\n"
        "REF = core\n"
        "\n"
        "\n"
        "def test_ref():\n"
        "    assert REF is not None\n"
    ),
    "tests/test_unrelated.py": (
        "from src.other import helper\n"
        "\n"
        "\n"
        "def test_helper():\n"
        "    assert helper() == 9\n"
    ),
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True
    )


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-m",
        message,
    )


def _repo(root: Path, files: dict[str, str]) -> Path:
    _git(root, "init", "-q")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _commit_all(root, "base")
    assert cli.main(["map", str(root), "--quiet"]) == 0
    return root


def _change_core(root: Path) -> None:
    (root / "src/app.py").write_text(
        "def core() -> int:\n"
        "    return 2\n"
        "\n"
        "\n"
        "def wrapper() -> int:\n"
        "    return core()\n"
    )


def test_clean_tree_has_no_impact(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    assert cli.main(["affected", "--root", str(root)]) == 0
    assert "no impacted tests" in capsys.readouterr().out


def test_tiers_direct_transitive_import(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)

    assert cli.main(["affected", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "[direct] tests/test_direct.py" in out
    assert "[transitive] tests/test_transitive.py" in out
    assert "[import] tests/test_import_only.py" in out
    assert "test_unrelated.py" not in out


def test_pytest_hint_lists_impacted_files(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)

    assert cli.main(["affected", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    hint = next(ln for ln in out.splitlines() if ln.startswith("pytest "))
    assert "tests/test_direct.py" in hint
    assert "tests/test_import_only.py" in hint
    assert "tests/test_unrelated.py" not in hint


def test_pytest_hint_caps_a_large_impact_set(tmp_path: Path) -> None:
    # tensorflow's bug (B6): a real ~1,500-impact repo embedded every
    # path in this one line, blowing a workset budget 3.6x over its
    # stated cap. The hint must truncate like every other list in the
    # tool instead of enumerating every path unconditionally.
    impacts = [
        affected.TestImpact(path=f"tests/test_{i}.py", tier="direct")
        for i in range(50)
    ]
    hint = affected._test_hint(impacts, tmp_path)
    shown = hint.split("#")[0].split()
    assert len(shown) == 1 + affected._MAX_HINT_PATHS  # "pytest" + paths
    assert "+30 more impacted test files not shown" in hint


def test_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)

    assert cli.main(["affected", "--root", str(root), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    by_path = {i["path"]: i for i in doc["impacted"]}
    assert by_path["tests/test_direct.py"]["tier"] == "direct"
    assert by_path["tests/test_transitive.py"]["tier"] == "transitive"
    assert by_path["tests/test_import_only.py"]["tier"] == "import"
    assert by_path["tests/test_direct.py"]["symbols"][0]["id"] == (
        "tests/test_direct.py::test_core"
    )
    assert doc["command"].startswith("pytest ")


def test_editing_a_test_marks_it_direct(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, BASE)
    (root / "tests/test_direct.py").write_text(
        "from src.app import core\n"
        "\n"
        "\n"
        "def test_core():\n"
        "    assert core() == 1  # tweaked\n"
    )
    assert cli.main(["affected", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "[direct] tests/test_direct.py" in out
    # Nothing else changed, so no other test file is impacted.
    assert "test_transitive.py" not in out
    assert "test_import_only.py" not in out


def test_bad_rev(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _repo(tmp_path, BASE)
    assert cli.main(["affected", "nope-not-a-rev", "--root", str(root)]) == 2
    assert "cannot export git rev" in capsys.readouterr().err


VENDORED_ONLY = {
    **BASE,
    "third_party/lib.py": "def helper() -> int:\n    return 1\n",
}


def test_no_impact_on_vendored_only_change_carries_coverage_note(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # Track E's optional item, closed as a follow-up: a diff that only
    # touches vendored-excluded files (e.g. tensorflow's
    # ``third_party/xla``) is invisible to the diff pipeline entirely
    # (the walker never mapped it, so it produces no symbol delta), so
    # "no impacted tests" would otherwise read identically to a
    # genuinely safe change. The report must carry the same coverage
    # caveat ``query``'s not-found replies already have.
    root = _repo(tmp_path, VENDORED_ONLY)
    (root / "third_party/lib.py").write_text(
        "def helper() -> int:\n    return 2\n"
    )
    assert cli.main(["affected", "--root", str(root)]) == 0
    out = capsys.readouterr()
    assert "no impacted tests" in out.out
    assert "third_party" in out.err
    assert "default-excluded directories" in out.err
    assert "this answer may be incomplete" in out.err


def test_json_no_impact_on_vendored_only_change_has_coverage_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _repo(tmp_path, VENDORED_ONLY)
    (root / "third_party/lib.py").write_text(
        "def helper() -> int:\n    return 2\n"
    )
    assert cli.main(["affected", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "third_party" in doc["coverage_warning"]


def test_mcp_impacted_tests(tmp_path: Path) -> None:
    root = _repo(tmp_path, BASE)
    _change_core(root)
    ctx = server.Context(default_root=root, no_regen=False)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "impacted_tests", "arguments": {}},
    }
    result = server.handle(ctx, msg)["result"]
    assert not result["isError"]
    text = result["content"][0]["text"]
    assert "tests/test_direct.py" in text


def test_impacted_tests_in_tool_list() -> None:
    names = {t["name"] for t in server.TOOLS}
    assert "impacted_tests" in names


# --- 1.3: language-aware test hints -----------------------------------


def test_test_hint_python_only_matches_historical_pytest_format(
    tmp_path: Path,
) -> None:
    """Regression guard: a Python-only impact set must render byte-
    identical to the old hardcoded ``pytest ...`` behavior."""
    impacts = [
        affected.TestImpact(path="tests/test_a.py", tier="direct"),
        affected.TestImpact(path="tests/test_b.py", tier="import"),
    ]
    hint = affected._test_hint(impacts, tmp_path)
    assert hint == "pytest tests/test_a.py tests/test_b.py"


def test_test_hint_rust_has_no_hardcoded_pytest(tmp_path: Path) -> None:
    impacts = [affected.TestImpact(path="src/lib_test.rs", tier="direct")]
    hint = affected._test_hint(impacts, tmp_path)
    assert hint.startswith("cargo test")
    assert "pytest" not in hint
    assert "src/lib_test.rs" in hint


def test_test_hint_go_has_no_hardcoded_pytest(tmp_path: Path) -> None:
    impacts = [affected.TestImpact(path="pkg/foo_test.go", tier="direct")]
    hint = affected._test_hint(impacts, tmp_path)
    assert hint.startswith("go test ./...")
    assert "pytest" not in hint


def test_test_hint_js_reads_declared_bun_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "bun test"}}')
    (tmp_path / "bun.lock").write_text("")
    impacts = [affected.TestImpact(path="src/foo.test.ts", tier="direct")]
    hint = affected._test_hint(impacts, tmp_path)
    assert hint == "bun run test"


def test_test_hint_js_defaults_to_npm_without_a_lockfile_signal(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}')
    impacts = [affected.TestImpact(path="src/foo.test.ts", tier="direct")]
    assert affected._test_hint(impacts, tmp_path) == "npm test"


def test_test_hint_js_omitted_without_a_declared_test_script(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {}}')
    impacts = [affected.TestImpact(path="src/foo.test.ts", tier="direct")]
    assert affected._test_hint(impacts, tmp_path) == ""


def test_test_hint_jvm_prefers_gradle_over_maven(tmp_path: Path) -> None:
    (tmp_path / "gradlew").write_text("")
    (tmp_path / "pom.xml").write_text("<project/>")
    impacts = [
        affected.TestImpact(path="src/test/FooTest.java", tier="direct")
    ]
    assert affected._test_hint(impacts, tmp_path) == "./gradlew test"


def test_test_hint_unmapped_language_is_omitted(tmp_path: Path) -> None:
    impacts = [affected.TestImpact(path="test/foo_test.rb", tier="direct")]
    assert affected._test_hint(impacts, tmp_path) == ""


def test_test_hint_groups_mixed_languages_into_separate_lines(
    tmp_path: Path,
) -> None:
    impacts = [
        affected.TestImpact(path="tests/test_a.py", tier="direct"),
        affected.TestImpact(path="pkg/foo_test.go", tier="import"),
    ]
    hint = affected._test_hint(impacts, tmp_path)
    lines = hint.splitlines()
    assert any(ln.startswith("pytest ") for ln in lines)
    assert any(ln.startswith("go test ./...") for ln in lines)


# --- 1.5-remainder: import-tier fallback for a symbol seed -------------


def _sym(path: str, name: str) -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind="function",
        path=path,
        language="cpp",
        start_line=1,
        end_line=2,
    )


def test_impacts_from_symbol_falls_back_to_import_tier() -> None:
    """investigation-1.5-cpp-gtest-affected.md: a C++-style
    cross-file call that the resolver drops as ``ambiguous`` never
    reaches ``calls_in``, so a pure call-edge walk sees zero impacted
    tests despite a real, direct test call. ``impacts_from_symbol``
    must fall back to import evidence (a test file that ``#include``s
    the seed's own file) the same way ``analyze()``'s diff path
    already does.
    """
    seed = _sym("rewrite_utils.cc", "GetGrapplerItem")
    test_sym = _sym("rewrite_utils_test.cc", "TEST")
    test_sym.test = True
    index = mapfile.MapIndex(root_label="repo")
    index.symbols_by_id[seed.id] = seed
    index.symbols_by_id[test_sym.id] = test_sym
    # No calls_in entry for seed.id: the call was dropped as ambiguous,
    # exactly like the resolver does for C++ same-named candidates.
    index.imports_by_path["rewrite_utils_test.cc"] = [
        Import(
            path="rewrite_utils_test.cc",
            name="",
            source="rewrite_utils.h",
        )
    ]

    impacts = affected.impacts_from_symbol(index, {seed.id})

    assert [i.path for i in impacts] == ["rewrite_utils_test.cc"]
    assert impacts[0].tier == "import"


def test_impacts_from_symbol_still_finds_direct_call_edges() -> None:
    """The new import-tier fallback must not shadow a real call edge —
    a direct caller still wins the stronger ``direct``/``transitive``
    tier over any coincidental import match."""
    seed = _sym("a.py", "helper")
    caller = _sym("tests/test_a.py", "test_helper")
    caller.test = True
    index = mapfile.MapIndex(root_label="repo")
    index.symbols_by_id[seed.id] = seed
    index.symbols_by_id[caller.id] = caller
    index.calls_in[seed.id] = [caller.id]

    impacts = affected.impacts_from_symbol(index, {seed.id})

    assert [i.path for i in impacts] == ["tests/test_a.py"]
    assert impacts[0].tier == "direct"
