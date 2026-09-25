"""``unused``'s property-read dispatch evidence and external-consumer root."""

import json

import pytest

from dekko.analysis import unused
from dekko.core.model import Import, ReadSite, Symbol
from dekko.integrations import cli
from dekko.render.mapfile import MapIndex

from conftest import RepoFactory


def _sym(name: str, path: str, **kw: object) -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind=str(kw.get("kind", "function")),
        path=path,
        language="typescript",
        in_literal=bool(kw.get("in_literal", False)),
        literal_consumer=kw.get("literal_consumer"),  # type: ignore[arg-type]
    )


def _index(symbols: list[Symbol], **kw: object) -> MapIndex:
    idx = MapIndex(root_label="t")
    for sym in symbols:
        idx.symbols_by_id[sym.id] = sym
        idx.symbols_by_path.setdefault(sym.path, []).append(sym)
        idx.languages_by_path[sym.path] = sym.language
        idx.symbols_by_name.setdefault(sym.name, []).append(sym)
    idx.calls_in = dict(kw.get("calls_in", {}))  # type: ignore[arg-type]
    idx.ambiguous_in = dict(kw.get("ambiguous_in", {}))  # type: ignore
    idx.reads_by_name = dict(kw.get("reads_by_name", {}))  # type: ignore
    idx.imports_by_path = dict(kw.get("imports", {}))  # type: ignore
    idx.module_external = dict(kw.get("module_external", {}))  # type: ignore
    return idx


def _reads(name: str) -> dict[str, list[ReadSite]]:
    return {name: [ReadSite(reader="use.ts::pick", name=name, lines=[4])]}


# --- property-read evidence ---------------------------------------------


def test_shared_name_read_as_property_is_dispatch_evidence() -> None:
    a = _sym("isHidden", "a.ts")
    b = _sym("isHidden", "b.ts")
    idx = _index([a, b], reads_by_name=_reads("isHidden"))
    assert unused._dispatch_evidence(a, idx) == unused.EVIDENCE_PROPERTY_READ
    assert unused._dispatch_evidence(b, idx) == unused.EVIDENCE_PROPERTY_READ
    found = unused.find_dispatch_candidates(idx, ())
    assert {s.id for s in found} == {a.id, b.id}


def test_lone_literal_member_read_as_property_is_dispatch_evidence() -> None:
    getter = _sym("environmentId", "a.ts", in_literal=True)
    idx = _index([getter], reads_by_name=_reads("environmentId"))
    assert (
        unused._dispatch_evidence(getter, idx) == unused.EVIDENCE_PROPERTY_READ
    )


def test_lone_free_function_read_as_property_is_not_evidence() -> None:
    # ``msg.type`` reads say nothing about a free function named
    # ``type``: a read never resolves, so only a shared name or a
    # literal member earns the mark.
    fn = _sym("type", "a.ts")
    idx = _index([fn], reads_by_name=_reads("type"))
    assert unused._dispatch_evidence(fn, idx) is None
    assert unused.find_dispatch_candidates(idx, ()) == []


def test_a_type_sharing_the_name_does_not_make_it_shared() -> None:
    fn = _sym("Config", "a.ts")
    typ = _sym("Config", "b.ts", kind="interface")
    idx = _index([fn, typ], reads_by_name=_reads("Config"))
    assert unused._dispatch_evidence(fn, idx) is None


def test_ambiguous_evidence_outranks_property_read() -> None:
    a = _sym("isHidden", "a.ts")
    b = _sym("isHidden", "b.ts")
    idx = _index(
        [a, b],
        ambiguous_in={a.id: [("use.ts::pick", "isHidden")]},
        reads_by_name=_reads("isHidden"),
    )
    assert unused._dispatch_evidence(a, idx) == unused.EVIDENCE_AMBIGUOUS
    assert unused._dispatch_evidence(b, idx) == unused.EVIDENCE_PROPERTY_READ


def test_a_read_never_counts_as_used() -> None:
    getter = _sym("isHidden", "a.ts", in_literal=True)
    idx = _index([getter], reads_by_name=_reads("isHidden"))
    assert unused.unused_status(idx, getter, ()).flagged is True


# --- external-consumer root ---------------------------------------------


def _imports(path: str, name: str, source: str) -> dict[str, list[Import]]:
    return {path: [Import(path=path, name=name, source=source)]}


def test_literal_member_handed_to_an_external_import_is_a_root() -> None:
    member = _sym(
        "hideInstance",
        "ink/reconciler.ts",
        in_literal=True,
        literal_consumer="createReconciler",
    )
    idx = _index(
        [member],
        imports=_imports(
            "ink/reconciler.ts", "createReconciler", "react-reconciler"
        ),
        module_external={"ink/reconciler.ts": ["react-reconciler"]},
    )
    status = unused.unused_status(idx, member, ())
    assert (status.flagged, status.reason) == (False, unused.STATUS_ROOT)
    assert unused.find_unused(idx, ()) == []


