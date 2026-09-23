"""The .dekko per-file call-resolution cache.

The load-bearing property is parity: an incremental map that reuses
cached resolution must produce the same map.json as a full rebuild of the
same tree. A parity assertion alone is not enough, though — it passes
trivially if the reuse gate never fires — so these tests also assert
*when* the gate fires and when it correctly refuses to.
"""

import gzip
import json
from pathlib import Path

import pytest

from dekko.core import resolver as resolver_mod
from dekko.integrations import cli
from dekko.storage import cache as cache_mod
from dekko.storage import resolvecache

from conftest import RepoFactory

# Two files where b.py calls into a.py, so there is a real cross-file
# edge whose reuse actually means something.
SRC = {
    "a.py": (
        "def helper() -> int:\n"
        "    return 1\n"
        "\n"
        "def other() -> int:\n"
        "    return 2\n"
    ),
    "b.py": (
        "from a import helper\n\ndef caller() -> int:\n    return helper()\n"
    ),
}


def _map(root: Path, *extra: str) -> None:
    assert cli.main(["map", str(root), "--quiet", *extra]) == 0


def _graph_json(root: Path) -> dict:
    """map.json's resolution sections, minus anything run-dependent."""
    doc = json.loads((root / ".dekko" / "map.json").read_text())
    return {
        "edges": doc.get("edges"),
        "ambiguous": doc.get("ambiguous"),
        "external": doc.get("external"),
        "referenced": doc.get("referenced"),
        "heritage": doc.get("heritage"),
        "throws": doc.get("throws"),
        "catches": doc.get("catches"),
        "module_graph": doc.get("module_graph"),
        "symbols": doc.get("symbols"),
        "ids": doc.get("ids"),
    }


def _cache_path(root: Path) -> Path:
    return root / ".dekko" / resolvecache.RESOLVE_CACHE_FILE


def _fired(root: Path) -> bool:
    """Whether a reuse plan would be built for the current tree state."""
    return resolvecache.load(root) is not None


# --- the cache is written, small, and self-describing -----------------


