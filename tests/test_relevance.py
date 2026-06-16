"""Pillar B: task-aware relevance scoring and the --task blend.

The pure core (:mod:`dekko.relevance`) is tested offline and
deterministically; the three blend points (lean, workset, context) are
tested for both the task-aware ordering and byte-for-byte backward
compatibility when no task is supplied.
"""

from pathlib import Path

from dekko import cli, contextpack, query, relevance, render_lean, workset
from dekko.mapfile import MapIndex, load_map
from dekko.relevance import (
    Candidate,
    LexicalScorer,
    TaskContext,
    blended_scores,
    normalize_terms,
)

from conftest import RepoFactory

# --- pure core: normalize_terms --------------------------------------


def test_normalize_splits_camel_and_snake() -> None:
    terms = set(normalize_terms("parseInput load_config"))
    assert {"parse", "input", "load", "config"} <= terms


def test_normalize_drops_stopwords_and_short_tokens() -> None:
    terms = normalize_terms("fix the LoginForm bug a x")
    assert "fix" not in terms          # stop word
    assert "the" not in terms          # stop word
    assert "x" not in terms            # too short
    assert "login" in terms
    assert "form" in terms
    assert "bug" in terms


def test_normalize_dedupes_preserving_order() -> None:
    assert normalize_terms("config config load config") == ["config", "load"]


# --- pure core: LexicalScorer ----------------------------------------


def test_exact_overlap_outranks_no_match() -> None:
    task = TaskContext(terms=("login", "form"))
    cands = [
        Candidate("hit", "login form handler", "a.py"),
        Candidate("miss", "database pool", "b.py"),
    ]
    scores = LexicalScorer().score(task, cands)
    assert scores["hit"] == 1.0       # normalized top
    assert scores["miss"] == 0.0


def test_no_candidate_matches_is_all_zero() -> None:
    task = TaskContext(terms=("nonexistent",))
    cands = [Candidate("a", "alpha", "a.py"), Candidate("b", "beta", "b.py")]
    assert LexicalScorer().score(task, cands) == {"a": 0.0, "b": 0.0}


def test_partial_substring_match_scores_above_zero() -> None:
    task = TaskContext(terms=("auth",))
    cands = [
        Candidate("hit", "authenticate user", "a.py"),
        Candidate("miss", "logout", "b.py"),
    ]
    scores = LexicalScorer().score(task, cands)
    assert scores["hit"] > scores["miss"] == 0.0


def test_diff_path_boost_beats_recent_beats_none() -> None:
    task = TaskContext(
        terms=(), diff_paths=frozenset({"a.py"}),
        recent_paths=frozenset({"b.py"}),
    )
    cands = [
        Candidate("diff", "x", "a.py"),
        Candidate("recent", "y", "b.py"),
        Candidate("cold", "z", "c.py"),
    ]
    scores = LexicalScorer().score(task, cands)
    assert scores["diff"] > scores["recent"] > scores["cold"] == 0.0


# --- pure core: blended_scores ---------------------------------------


def test_blend_w_rel_one_is_pure_relevance() -> None:
    task = TaskContext(terms=("beta",))
    cands = [Candidate("a", "alpha", "a.py"), Candidate("b", "beta", "b.py")]
    central = {"a": 100.0, "b": 0.0}
    blended = blended_scores(task, cands, central, w_rel=1.0)
    assert blended["b"] == 1.0 and blended["a"] == 0.0


def test_blend_w_rel_zero_is_pure_centrality() -> None:
    task = TaskContext(terms=("beta",))
    cands = [Candidate("a", "alpha", "a.py"), Candidate("b", "beta", "b.py")]
    central = {"a": 100.0, "b": 0.0}
    blended = blended_scores(task, cands, central, w_rel=0.0)
    assert blended["a"] == 1.0 and blended["b"] == 0.0


def test_blend_flat_centrality_contributes_nothing() -> None:
    task = TaskContext(terms=("beta",))
    cands = [Candidate("a", "alpha", "a.py"), Candidate("b", "beta", "b.py")]
    blended = blended_scores(task, cands, {"a": 5.0, "b": 5.0}, w_rel=0.0)
    assert blended == {"a": 0.0, "b": 0.0}