def test_dotted_consumer_is_judged_by_its_head_binding() -> None:
    member = _sym(
        "del", "markdown.ts", in_literal=True, literal_consumer="marked.use"
    )
    idx = _index(
        [member],
        imports=_imports("markdown.ts", "marked", "marked"),
        module_external={"markdown.ts": ["marked"]},
    )
    assert unused.unused_status(idx, member, ()).flagged is False


@pytest.mark.parametrize(
    ("consumer", "imports", "module_external"),
    [
        # A global, never imported: ``Bun.listen({...})``.
        ("Bun.listen", {}, {}),
        # An injected in-repo dependency the resolver can't follow.
        ("deps.callModel", {}, {}),
        # A built-in that hands the literal straight back.
        ("Promise.resolve", {}, {}),
        # Imported, but from a repo file the module graph resolved.
        (
            "createReconciler",
            _imports("a.ts", "createReconciler", "./reconciler.js"),
            {},
        ),
        # An external import in another file only.
        (
            "createReconciler",
            _imports("b.ts", "createReconciler", "react-reconciler"),
            {"b.ts": ["react-reconciler"]},
        ),
    ],
)
def test_consumer_not_imported_from_outside_the_repo_is_not_a_root(
    consumer: str,
    imports: dict[str, list[Import]],
    module_external: dict[str, list[str]],
) -> None:
    # The literal may have gone to in-repo code the map couldn't
    # follow, so the ordinary evidence rules judge the member.
    member = _sym("member", "a.ts", in_literal=True, literal_consumer=consumer)
    idx = _index([member], imports=imports, module_external=module_external)
    assert unused.unused_status(idx, member, ()).flagged is True


# --- end to end -----------------------------------------------------------

COMMANDS = {
    "commands/fast.ts": (
        "import type { Command } from '../types.js'\n"
        "const fast = {\n"
        "  name: 'fast',\n"
        "  get isHidden() { return false },\n"
        "  get unread() { return 1 },\n"
        "} satisfies Command\n"
        "export default fast\n"
    ),
    "commands/voice.ts": (
        "import type { Command } from '../types.js'\n"
        "const voice = {\n"
        "  name: 'voice',\n"
        "  get isHidden() { return true },\n"
        "} satisfies Command\n"
        "export default voice\n"
    ),
    "types.ts": "export type Command = { name: string; isHidden: boolean }\n",
    "help.ts": (
        "import type { Command } from './types.js'\n"
        "export function visible(cmds: Command[]): Command[] {\n"
        "  return cmds.filter(cmd => !cmd.isHidden)\n"
        "}\n"
    ),
    "reconciler.ts": (
        "import createReconciler from 'react-reconciler'\n"
        "export const reconciler = createReconciler({\n"
        "  hideInstance(node: any) { node.hidden = true },\n"
        "  resetTextContent() {},\n"
        "})\n"
    ),
}


def test_unused_marks_read_getters_and_spares_reconciler_members(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(COMMANDS)
    code = cli.main(
        [
            "unused",
            "--root",
            str(root),
            "--dispatch",
            "--json",
            "--limit",
            "50",
        ]
    )
    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    rows = {r["id"]: r for r in doc["results"]}
    assert rows["commands/fast.ts::isHidden"]["dispatch_candidate"] is True
    assert rows["commands/voice.ts::isHidden"]["dispatch_candidate"] is True
    assert "dispatch_candidate" not in rows["commands/fast.ts::unread"]
    assert "reconciler.ts::hideInstance" not in rows
    assert "reconciler.ts::resetTextContent" not in rows
    evidence = {c["id"]: c["evidence"] for c in doc["dispatch_candidates"]}
    assert evidence["commands/fast.ts::isHidden"] == "property-read"
    assert "property-read candidates" in doc["dispatch_caveat"]


def test_unused_text_says_property_read_on_the_dispatch_row(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(COMMANDS)
    cli.main(["unused", "--root", str(root), "--dispatch"])
    out = capsys.readouterr().out
    assert "commands/fast.ts:4  isHidden()  [function]  [dispatch?]" in out
    assert (
        "possible property-read target (getter/handler read, not called)"
        in out
    )


def test_sanity_unused_discloses_property_reads(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(COMMANDS)
    code = cli.main(
        [
            "sanity",
            "--unused",
            "commands/fast.ts:isHidden:4",
            "--root",
            str(root),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "dekko evidence (calls_in/referenced_in): none" in out
    assert (
        "dekko evidence (property reads): 1 site(s) read this name as a "
        "property, e.g. help.ts:3" in out
    )
    cli.main(
        [
            "sanity",
            "--unused",
            "commands/fast.ts:isHidden:4",
            "--root",
            str(root),
            "--json",
        ]
    )
    doc = json.loads(capsys.readouterr().out)
    assert doc["property_reads"]["count"] == 1
    assert doc["property_reads"]["sites"] == ["help.ts:3"]
