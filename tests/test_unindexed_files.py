"""Files dekko recognizes and does not index: Groovy and build scripts.

A Groovy source file has no usable parser, so it is skipped and
counted in the coverage note every empty answer carries. A Gradle
build script is skipped by design and counted on its own line, off the
per-query notes, except where the symbol asked about is build logic.
"""

import json
from pathlib import Path

import pytest

from dekko.analysis import sanity
from dekko.integrations import cli, server
from dekko.render import mapfile

from conftest import RepoFactory

REPO = {
    "src/main/java/a/Tool.java": (
        "package a;\n"
        "public class Tool {\n"
        "  public void runIt() {}\n"
        "  public static void main(String[] x) { new Tool().helper(); }\n"
        "  void helper() {}\n"
        "}\n"
    ),
    "buildSrc/src/main/java/b/Logic.java": (
        "package b;\n"
        "public class Logic {\n"
        "  public void normalizePort() {}\n"
        "  public String forDocs() { return null; }\n"
        "}\n"
    ),
    "build.gradle": (
        'plugins { id "java" }\n'
        'tasks.register("go", a.Tool) { runIt() }\n'
        'tasks.register("fix", b.Logic) { normalizePort() }\n'
        "println logic.forDocs()\n"
        "def helper(x) { return x }\n"
    ),
    "ToolSpec.groovy": (
        "class ToolSpec extends spock.lang.Specification {\n"
        '  def "runs it"() { expect: new a.Tool().runIt() == null }\n'
        "}\n"
    ),
}