def test_map_writes_a_resolve_cache(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(SRC)
    assert _cache_path(root).is_file()
    doc = json.loads(gzip.decompress(_cache_path(root).read_bytes()))
    assert doc["version"] == resolvecache.RESOLVE_CACHE_VERSION
    assert doc["resolve_hash"] == resolver_mod.resolve_fingerprint()
    # Every mapped file gets an entry, including ones that resolved to
    # nothing -- otherwise they look "never resolved" forever.
    assert set(doc["files"]) == {"a.py", "b.py"}


def test_ids_are_interned_not_repeated(
    make_mapped_repo: RepoFactory,
) -> None:
    """Storing ids verbatim made the artifact 10x larger and cost more to
    serialize than the resolution it saved; the stored form must be
    integer references into an id table."""
    root = make_mapped_repo(SRC)
    doc = json.loads(gzip.decompress(_cache_path(root).read_bytes()))
    assert isinstance(doc["ids"], list)
    assert any("::" in text for text in doc["ids"])
    for entry in doc["files"].values():
        for caller, callee, _lines in entry["e"]:
            assert isinstance(caller, int)
            assert isinstance(callee, int)


# --- parity across edit shapes ----------------------------------------


@pytest.mark.parametrize(
    ("label", "target", "edit"),
    [
        # Edit the *callee* side. b.py is then untouched, so its edge into
        # a.py can only come from the cache -- which is what makes these
        # cases actually exercise reuse. A mutation probe confirmed that
        # editing b.py instead (the edge's owning file) leaves nothing to
        # reuse, so those variants would pass even with reuse broken.
        ("callee_comment_only", "a.py", "# trailing comment\n"),
        ("callee_lines_shift", "a.py", "\n\n\n# lines below shift\n"),
        ("callee_body_rewrite", "a.py", "\ndef _unused_local() -> None:\n"),
        # A callee-side symbol addition: under v2 this narrows (a.py
        # dirty, b.py reused from cache -- see
        # test_gate_narrows_when_an_unrelated_symbol_is_added below) but
        # parity must hold either way, so this stays in the matrix.
        (
            "callee_new_symbol",
            "a.py",
            "def brand_new() -> int:\n    return 3\n",
        ),
        # Caller-side edits, for completeness.
        ("caller_comment_only", "b.py", "# trailing comment\n"),
        (
            "caller_added_call",
            "b.py",
            "def extra() -> int:\n    return helper()\n",
        ),
    ],
)
def test_incremental_matches_full_rebuild(
    make_mapped_repo: RepoFactory, label: str, target: str, edit: str
) -> None:
    """The whole point: reuse must never change the answer."""
    root = make_mapped_repo(SRC)
    (root / target).write_text(SRC[target] + edit)

    _map(root)
    incremental = _graph_json(root)

    _map(root, "--full")
    full = _graph_json(root)

    assert incremental == full, label


# Five files: a.py/b.py exercise a plain function-name edge, widget.py/
# user.py exercise a class-constructor edge, and bystander.py references
# neither -- it must never end up in `dirty` for any of the five shapes
# below, proving the gate narrows rather than just not-fully-refusing.
FIVE_SHAPE_SRC = {
    "a.py": (
        "def helper() -> int:\n    return 1\n\n"
        "def other() -> int:\n    return 2\n"
    ),
    "b.py": (
        "from a import helper\n\ndef caller() -> int:\n    return helper()\n"
    ),
    "widget.py": "class Widget:\n    pass\n",
    "user.py": (
        "from widget import Widget\n\n"
        "def make() -> Widget:\n    return Widget()\n"
    ),
    "bystander.py": "def unrelated() -> int:\n    return 42\n",
}


@pytest.mark.parametrize(
    ("label", "after", "expect_full_fallback", "expect_dirty"),
    [
        (
            "add_a_function",
            {
                "a.py": FIVE_SHAPE_SRC["a.py"]
                + "def newly_added() -> int:\n    return 9\n"
            },
            False,
            {"a.py"},
        ),
        (
            "remove_a_function",
            {"a.py": "def helper() -> int:\n    return 1\n"},
            False,
            {"a.py"},
        ),
        (
            "rename_a_function",
            {
                "a.py": (
                    "def helper() -> int:\n    return 1\n\n"
                    "def renamed() -> int:\n    return 2\n"
                )
            },
            False,
            {"a.py"},
        ),
        (
            "add_a_method_to_an_existing_class",
            {
                "widget.py": (
                    "class Widget:\n"
                    "    def __init__(self) -> None:\n"
                    "        pass\n"
                )
            },
            False,
            {"widget.py", "user.py"},
        ),
        (
            "add_a_struct",
            {"a.py": FIVE_SHAPE_SRC["a.py"] + "class NewType:\n    pass\n"},
            True,
            None,
        ),
    ],
)
def test_parity_across_symbol_edit_shapes(
    make_mapped_repo: RepoFactory,
    label: str,
    after: dict[str, str],
    expect_full_fallback: bool,
    expect_dirty: set[str] | None,
) -> None:
    """The acceptance bar: for each of the five required edit shapes,
    an incremental map and a ``--full`` map of the same edited
    tree must produce identical ``map.json`` -- and the fast path (a
    narrow ``dirty`` set, never falling back to a full resolve except
    for the struct/class case) must actually have been taken, not just
    "not obviously broken". A parity assertion alone is not proof: it
    would pass identically if every case silently fell back to a full
    resolve, which is exactly the bug this work package fixes.
    """
    root = make_mapped_repo(FIVE_SHAPE_SRC)
    for name, text in after.items():
        (root / name).write_text(text)

    reuse = _build(root)
    if expect_full_fallback:
        assert reuse is None, label
    else:
        assert reuse is not None, label
        assert reuse.dirty == expect_dirty, label
        # The fast path only means something if it left files unresolved
        # (reused, not re-touched) -- assert the complement is non-empty.
        assert reuse.dirty != set(FIVE_SHAPE_SRC), label

    _map(root)
    incremental = _graph_json(root)

    _map(root, "--full")
    full = _graph_json(root)

    assert incremental == full, label


def test_reuse_actually_supplies_an_unchanged_files_edges(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the parity tests above from becoming vacuous.

    If reuse silently contributed nothing, every parity assertion would
    still pass (both sides would just full-resolve). Breaking the merge
    on purpose must visibly lose b.py's edge into a.py, proving those
    tests are exercising the cached path at all."""
    root = make_mapped_repo(SRC)
    (root / "a.py").write_text(SRC["a.py"] + "# callee-side comment\n")

    monkeypatch.setattr(resolver_mod, "_merge_reused", lambda *a, **k: None)
    _map(root)

    assert _graph_json(root)["edges"] == [], (
        "with the merge stubbed out, the unchanged caller's edge should "
        "vanish; if it survives, reuse was never being used"
    )


# --- the gate fires, and refuses, when it should ----------------------


def _build(root: Path) -> resolver_mod.ResolveReuse | None:
    """Re-run discovery the way run_map does, then build a reuse plan."""
    cache = cache_mod.IncrementalCache(cache_mod.load(root))
    files, _ = repo_ops_map(root, cache)
    return resolvecache.build_reuse(root, files, cache)


def repo_ops_map(root: Path, cache: cache_mod.IncrementalCache):  # noqa: ANN201
    from dekko import repo_ops

    return repo_ops.map_repository(
        root,
        subpath=None,
        excludes=(),
        max_file_size=1_000_000,
        cache=cache,
        jobs=1,
    )


def test_gate_fires_on_a_body_only_edit(
    make_mapped_repo: RepoFactory,
) -> None:
    """The case the feature exists for: only the edited file is dirty."""
    root = make_mapped_repo(SRC)
    (root / "b.py").write_text(SRC["b.py"] + "# just a comment\n")

    reuse = _build(root)

    assert reuse is not None
    assert reuse.dirty == {"b.py"}
    assert set(reuse.cached) == {"a.py", "b.py"}


def test_gate_fires_with_nothing_dirty_when_no_file_changed(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    reuse = _build(root)
    assert reuse is not None
    assert reuse.dirty == frozenset()


def test_gate_narrows_when_an_unrelated_symbol_is_added(
    make_mapped_repo: RepoFactory,
) -> None:
    """v2: a new symbol only dirties files that could depend on its
    *name* (``resolver.name_delta``), not the whole repo.

    ``newly_added`` never appears in b.py's cached call resolution (no
    edge, ambiguous entry, or import references it), so b.py's cached
    edge into ``a.helper`` stays trustworthy and only a.py itself needs
    re-resolving. v1 forced a full re-resolve here; that blanket rule is
    what this change exists to narrow.
    """
    root = make_mapped_repo(SRC)
    (root / "a.py").write_text(
        SRC["a.py"] + "def newly_added() -> int:\n    return 9\n"
    )
    reuse = _build(root)
    assert reuse is not None
    assert reuse.dirty == {"a.py"}


def test_gate_narrows_when_an_unrelated_symbol_is_renamed(
    make_mapped_repo: RepoFactory,
) -> None:
    """Same as above for a rename (remove ``other``, add ``renamed``):
    neither bare name appears in b.py's cached resolution, so b.py stays
    clean."""
    root = make_mapped_repo(SRC)
    (root / "a.py").write_text(SRC["a.py"].replace("other", "renamed"))
    reuse = _build(root)
    assert reuse is not None
    assert reuse.dirty == {"a.py"}


def test_gate_refuses_when_a_file_is_added(
    make_mapped_repo: RepoFactory,
) -> None:
    """Path-set changes can alter resolution for files that reference no
    changed name at all (repo_stems, crate roots, module matching), which
    no symbol comparison would catch."""
    root = make_mapped_repo(SRC)
    (root / "c.py").write_text("def fresh() -> int:\n    return 4\n")
    assert _build(root) is None


def test_gate_refuses_when_a_file_is_deleted(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    (root / "a.py").unlink()
    assert _build(root) is None


def test_gate_widens_when_a_called_symbols_signature_changes(
    make_mapped_repo: RepoFactory,
) -> None:
    """``params`` feeds arity gating in the resolution ladder, so it is
    compared even though the symbol's name and id are unchanged --
    and, unlike the unrelated-name cases above, b.py's cached edge
    *does* name ``helper`` (the changed symbol), so b.py must widen into
    ``dirty`` alongside a.py rather than staying clean."""
    root = make_mapped_repo(SRC)
    (root / "a.py").write_text(
        SRC["a.py"].replace("def helper() -> int:", "def helper(x, y=1):")
    )
    reuse = _build(root)
    assert reuse is not None
    assert reuse.dirty == {"a.py", "b.py"}


def test_gate_refuses_outright_when_a_type_kind_symbol_changes(
    make_mapped_repo: RepoFactory,
) -> None:
    """A type-kind addition/removal/change (``model.TYPE_KINDS``) still
    forces a full resolve -- the type-aware ladder steps
    (``_receiver_type_match`` and friends) read the whole repo-wide
    index for a type name regardless of which file wrote the call, so
    there is no bounded "affected files" set to compute the way there
    is for a function/method/variable name."""
    root = make_mapped_repo(SRC)
    (root / "a.py").write_text(SRC["a.py"] + "class NewType:\n    pass\n")
    assert _build(root) is None


def test_gate_widens_for_a_new_constructor_on_an_existing_class(
    make_mapped_repo: RepoFactory,
) -> None:
    """Constructor-collapse gap (dependency audit): adding
    ``__init__`` to a class never changes the class's own bare name, so
    a caller's already-cached ``Widget()`` edge doesn't literally
    mention ``__init__`` anywhere. ``_constructor_of`` would now find
    the new ``__init__`` and add a second edge to it, so the caller must
    be marked dirty even though nothing about its own call site's name
    changed -- ``name_delta`` folds the class's own name into
    ``changed`` whenever a constructor-shaped method inside it changes.
    """
    src = {
        "widget.py": "class Widget:\n    pass\n",
        "caller.py": (
            "from widget import Widget\n\n"
            "def make() -> Widget:\n    return Widget()\n"
        ),
    }
    root = make_mapped_repo(src)
    (root / "widget.py").write_text(
        "class Widget:\n    def __init__(self) -> None:\n        pass\n"
    )
    reuse = _build(root)
    assert reuse is not None
    assert reuse.dirty == {"widget.py", "caller.py"}


def test_gate_widens_for_an_import_alias_newly_resolving(
    make_mapped_repo: RepoFactory,
) -> None:
    """Import-alias gap (dependency audit): a call through an
    import alias whose target doesn't exist yet is cached as
    ``external``, keyed by the alias text as written -- never by the
    real name ``_alias_candidates`` looked up
    (``resolver.alias_original_name``). Defining that real name
    elsewhere must still widen the aliasing caller into ``dirty``, even
    though the alias text itself was never a cached dependency name."""
    src = {
        "mod.py": "def other() -> int:\n    return 0\n",
        "caller.py": (
            "from mod import real as aliased\n\n"
            "def use() -> int:\n    return aliased()\n"
        ),
    }
    root = make_mapped_repo(src)
    (root / "mod.py").write_text(
        "def other() -> int:\n    return 0\n\n"
        "def real() -> int:\n    return 1\n"
    )
    reuse = _build(root)
    assert reuse is not None
    assert reuse.dirty == {"mod.py", "caller.py"}


# --- invalidation keys ------------------------------------------------


def test_a_resolver_change_invalidates_the_cache(
    make_mapped_repo: RepoFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extraction ``spec_hash`` says nothing about the resolution
    ladder, so editing ``_pick_candidate`` would otherwise silently reuse
    edges resolved by the old ladder."""
    root = make_mapped_repo(SRC)
    assert _fired(root)

    # Patched where ``resolvecache`` looks it up: the module from-imports
    # the function, so it holds its own binding.
    monkeypatch.setattr(
        resolvecache, "resolve_fingerprint", lambda: "a-different-ladder"
    )
    assert resolvecache.load(root) is None


def test_a_corrupt_cache_is_a_miss_not_a_crash(
    make_mapped_repo: RepoFactory,
) -> None:
    """A garbage cache must read as "no cache", never raise. It self-heals
    on the next run that actually does work: a no-op map short-circuits
    before resolution (``_maybe_short_circuit``) and so deliberately
    rewrites nothing, but any real edit rebuilds it."""
    root = make_mapped_repo(SRC)
    _cache_path(root).write_bytes(b"not gzip at all")
    assert resolvecache.load(root) is None

    _map(root)
    assert not _fired(root), "a byte-identical no-op run writes nothing"

    (root / "b.py").write_text(SRC["b.py"] + "# real change\n")
    _map(root)
    assert _fired(root)


def test_truncated_id_table_is_a_miss_not_a_crash(
    make_mapped_repo: RepoFactory,
) -> None:
    root = make_mapped_repo(SRC)
    doc = json.loads(gzip.decompress(_cache_path(root).read_bytes()))
    doc["ids"] = []
    _cache_path(root).write_bytes(gzip.compress(json.dumps(doc).encode()))
    assert resolvecache.load(root) is None


def test_full_run_still_writes_a_usable_cache(
    make_mapped_repo: RepoFactory,
) -> None:
    """A ``--full`` run ignores the cache for reuse but must still leave
    one behind, or the next incremental run has nothing to work from."""
    root = make_mapped_repo(SRC)
    _cache_path(root).unlink()
    _map(root, "--full")
    assert _fired(root)


def test_gate_miss_still_leaves_a_fresh_cache(
    make_mapped_repo: RepoFactory,
) -> None:
    """A miss that wrote nothing back would make every later run miss
    too, permanently."""
    root = make_mapped_repo(SRC)
    (root / "c.py").write_text("def fresh() -> int:\n    return 4\n")
    assert _build(root) is None

    _map(root)

    doc = json.loads(gzip.decompress(_cache_path(root).read_bytes()))
    assert set(doc["files"]) == {"a.py", "b.py", "c.py"}
    # The now-current tree reuses cleanly on the following run.
    reuse = _build(root)
    assert reuse is not None
    assert reuse.dirty == frozenset()


# --- symbol projection ------------------------------------------------


def test_projection_ignores_line_numbers_and_docs() -> None:
    """The exclusions that make the whole feature work: a body edit
    shifts every later symbol's line numbers, and if that counted as a
    change the gate would never open on the edit shape it exists for."""
    base = {
        "id": "a.py::f",
        "name": "f",
        "qualname": "f",
        "kind": "function",
        "path": "a.py",
        "language": "python",
        "params": [],
        "returns": None,
        "start_line": 1,
        "end_line": 2,
        "decorated": False,
        "exported": True,
        "doc": None,
        "test": False,
    }
    moved = {**base, "start_line": 99, "end_line": 120, "doc": "new doc"}
    assert resolver_mod.symbol_projection(
        [base]
    ) == resolver_mod.symbol_projection([moved])


def test_projection_is_order_sensitive() -> None:
    """The ``#N`` collision suffix is assigned in extraction order, so
    reordering two same-qualname symbols swaps which id means which
    symbol while leaving the *set* of ids identical."""
    one = {
        "id": "a.py::dup",
        "name": "dup",
        "qualname": "dup",
        "kind": "function",
        "path": "a.py",
        "language": "python",
        "params": [],
        "returns": None,
        "start_line": 1,
        "end_line": 2,
        "decorated": False,
        "exported": True,
        "doc": None,
        "test": False,
    }
    two = {**one, "id": "a.py::dup#1", "kind": "method"}
    assert resolver_mod.symbol_projection(
        [one, two]
    ) != resolver_mod.symbol_projection([two, one])


def test_projection_compares_every_non_excluded_field() -> None:
    """Driven off ``dataclasses.fields(Symbol)`` minus the blind set, so
    a field added to Symbol later is compared by default rather than
    silently ignored."""
    projected = set(resolver_mod._projected_symbol_fields())
    assert projected.isdisjoint(resolver_mod._RESOLUTION_BLIND_SYMBOL_FIELDS)
    for required in ("id", "name", "kind", "params", "path", "qualname"):
        assert required in projected
