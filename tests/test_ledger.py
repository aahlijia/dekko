"""Pillar C read side: the transcript-projected session ledger.

Anchored on a static golden transcript fixture (the R1 schema-drift
guard): a mix of whole/partial reads, a subagent read, an out-of-root
read, a dekko emission, real usage tokens, and deliberate junk lines.
The fixture uses a fixed ``/repo`` root, decoupled from the tmp map.
"""

from pathlib import Path

from dekko import cli, ledger
from dekko.mapfile import MapIndex, load_map

from conftest import RepoFactory

_FIXTURE = (
    Path(__file__).parent / "fixtures" / "transcripts" / "session_basic.jsonl"
)
_ROOT = Path("/repo")

_FILES = {
    "src/auth.py": (
        '"""Auth."""\n'
        "def login() -> None:\n    pass\n"
        "def logout() -> None:\n    pass\n"
    ),
    "src/db.py": (
        '"""DB."""\n'
        "def connect() -> None:\n    pass\n"
        "def disconnect() -> None:\n    pass\n"
    ),
}


def _index(make_mapped_repo: RepoFactory) -> MapIndex:
    index = load_map(make_mapped_repo(_FILES))
    assert index is not None
    return index


def _view(make_mapped_repo: RepoFactory) -> ledger.LedgerView:
    return ledger.build_view(_FIXTURE, _index(make_mapped_repo), _ROOT)


# --- core projection -------------------------------------------------


def test_session_id_and_turns(make_mapped_repo: RepoFactory) -> None:
    view = _view(make_mapped_repo)
    assert view.session_id == "sess-abc"
    assert view.turns == 6          # six assistant records


def test_real_usage_uses_peak(make_mapped_repo: RepoFactory) -> None:
    # Peak usage: 1500 + 600 + 400 (the larger, later turn), robust to a
    # trailing in-progress turn whose usage is still zero.
    assert _view(make_mapped_repo).consumed_tokens == 2500


def test_whole_read_captures_all_symbols(
    make_mapped_repo: RepoFactory,
) -> None:
    view = _view(make_mapped_repo)
    auth = view.files["src/auth.py"]
    assert auth.fully_read is True
    names = {sid.rsplit("::", 1)[-1] for sid in auth.symbols_seen}
    assert names == {"login", "logout"}


def test_partial_read_captures_only_in_range(
    make_mapped_repo: RepoFactory,
) -> None:
    view = _view(make_mapped_repo)
    db = view.files["src/db.py"]
    assert db.fully_read is False
    names = {sid.rsplit("::", 1)[-1] for sid in db.symbols_seen}
    # offset 1, limit 3 -> lines [1,4): connect (l.2) in, disconnect (l.4) out
    assert names == {"connect"}


def test_subagent_and_out_of_root_reads_ignored(
    make_mapped_repo: RepoFactory,
) -> None:
    view = _view(make_mapped_repo)
    assert "src/secret.py" not in view.files     # subagent caller
    assert not any("hosts" in p for p in view.files)  # outside root


def test_dekko_emission_attributed(make_mapped_repo: RepoFactory) -> None:
    assert _view(make_mapped_repo).files["src/auth.py"].dekko_emitted is True


def test_aggregate_counts(make_mapped_repo: RepoFactory) -> None:
    view = _view(make_mapped_repo)
    assert len(view.files) == 2
    assert len(view.symbols) == 3
    assert view.has_file("src/auth.py")
    assert not view.has_file("src/missing.py")


def test_remaining_budget(make_mapped_repo: RepoFactory) -> None:
    view = _view(make_mapped_repo)        # consumed 2500
    assert view.remaining(4000) == 1500
    assert view.remaining(1000) == 0      # floored, never negative


# --- robustness: the R1 guard ----------------------------------------


def test_peak_survives_trailing_zero_usage(tmp_path: Path) -> None:
    # The live-write hazard: a real 2500-token turn followed by an
    # in-progress turn whose usage is still zero must not collapse to 0.
    transcript = tmp_path / "live.jsonl"
    transcript.write_text(
        '{"type":"assistant","message":{"role":"assistant","usage":'
        '{"input_tokens":2000,"cache_read_input_tokens":300,'
        '"cache_creation_input_tokens":200}}}\n'
        '{"type":"assistant","message":{"role":"assistant","usage":'
        '{"input_tokens":0,"cache_read_input_tokens":0,'
        '"cache_creation_input_tokens":0}}}\n'
    )
    view = ledger.build_view(transcript, MapIndex(root_label="x"), _ROOT)
    assert view.consumed_tokens == 2500


def test_missing_transcript_is_empty_not_error() -> None:
    view = ledger.build_view(
        Path("/no/such/transcript.jsonl"), MapIndex(root_label="x"), _ROOT
    )
    assert view.files == {} and view.consumed_tokens == 0


def test_iter_records_skips_junk() -> None:
    records = list(ledger.iter_records(_FIXTURE))
    # 11 well-formed objects survive (incl. snapshot + mode records); the
    # malformed line and the bare `42` (not an object) both drop.
    assert len(records) == 11
    assert all(isinstance(r, dict) for r in records)


def test_empty_index_still_tallies(make_mapped_repo: RepoFactory) -> None:
    # No map at all: tokens/turns/files still resolve; symbols just empty.
    view = ledger.build_view(_FIXTURE, MapIndex(root_label="x"), _ROOT)
    assert view.consumed_tokens == 2500
    assert "src/auth.py" in view.files
    assert view.symbols == set()


# --- CLI command -----------------------------------------------------


def test_cli_ledger_json(
    make_mapped_repo: RepoFactory, capsys: object
) -> None:
    root = make_mapped_repo(_FILES)
    code = cli.main(
        ["ledger", "--transcript", str(_FIXTURE), "--root", str(root),
         "--json"]
    )
    assert code == 0
    import json

    doc = json.loads(capsys.readouterr().out)
    assert doc["session_id"] == "sess-abc"
    assert doc["consumed_tokens"] == 2500
    assert doc["turns"] == 6


def test_cli_ledger_no_transcript_exit_code(tmp_path: Path) -> None:
    code = cli.main(
        ["ledger", "--transcript", str(tmp_path / "nope.jsonl"),
         "--root", str(tmp_path)]
    )
    assert code == ledger.EXIT_NO_TRANSCRIPT


def test_mcp_ledger_tool(make_mapped_repo: RepoFactory) -> None:
    from dekko import server

    root = make_mapped_repo(_FILES)
    ctx = server.Context(default_root=root, no_regen=False)
    out = server.tool_ledger(
        ctx, {"transcript": str(_FIXTURE), "root": str(root)}
    )
    assert "ledger · session sess-abc" in out
    assert "2 turns" not in out          # six assistant turns, not two
    assert "6 turns" in out