BUILD_SCRIPTS = {"count": 1, "languages": {"gradle": 1}}
LOGIC = "buildSrc/src/main/java/b/Logic.java::Logic.normalizePort"


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _no_dekko_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every grep hit land in the grep-only bucket."""
    monkeypatch.setattr(
        sanity, "_dekko_hits_callers", lambda *_a, **_kw: ([], [])
    )


def _grep_only_causes(capsys: pytest.CaptureFixture) -> dict[str, str]:
    doc = json.loads(capsys.readouterr().out)
    return {row["file"]: row["cause"] for row in doc["grep_only"]}


# --- dekko map ----------------------------------------------------------


def test_map_skips_both_with_a_reason_and_no_install_advice(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _write(tmp_path, REPO)
    assert cli.main(["map", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "mapped 2 files (java 2)" in out
    assert "build script (gradle) 1" in out
    assert "no parser (groovy) 1" in out
    # Neither file reaches the extractor, so a default install no
    # longer suggests the grammar pack for files it cannot map.
    assert "NOT parsed" not in out
    assert "dekko[all]" not in out


def test_build_script_calls_leave_no_rows_in_the_map(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(REPO)
    index = mapfile.load_map(root)
    assert index is not None
    assert set(index.languages_by_path) == {
        "src/main/java/a/Tool.java",
        "buildSrc/src/main/java/b/Logic.java",
    }
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    callers = {doc["ids"][row["caller"]] for row in doc["external"]}
    assert not any("build.gradle" in c or ".groovy" in c for c in callers)


def test_kotlin_build_script_is_still_indexed(
    make_mapped_repo: RepoFactory,
) -> None:
    script = "fun helper(x: Int): Int = x\nval y = helper(1)\n"
    root = make_mapped_repo({"build.gradle.kts": script})
    index = mapfile.load_map(root)
    assert index is not None
    assert index.languages_by_path == {"build.gradle.kts": "kotlin"}
    assert index.provenance["build_scripts"] is None


# --- provenance ---------------------------------------------------------


def test_provenance_counts_build_scripts_apart_from_unsupported(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(REPO)
    index = mapfile.load_map(root)
    assert index is not None
    prov = index.provenance
    assert prov["build_scripts"] == BUILD_SCRIPTS
    assert prov["unsupported"] == {"count": 1, "languages": {"groovy": 1}}
    assert mapfile.format_build_scripts(prov) == ("1 not indexed: gradle (1)")
    # The per-query coverage note names the unparsed language only.
    note = mapfile.format_unsupported(prov)
    assert note == "1 files unparsed — no parser for: groovy (1)"


def test_format_build_scripts_none_without_build_scripts(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo({"a.py": "def f():\n    return 1\n"})
    index = mapfile.load_map(root)
    assert index is not None
    assert index.provenance["build_scripts"] is None
    assert mapfile.format_build_scripts(index.provenance) is None
    # A map written before the bucket existed has no such key.
    assert mapfile.format_build_scripts({}) is None
    assert mapfile.format_build_scripts(None) is None


# --- whole-repo reports -------------------------------------------------


def test_stats_prints_build_scripts_on_their_own_line(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    assert cli.main(["stats", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "languages: java 2f/7s" in out
    assert "groovy 1f" not in out
    assert "no parser for: groovy (1)" in out
    assert "build scripts: 1 not indexed: gradle (1)" in out

    assert cli.main(["stats", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["build_scripts"] == BUILD_SCRIPTS
    assert "gradle" not in doc["coverage_warning"]


def test_status_reports_build_scripts(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    assert cli.main(["status", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "map fresh (2 files" in out
    assert "build scripts: 1 not indexed: gradle (1)" in out

    assert cli.main(["status", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["build_scripts"] == BUILD_SCRIPTS

    (root / "src/main/java/a/Tool.java").write_text("class Tool {}\n")
    assert cli.main(["status", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "stale" in out
    assert "build scripts: 1 not indexed: gradle (1)" in out


def test_editing_a_build_script_does_not_stale_the_map(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    (root / "build.gradle").write_text('plugins { id "application" }\n')
    assert cli.main(["status", "--root", str(root)]) == 0
    assert "map fresh" in capsys.readouterr().out


def test_summary_reports_build_scripts(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    assert cli.main(["summary", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "build scripts: 1 not indexed: gradle (1)" in out

    assert cli.main(["summary", "--root", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["build_scripts"] == (
        BUILD_SCRIPTS
    )


def test_map_status_tool_reports_build_scripts(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(REPO)
    ctx = server.Context(default_root=root, no_regen=False)
    text = server.tool_map_status(ctx, {})
    assert "fresh" in text
    assert "no parser for: groovy (1)" in text
    assert "build scripts: 1 not indexed: gradle (1)" in text


# --- query --------------------------------------------------------------


def test_empty_callers_of_product_code_has_no_build_script_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    assert cli.main(["query", "callers", "runIt", "--root", str(root)]) == 0
    err = capsys.readouterr().err
    assert "no parser for: groovy (1)" in err
    assert "build scripts" not in err

    args = ["query", "callers", "runIt", "--root", str(root), "--json"]
    assert cli.main(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "build_script_warning" not in doc
    assert "groovy" in doc["coverage_warning"]


def test_empty_callers_of_build_logic_says_build_scripts_are_unread(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    args = ["query", "callers", "normalizePort", "--root", str(root)]
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert f"(no callers of {LOGIC})" in captured.out
    assert "build scripts: 1 not indexed: gradle (1)" in captured.err
    assert "under buildSrc/" in captured.err
    assert f"dekko sanity {LOGIC}" in captured.err

    assert cli.main([*args, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "under buildSrc/" in doc["build_script_warning"]


def test_callees_of_build_logic_has_no_build_script_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    args = ["query", "callees", "normalizePort", "--root", str(root)]
    assert cli.main(args) == 0
    assert "build scripts" not in capsys.readouterr().err


def test_build_logic_without_build_scripts_has_no_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {k: v for k, v in REPO.items() if k.endswith(".java")}
    root = make_mapped_repo(files)
    args = ["query", "callers", "normalizePort", "--root", str(root)]
    assert cli.main(args) == 0
    assert "build scripts" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (
            "build.gradle",
            "a Gradle build script; dekko does not index build scripts",
        ),
        ("ToolSpec.groovy", "groovy; dekko has no parser for it"),
    ],
)
def test_file_lookups_say_why_a_path_is_not_mapped(
    make_mapped_repo: RepoFactory,
    capsys: pytest.CaptureFixture,
    target: str,
    reason: str,
) -> None:
    root = make_mapped_repo(REPO)
    assert cli.main(["query", "file", target, "--root", str(root)]) == 3
    err = capsys.readouterr().err
    assert f"no mapped file matches '{target}' ({reason})" in err

    assert cli.main(["outline", target, "--root", str(root)]) == 3
    err = capsys.readouterr().err
    assert f"no mapped file or directory '{target}' ({reason})" in err


def test_file_lookup_miss_on_an_ordinary_path_names_no_reason(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(REPO)
    assert cli.main(["query", "file", "Gone.java", "--root", str(root)]) == 3
    err = capsys.readouterr().err
    assert "no mapped file matches 'Gone.java'\n" in err


# --- sanity -------------------------------------------------------------


def test_sanity_names_a_build_script_hit_whatever_the_line_looks_like(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo(REPO)
    _no_dekko_hits(monkeypatch)
    # A bare call inside a closure: used to read "unexplained".
    args = ["sanity", "normalizePort", "--root", str(root), "--json"]
    assert cli.main(args) == 0
    assert _grep_only_causes(capsys) == {
        "build.gradle": sanity.CAUSE_BUILD_SCRIPT
    }
    # A receiver call: used to read "qualified call, resolver blind
    # spot", which is a claim about a resolver that never saw the file.
    args = ["sanity", "forDocs", "--root", str(root), "--json"]
    assert cli.main(args) == 0
    assert _grep_only_causes(capsys) == {
        "build.gradle": sanity.CAUSE_BUILD_SCRIPT
    }


def test_sanity_names_a_groovy_hit_as_an_unparsed_file(
    make_mapped_repo: RepoFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    root = make_mapped_repo(REPO)
    _no_dekko_hits(monkeypatch)
    args = ["sanity", "runIt", "--root", str(root), "--json"]
    assert cli.main(args) == 0
    assert _grep_only_causes(capsys) == {
        "ToolSpec.groovy": sanity.CAUSE_UNSUPPORTED_LANGUAGE,
        "build.gradle": sanity.CAUSE_BUILD_SCRIPT,
    }


def test_unindexed_cause_is_limited_to_recognized_files() -> None:
    assert sanity._unindexed_cause("build.gradle") == (
        sanity.CAUSE_BUILD_SCRIPT
    )
    assert sanity._unindexed_cause("x/Card.astro") == (
        sanity.CAUSE_UNSUPPORTED_LANGUAGE
    )
    # Everything else keeps the ladder it had: prose, data, and every
    # file dekko does parse.
    for path in ("README.md", "package.json", "a.py", "build.gradle.kts"):
        assert sanity._unindexed_cause(path) is None


def test_build_script_cause_is_not_re_decided_by_target_facts() -> None:
    assert sanity.CAUSE_BUILD_SCRIPT not in sanity._REMAINING_CAUSES


@pytest.mark.parametrize("path", ["build.gradle", "src/ToolSpec.groovy"])
def test_a_comment_in_an_unindexed_file_is_still_a_comment(
    path: str,
) -> None:
    # ``sanity --unused`` sorts grep hits into evidence and noise by
    # comment shape, and that must keep working for files with no
    # grammar behind them.
    assert sanity._looks_like_comment_line("// calls runIt later", path)
    assert not sanity._looks_like_comment_line("runIt()", path)


def _outline_err(
    root: Path, target: str, capsys: pytest.CaptureFixture
) -> str:
    assert cli.main(["outline", target, "--root", str(root)]) == 3
    return capsys.readouterr().err


def test_a_file_no_grammar_maps_says_so(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {"a.py": "def f():\n    pass\n", "README.md": "#\n"}
    )
    err = _outline_err(root, "README.md", capsys)
    assert "(exists, but no grammar maps .md files)" in err
    assert cli.main(["query", "file", "README.md", "--root", str(root)]) == 3
    assert "(exists, but no grammar maps .md files)" in capsys.readouterr().err


def test_a_directory_with_nothing_mapped_says_so(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {"a.py": "def f():\n    pass\n", "docs/guide.md": "#\n"}
    )
    assert "(exists, but holds no mapped files)" in _outline_err(
        root, "docs", capsys
    )


def test_a_file_over_the_size_cap_names_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / "small.py").write_text("def f():\n    pass\n")
    (tmp_path / "big.py").write_text("def g():\n    pass\n" * 200)
    assert (
        cli.main(["map", str(tmp_path), "--quiet", "--max-file-size", "100"])
        == 0
    )
    err = _outline_err(tmp_path, "big.py", capsys)
    assert "exceeded the size cap" in err
    assert "--max-file-size" in err


def test_a_symlink_names_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / "real.py").write_text("def f():\n    pass\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    err = _outline_err(tmp_path, "link.py", capsys)
    assert "(a symlink; pass --follow-symlinks to map it)" in err


def test_an_excluded_file_says_it_exists(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "a.py": "def f():\n    pass\n",
            "node_modules/pkg/index.js": "function g() {}\n",
        }
    )
    err = _outline_err(root, "node_modules/pkg/index.js", capsys)
    assert "(exists, but isn't mapped: ignored, vendored, or excluded)" in err


def test_a_missing_path_keeps_the_plain_message(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo({"a.py": "def f():\n    pass\n"})
    err = _outline_err(root, "nope.py", capsys)
    assert "no mapped file or directory 'nope.py'" in err
    assert "exists" not in err


def test_a_symlink_to_a_file_no_grammar_maps_says_no_grammar(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    (tmp_path / "notes.md").write_text("#\n")
    (tmp_path / "rules.md").symlink_to(tmp_path / "notes.md")
    assert cli.main(["map", str(tmp_path), "--quiet"]) == 0
    err = _outline_err(tmp_path, "rules.md", capsys)
    assert "(exists, but no grammar maps .md files)" in err
