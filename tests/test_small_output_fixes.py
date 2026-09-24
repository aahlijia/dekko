"""Small output fixes across several commands.

The ``deps --file`` scope note, daemon status booleans, ``unused``
--dispatch/--suspect section cap disclosure, external heritage rows
that keep their relation, and the json-backend doctor row plus the
[all] extra.
"""

import json
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import pytest

from dekko.analysis import unused
from dekko.core.model import ExternalCall, Param, Symbol
from dekko.daemon import daemon
from dekko.integrations import cli, doctor
from dekko.render import mapfile

from conftest import RepoFactory

GO_REPO = {
    "main.go": (
        'package main\n\nimport (\n\t"fmt"\n\t"example.com/x/pkg/slug"\n)\n\n'
        'func main() {\n\tfmt.Println(slug.Make("hi"))\n}\n'
    ),
    "pkg/slug/slug.go": (
        "package slug\n\nfunc Make(s string) string {\n\treturn s\n}\n"
    ),
}

PY_REPO = {
    "a.py": "from b import helper\n\ndef top():\n    return helper()\n",
    "b.py": "def helper():\n    return 1\n",
}


# --- deps --file scope note ----------------------------------------------


def test_deps_file_on_go_carries_the_scope_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(GO_REPO)
    assert cli.main(["deps", "--file", "main.go", "--root", str(root)]) == 0
    captured = capsys.readouterr()
    assert "imports (0):" in captured.out
    assert "main.go is go: dekko does not resolve go imports" in captured.err
    assert "imported-by is always empty" in captured.err

    assert (
        cli.main(["deps", "--file", "main.go", "--root", str(root), "--json"])
        == 0
    )
    doc = json.loads(capsys.readouterr().out)
    assert "does not resolve go imports" in doc["import_scope_note"]


