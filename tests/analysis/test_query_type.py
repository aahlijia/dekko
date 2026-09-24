"""``dekko query type``: type-usage search over params/returns."""

import json

import pytest

from dekko.integrations import cli
from dekko.analysis import query
from dekko.render.mapfile import MapIndex
from dekko.core.model import Param, Symbol

from conftest import RepoFactory

PY_TYPE_USAGE = {
    "app.py": (
        "from typing import Optional\n"
        "\n"
        "\n"
        "class Config:\n"
        "    pass\n"
        "\n"
        "\n"
        "class ConfigManager:\n"
        "    pass\n"
        "\n"
        "\n"
        "class AppConfig:\n"
        "    pass\n"
        "\n"
        "\n"
        "def start(cfg: Config) -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "def load_config() -> Config:\n"
        "    return Config()\n"
        "\n"
        "\n"
        "def run(cfg: Optional[Config] = None) -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "def run_union(cfg: Config | None = None) -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "def manage(mgr: ConfigManager) -> None:\n"
        "    pass\n"
        "\n"
        "\n"
        "def configure(app: AppConfig) -> None:\n"
        "    pass\n"
    ),
}


def test_type_default_matches_param_and_return(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "start(cfg: Config) -> None" in out
    assert "[param: cfg]" in out
    assert "load_config() -> Config" in out
    assert "[return]" in out


def test_type_default_matches_wrapper_syntax(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Optional[Config] and Config | None both tokenize to a bare
    # `Config` identifier, so the default (non-exact) match must find
    # both wrapper forms.
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "run(cfg: Optional[Config]" in out
    assert "run_union(cfg: Config | None" in out


def test_type_default_rejects_similarly_named_types(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # ConfigManager/AppConfig must not match a bare "Config" query —
    # this is the false-positive guard the identifier-token match
    # exists for.
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "manage(mgr: ConfigManager)" not in out
    assert "configure(app: AppConfig)" not in out


def test_type_exact_rejects_wrapper_syntax(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(
        ["query", "type", "Config", "--exact", "--root", str(root)]
    )
    assert code == 0
    out = capsys.readouterr().out
    # Only the bare `Config` param/return survive --exact; the wrapped
    # forms (Optional[Config], Config | None) are deliberately dropped.
    assert "start(cfg: Config) -> None" in out
    assert "load_config() -> Config" in out
    assert "run(cfg: Optional[Config]" not in out
    assert "run_union(cfg: Config | None" not in out


def test_type_exact_returns_fewer_or_equal_results(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_TYPE_USAGE)
    assert (
        cli.main(["query", "type", "Config", "--json", "--root", str(root)])
        == 0
    )
    default_doc = json.loads(capsys.readouterr().out)
    assert (
        cli.main(
            [
                "query",
                "type",
                "Config",
                "--exact",
                "--json",
                "--root",
                str(root),
            ]
        )
        == 0
    )
    exact_doc = json.loads(capsys.readouterr().out)
    assert len(exact_doc["results"]) <= len(default_doc["results"])
    assert len(exact_doc["results"]) < len(default_doc["results"])


def test_type_json_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(["query", "type", "Config", "--json", "--root", str(root)])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["action"] == "type"
    assert doc["name"] == "Config"
    assert doc["exact"] is False
    param_entries = {
        e["raw_type"]: e for e in doc["results"] if e["usage"] == "param"
    }
    bare_param = param_entries["Config"]
    assert bare_param["param_name"] == "cfg"
    assert "Optional[Config]" in param_entries
    assert "Config | None" in param_entries
    return_entries = [e for e in doc["results"] if e["usage"] == "return"]
    assert return_entries
    assert "param_name" not in return_entries[0]
    assert return_entries[0]["raw_type"] == "Config"


def test_type_no_matches_reports_closest_type_names(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(["query", "type", "Confg", "--root", str(root)])
    assert code == 3
    err = capsys.readouterr().err
    assert "no results for type 'Confg'" in err
    assert "Config" in err


def test_type_exact_no_matches_does_not_self_echo_suggestion(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # `Project` is a real declared type, but every usage
    # in this fixture is wrapped (`Optional[Project]`), so --exact's
    # literal-text match rejects all of them and the not-found path is
    # reached. The "closest type names" suggester must not echo
    # `Project` back as its own closest match -- that's the exact
    # string that just failed, offering nothing new.
    files = {
        "app.py": (
            "from typing import Optional\n"
            "\n"
            "\n"
            "class Project:\n"
            "    pass\n"
            "\n"
            "\n"
            "def start(p: Optional[Project] = None) -> None:\n"
            "    pass\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(
        ["query", "type", "Project", "--exact", "--root", str(root)]
    )
    assert code == 3
    err = capsys.readouterr().err
    assert "no results for type 'Project'" in err
    assert "closest type names: Project\n" not in err
    assert "closest type names: Project," not in err


def test_type_no_matches_json_success_path_unaffected(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # A not-found type target is not a crash in --json mode either;
    # it's the same not-found exit code and stderr report as text mode
    # (no JSON is printed to stdout on a not-found path, matching the
    # 'uses' action's contract).
    root = make_mapped_repo(PY_TYPE_USAGE)
    code = cli.main(
        ["query", "type", "TotallyUnknownType", "--json", "--root", str(root)]
    )
    assert code == 3
    assert capsys.readouterr().out == ""


def test_type_budget_caps_many_matches(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    pad = "z" * 60
    files = {"types.py": "class Shared:\n    pass\n"}
    for i in range(30):
        files[f"user_{i}.py"] = (
            "from types import Shared\n\n\n"
            f"def use_with_a_long_padded_name_{pad}_{i}"
            "(x: Shared) -> None:\n"
            "    pass\n"
        )
    root = make_mapped_repo(files)
    code = cli.main(["query", "type", "Shared", "--root", str(root)])
    assert code == 0
    assert "omitted" in capsys.readouterr().out


def test_type_json_budget_reports_truncated_by_budget(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    pad = "z" * 60
    files = {"types.py": "class Shared:\n    pass\n"}
    for i in range(30):
        files[f"user_{i}.py"] = (
            "from types import Shared\n\n\n"
            f"def use_with_a_long_padded_name_{pad}_{i}"
            "(x: Shared) -> None:\n"
            "    pass\n"
        )
    root = make_mapped_repo(files)
    code = cli.main(
        [
            "query",
            "type",
            "Shared",
            "--json",
            "--budget",
            "50",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["meta"]["truncated_by"] == "budget"


def test_type_no_tests_excludes_test_files(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "app.py": "class Config:\n    pass\n",
        "tests/test_app.py": (
            "from app import Config\n\n\n"
            "def make_config(cfg: Config) -> None:\n"
            "    pass\n"
        ),
    }
    root = make_mapped_repo(files)

    # Default: test-file usages are included.
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    assert "make_config" in capsys.readouterr().out

    # --no-tests: test-file usages are excluded.
    code = cli.main(
        ["query", "type", "Config", "--no-tests", "--root", str(root)]
    )
    assert code == 3
    assert "make_config" not in capsys.readouterr().out


def test_type_rust_pointer_and_option_wrappers(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "lib.rs": (
            "struct Config;\n"
            "\n"
            "fn start(cfg: &Config) {\n"
            "}\n"
            "\n"
            "fn maybe(cfg: Option<Config>) {\n"
            "}\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "start" in out
    assert "maybe" in out


def test_type_go_pointer_wrapper(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "main.go": (
            "package main\n"
            "\n"
            "type Config struct{}\n"
            "\n"
            "func start(cfg *Config) {\n"
            "}\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    assert "start" in capsys.readouterr().out


def test_type_typescript_union_wrapper(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    files = {
        "app.ts": (
            "interface Config {}\n"
            "\n"
            "function start(cfg: Config | undefined): void {\n"
            "}\n"
        ),
    }
    root = make_mapped_repo(files)
    code = cli.main(["query", "type", "Config", "--root", str(root)])
    assert code == 0
    assert "start" in capsys.readouterr().out


def _fn(name: str, **kw: object) -> Symbol:
    return Symbol(
        id=f"a.py::{name}",
        name=name,
        qualname=name,
        kind="function",
        path="a.py",
        language="python",
        params=list(kw.get("params", [])),  # type: ignore[arg-type]
        returns=kw.get("returns"),  # type: ignore[arg-type]
    )


def test_type_usage_name_index_matches_wrapper_syntax() -> None:
    # unused.py's --kinds types relies on this being an O(symbols)
    # equivalent of calling type_usage_rows(index, name, exact=False)
    # once per type name — same identifier-token matching, just
    # inverted into a single pass. Cover the same wrapper shapes
    # type_usage_rows' own tests above already exercise via the CLI.
    idx = MapIndex(root_label="t")
    fns = [
        _fn("start", params=[Param(name="cfg", type="Config")]),
        _fn(
            "maybe",
            params=[Param(name="cfg", type="Optional[Config]")],
        ),
        _fn("load", returns="Config | None"),
        _fn("ptr", params=[Param(name="cfg", type="*Config")]),
        _fn("unrelated", params=[Param(name="x", type="int")]),
    ]
    for fn in fns:
        idx.symbols_by_id[fn.id] = fn
    names = query.type_usage_name_index(idx)
    assert "Config" in names
    assert "int" in names
    assert "ConfigManager" not in names  # not a whole-token match

    # Parity check: every function this reports as a Config hit via
    # the inverted index is also found by the naive per-needle
    # matcher, and vice versa — confirms the inversion didn't change
    # matching semantics, only its cost shape.
    rows = query.type_usage_rows(idx, "Config", exact=False)
    row_names = {r.symbol.name for r in rows if r.symbol is not None}
    assert row_names == {"start", "maybe", "load", "ptr"}


# --- Sites the map never named: nested/anonymous function shapes -----

# ``Ctx`` is used only where no symbol carries params: a returned
# arrow inside ``make``, a function-typed member of ``Opts``, and a
# module-level callback in a test file. ``start`` uses ``Other``
# through its own signature (the pre-existing row shape).
TS_NESTED = {
    "app.ts": (
        "export interface Ctx { id: string }\n"
        "export interface Other { n: number }\n"
        "\n"
        "export function make() {\n"
        "  return (ctx: Ctx) => ctx.id;\n"
        "}\n"
        "\n"
        "export interface Opts {\n"
        "  build: (config: Ctx) => string\n"
        "}\n"
        "\n"
        "export function start(o: Other): void {}\n"
    ),
    "app.test.ts": (
        "import { Ctx } from './app';\n"
        "describe('x', () => { items.map((c: Ctx) => c) });\n"
    ),
}


def test_type_finds_sites_on_unnamed_function_shapes(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_NESTED)
    code = cli.main(["query", "type", "Ctx", "--root", str(root)])
    assert code == 0
    out = capsys.readouterr().out
    assert "app.ts:5  arrow function in make  [param: ctx]" in out
    assert "app.ts:9  function type in Opts  [param: config]" in out
    assert "app.test.ts:2  arrow function (module level)  [param: c]" in out
    # Owned rows sort by relevance first; the module-level row last.
    assert out.index("in make") < out.index("(module level)")


def test_type_symbol_signature_rows_keep_their_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_NESTED)
    assert cli.main(["query", "type", "Other", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "start(o: Other) -> void  [param: o]" in out
    assert "function type" not in out
    capsys.readouterr()
    code = cli.main(["query", "type", "Other", "--root", str(root), "--json"])
    assert code == 0
    entry = json.loads(capsys.readouterr().out)["results"][0]
    assert entry["id"] == "app.ts::start"
    assert "site" not in entry
    assert "owner" not in entry


def test_type_json_site_entry_shape(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_NESTED)
    code = cli.main(["query", "type", "Ctx", "--root", str(root), "--json"])
    assert code == 0
    results = json.loads(capsys.readouterr().out)["results"]
    by_line = {(r["path"], r["line"]): r for r in results}
    member = by_line[("app.ts", 9)]
    assert member["site"] == "function_type"
    assert member["owner"]["id"] == "app.ts::Opts"
    assert member["usage"] == "param"
    assert member["param_name"] == "config"
    assert member["raw_type"] == "Ctx"
    top = by_line[("app.test.ts", 2)]
    assert top["site"] == "arrow_function"
    assert top["owner"] is None


def test_type_exact_matches_site_rows(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_NESTED)
    code = cli.main(["query", "type", "Ctx", "--root", str(root), "--exact"])
    assert code == 0
    assert "in Opts" in capsys.readouterr().out


def test_type_no_tests_drops_test_file_sites(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_NESTED)
    code = cli.main(
        ["query", "type", "Ctx", "--root", str(root), "--no-tests"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "in make" in out
    assert "app.test.ts" not in out
    assert "(module level)" not in out


def test_unused_types_credits_sites_as_evidence(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # Before sites were recorded ``Ctx`` had no type-usage evidence
    # at all and ``--kinds types`` reported it dead.
    root = make_mapped_repo(TS_NESTED)
    cli.main(["unused", "--kinds", "types", "--root", str(root), "--json"])
    doc = json.loads(capsys.readouterr().out)
    flagged = {r["id"] for r in doc["results"]}
    assert "app.ts::Ctx" not in flagged
    assert "app.ts::Other" not in flagged


def test_workset_type_impact_counts_sites_and_bundles_owners(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(TS_NESTED)
    code = cli.main(
        [
            "workset",
            "--symbol",
            "Ctx",
            "--type-impact",
            "--root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    # Two owned sites (make, Opts) + one module-level site, all
    # counted; only the two owners can be bundled with the target.
    assert doc["seed"]["blast_radius"]["type_usage"] == 3
    assert doc["seed"]["touched_symbols"] == 3
    assert "app.ts" in doc["seed"]["touched_files"]