def test_task_context_is_empty() -> None:
    assert TaskContext().is_empty is True
    assert TaskContext(terms=("x",)).is_empty is False
    assert TaskContext(diff_paths=frozenset({"a.py"})).is_empty is False


# --- integration fixtures --------------------------------------------

_TWO_LEAVES = {
    "src/auth.py": (
        '"""Authentication."""\n'
        "def login() -> None:\n    pass\n"
    ),
    "src/db.py": (
        '"""Database access."""\n'
        "def connect() -> None:\n    pass\n"
    ),
}

_CALLERS = {
    "src/core.py": (
        '"""Core."""\n'
        "def target() -> None:\n    pass\n"
        "def alpha() -> None:\n    target()\n"
        "def bravo() -> None:\n    target()\n"
    ),
}


def _index(
    make_mapped_repo: RepoFactory, files: dict[str, str]
) -> tuple[Path, MapIndex]:
    root = make_mapped_repo(files)
    index = load_map(root)
    assert index is not None
    return root, index


# --- lean blend ------------------------------------------------------


def test_lean_relevance_lifts_matching_symbol(
    make_mapped_repo: RepoFactory,
) -> None:
    root, index = _index(make_mapped_repo, _TWO_LEAVES)
    model = render_lean.build_model(index, root)
    task = relevance.task_context("work on login", root)
    scores = render_lean._relevance_scores(model, task)
    login = next(k for k in scores if k.endswith("login"))
    connect = next(k for k in scores if k.endswith("connect"))
    # Equal (zero) centrality leaves; the task term breaks the tie.
    assert scores[login] > scores[connect]


def test_lean_live_atoms_sort_lowest_survival_first(
    make_mapped_repo: RepoFactory,
) -> None:
    root, index = _index(make_mapped_repo, _TWO_LEAVES)
    model = render_lean.build_model(index, root)
    ids = [
        a.sym_id
        for atoms in model.atoms_by_path.values()
        for a in atoms
    ]
    scores = {sid: float(i) for i, sid in enumerate(sorted(ids))}
    ordered = render_lean._live_atoms(model, scores)
    got = [a.sym_id for a in ordered]
    assert got == sorted(got, key=lambda s: (scores[s], s))


def test_lean_without_task_is_unchanged(
    make_mapped_repo: RepoFactory,
) -> None:
    root, index = _index(make_mapped_repo, _TWO_LEAVES)
    a, _ = render_lean.generate(index, root)
    b, _ = render_lean.generate(index, root, task=TaskContext())
    assert a == b


# --- workset blend ---------------------------------------------------


def test_workset_apply_task_reorders_touched_and_files(
    make_mapped_repo: RepoFactory,
) -> None:
    root, index = _index(make_mapped_repo, _TWO_LEAVES)
    login = index.symbols_by_path["src/auth.py"][0]
    connect = index.symbols_by_path["src/db.py"][0]
    seed = workset.Seed(
        mode="rev", label="t", rev=None, symbol=None,
        touched=[connect, login],
        files=["src/db.py", "src/auth.py"],
        impacts=[],
    )
    task = relevance.task_context("fix the login flow", root)
    out = workset._apply_task(seed, index, task)
    assert out.touched[0].id == login.id
    assert out.files[0] == "src/auth.py"


# --- context blend ---------------------------------------------------


def test_context_entry_scores_favor_task_match(
    make_mapped_repo: RepoFactory,
) -> None:
    root, index = _index(make_mapped_repo, _CALLERS)
    target, _ = query.resolve_target(index, "target")
    assert target is not None
    pack = contextpack.build_pack(index, target, 1)
    task = relevance.task_context("touch alpha", root)
    scores = contextpack._entry_scores(index, pack, task)
    alpha = next(k for k in scores if k.endswith("alpha"))
    bravo = next(k for k in scores if k.endswith("bravo"))
    assert scores[alpha] > scores[bravo]


# --- CLI smoke: --task accepted everywhere ---------------------------


def test_cli_task_flag_accepted(make_mapped_repo: RepoFactory) -> None:
    root = make_mapped_repo(_TWO_LEAVES)
    r = str(root)
    assert cli.main(["lean", "--root", r, "--task", "login"]) == 0
    assert cli.main(
        ["context", "login", "--root", r, "--task", "auth"]
    ) == 0
    assert cli.main(
        ["workset", "--symbol", "login", "--root", r, "--task", "auth"]
    ) == 0