def test_deps_file_on_a_resolved_language_has_no_scope_note(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(PY_REPO)
    assert cli.main(["deps", "--file", "a.py", "--root", str(root)]) == 0
    captured = capsys.readouterr()
    assert "does not resolve" not in captured.err
    assert (
        cli.main(["deps", "--file", "a.py", "--root", str(root), "--json"])
        == 0
    )
    assert "import_scope_note" not in json.loads(capsys.readouterr().out)


# --- daemon status -------------------------------------------------------


def test_daemon_status_text_prints_busy_as_a_word(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    data = {
        "running": True,
        "pid": 1,
        "uptime_seconds": 1.0,
        "busy": False,
        "transport": "unix",
        "cache": None,
    }
    monkeypatch.setattr(daemon, "_probe_status", lambda _t: (data, False))
    assert daemon.status(tmp_path, as_json=False) == 0
    out = capsys.readouterr().out
    assert "  busy: no\n" in out
    assert "False" not in out


# --- unused section caps -------------------------------------------------


def _sym(i: int) -> Symbol:
    return Symbol(
        id=f"m{i}.ts::M{i}.run",
        name="run",
        qualname=f"M{i}.run",
        kind="method",
        path=f"m{i}.ts",
        language="typescript",
        params=[Param(name="x")],
        start_line=i,
        end_line=i + 2,
    )


def test_dispatch_section_discloses_its_own_cap(
    capsys: pytest.CaptureFixture,
) -> None:
    candidates = [_sym(i) for i in range(30)]
    unused._print_dispatch_text(candidates)
    out = capsys.readouterr().out
    assert "dispatch candidates: 30 of these" in out
    assert out.count("possible polymorphic-dispatch target") == 20
    assert "10 of 30 omitted" in out


def test_dispatch_section_honors_an_explicit_lower_limit(
    capsys: pytest.CaptureFixture,
) -> None:
    unused._print_dispatch_text([_sym(i) for i in range(30)], limit=3)
    out = capsys.readouterr().out
    assert out.count("possible polymorphic-dispatch target") == 3
    assert "27 of 30 omitted" in out


def test_dispatch_section_honors_budget_independently(
    capsys: pytest.CaptureFixture,
) -> None:
    unused._print_dispatch_text([_sym(i) for i in range(30)], budget=120)
    out = capsys.readouterr().out
    assert out.count("possible polymorphic-dispatch target") < 20
    assert "omitted · raise --budget" in out


def test_suspects_section_discloses_its_own_cap(
    capsys: pytest.CaptureFixture,
) -> None:
    unused._print_suspects_text([_sym(i) for i in range(25)], limit=2)
    out = capsys.readouterr().out
    assert "suspects: 25 excluded symbols" in out
    assert out.count("also collides ambiguously") == 2
    assert "23 of 25 omitted" in out


def test_section_json_carries_totals() -> None:
    doc = unused._build_json_doc(
        found=[],
        suspects=[_sym(i) for i in range(25)],
        dispatch_candidates=[_sym(i) for i in range(30)],
        c_abi_caveat=None,
        dispatch_caveat=None,
        suspect=True,
        dispatch=True,
        budget=None,
        limit=3,
    )
    assert doc["suspects_meta"] == {"returned": 3, "total": 25}
    assert doc["dispatch_meta"] == {"returned": 3, "total": 30}
    assert len(doc["dispatch_candidates"]) == 3


# --- external heritage relation ------------------------------------------

JAVA_REPO = {
    "Base.java": "public class Base {}\n",
    "Impl.java": (
        "public class Impl extends Base implements Runnable, Comparable {}\n"
    ),
}


def test_external_heritage_rows_keep_their_relation(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(JAVA_REPO)
    assert cli.main(["query", "supertypes", "Impl", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "class Base  [extends]" in out
    assert "(external) Runnable  [implements]" in out
    assert "(external) Comparable  [implements]" in out

    assert (
        cli.main(
            ["query", "supertypes", "Impl", "--root", str(root), "--json"]
        )
        == 0
    )
    doc = json.loads(capsys.readouterr().out)
    ext = {e["text"]: e for e in doc["results"] if e.get("external")}
    assert ext["Runnable"]["relation"] == "implements"

    raw = json.loads((root / ".dekko" / "map.json").read_text())
    assert all("relation" in d for d in raw["heritage_external"])
    # the shared ExternalCall type must not leak the key elsewhere
    assert all("relation" not in d for d in raw["external"])


def test_pre_fix_map_without_relation_renders_as_before(
    capsys: pytest.CaptureFixture,
) -> None:
    sym = Symbol(
        id="a.py::Foo",
        name="Foo",
        qualname="Foo",
        kind="class",
        path="a.py",
        language="python",
        start_line=1,
        end_line=3,
    )
    index = mapfile.MapIndex(root_label="r")
    index.symbols_by_id[sym.id] = sym
    index.symbols_by_name["Foo"] = [sym]
    index.symbols_by_qualname["Foo"] = [sym]
    index.symbols_by_path["a.py"] = [sym]
    index.languages_by_path["a.py"] = "python"
    index.heritage_external_out[sym.id] = [
        ExternalCall(caller=sym.id, callee="pydantic.BaseModel", lines=[1])
    ]
    from dekko.analysis import query

    assert query.run(index, "supertypes", "Foo", as_json=False, limit=50) == 0
    line = next(
        ln for ln in capsys.readouterr().out.splitlines() if "(external)" in ln
    )
    assert line.rstrip().endswith("pydantic.BaseModel")


# --- json backend --------------------------------------------------------


def test_all_extra_includes_fast_json() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    extras = pyproject["project"]["optional-dependencies"]
    assert any(d.startswith("orjson") for d in extras["all"])
    assert not any("tiktoken" in d or "numpy" in d for d in extras["all"])


def test_doctor_reports_the_json_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.mapfile, "orjson", object())
    assert doctor._check_json_backend().status == "ok"
    monkeypatch.setattr(doctor.mapfile, "orjson", None)
    finding = doctor._check_json_backend()
    assert finding.status == "advisory"
    assert "fastjson" in (finding.fix or "")
