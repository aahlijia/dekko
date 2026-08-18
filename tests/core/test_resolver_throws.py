"""Resolver tests for raise/throw sites and except/catch clauses
(resolve_throws()/resolve_catches()).

A deliberately lighter-weight ladder than resolve_heritage()'s full
_pick_candidate (see resolver.py's "Throws/catches" section docstring):
unique repo-wide name, same-file, import-hint — landing on
resolved/ambiguous/external the same three-bucket shape heritage uses,
plus a fourth bucket (bare re-raise) heritage has no equivalent of.
resolve_catches() is even lighter: matching a `dekko query catches Y`
request is done by name against CatchSite.type_names directly, so
resolution here only populates `repo_types` for summary disclosure,
never gating whether a clause is considered a match.
"""

from dekko.core.model import FileMap, Import, RawCatch, RawThrow, Symbol
from dekko.core.resolver import MODULE_CALLER_SUFFIX, resolve


def _cls(path: str, name: str, line: int = 1) -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind="class",
        path=path,
        language="python",
        start_line=line,
        end_line=line + 1,
    )


def _fn(path: str, name: str, line: int = 1) -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind="function",
        path=path,
        language="python",
        start_line=line,
        end_line=line + 1,
    )


def _throw(
    caller_id: str | None,
    path: str,
    name: str | None,
    text: str | None = None,
    line: int = 1,
) -> RawThrow:
    return RawThrow(
        caller_id=caller_id,
        path=path,
        text=text if text is not None else name,
        name=name,
        line=line,
    )


def _catch(
    caller_id: str | None,
    path: str,
    types: list[str],
    bare: bool = False,
    line: int = 1,
) -> RawCatch:
    return RawCatch(
        caller_id=caller_id, path=path, types=types, bare=bare, line=line
    )


# ---------------------------------------------------------------------
# resolve_throws()


def test_same_file_resolution() -> None:
    err = _cls("a.py", "ConfigError")
    fn = _fn("a.py", "load", line=3)
    files = [
        FileMap(
            "a.py",
            "python",
            symbols=[err, fn],
            throws=[_throw("a.py::load", "a.py", "ConfigError", line=4)],
        )
    ]
    graph = resolve(files)
    assert graph.throws_out["a.py::load"] == ["a.py::ConfigError"]
    assert graph.throws[0].lines == [4]
    assert graph.throws_ambiguous == []
    assert graph.throws_external == []


def test_cross_file_resolution_via_import_hint() -> None:
    err = _cls("errors.py", "ConfigError")
    fn = _fn("a.py", "load")
    files = [
        FileMap("errors.py", "python", symbols=[err]),
        FileMap(
            "a.py",
            "python",
            symbols=[fn],
            imports=[
                Import(
                    path="a.py",
                    name="ConfigError",
                    source="errors.ConfigError",
                )
            ],
            throws=[_throw("a.py::load", "a.py", "ConfigError")],
        ),
    ]
    graph = resolve(files)
    assert graph.throws_out["a.py::load"] == ["errors.py::ConfigError"]


def test_unique_repo_wide_name_fallback() -> None:
    err = _cls("distant.py", "UniquelyNamedError")
    fn = _fn("a.py", "load")
    files = [
        FileMap("distant.py", "python", symbols=[err]),
        FileMap(
            "a.py",
            "python",
            symbols=[fn],
            throws=[_throw("a.py::load", "a.py", "UniquelyNamedError")],
        ),
    ]
    graph = resolve(files)
    assert graph.throws_out["a.py::load"] == ["distant.py::UniquelyNamedError"]


def test_same_named_types_in_two_files_are_ambiguous() -> None:
    err1 = _cls("a.py", "ConfigError")
    err2 = _cls("b.py", "ConfigError")
    fn = _fn("c.py", "load")
    files = [
        FileMap("a.py", "python", symbols=[err1]),
        FileMap("b.py", "python", symbols=[err2]),
        FileMap(
            "c.py",
            "python",
            symbols=[fn],
            throws=[_throw("c.py::load", "c.py", "ConfigError")],
        ),
    ]
    graph = resolve(files)
    assert graph.throws_out == {}
    assert graph.throws_ambiguous == [
        (
            "c.py::load",
            "ConfigError",
            ["a.py::ConfigError", "b.py::ConfigError"],
        )
    ]


def test_no_candidate_is_external() -> None:
    fn = _fn("a.py", "load")
    files = [
        FileMap(
            "a.py",
            "python",
            symbols=[fn],
            throws=[
                _throw(
                    "a.py::load", "a.py", "ValueError", text="ValueError('x')"
                )
            ],
        )
    ]
    graph = resolve(files)
    assert graph.throws_out == {}
    assert graph.throws_ambiguous == []
    assert len(graph.throws_external) == 1
    ext = graph.throws_external[0]
    assert ext.caller == "a.py::load"
    assert ext.callee == "ValueError('x')"


def test_candidates_filtered_to_type_kinds() -> None:
    # A same-named *function* must never be picked as a resolved
    # raised type — throws candidates are pre-filtered to TYPE_KINDS.
    same_named_fn = _fn("a.py", "ConfigError")
    caller = _fn("b.py", "load")
    files = [
        FileMap("a.py", "python", symbols=[same_named_fn]),
        FileMap(
            "b.py",
            "python",
            symbols=[caller],
            throws=[_throw("b.py::load", "b.py", "ConfigError")],
        ),
    ]
    graph = resolve(files)
    assert graph.throws_out == {}
    assert len(graph.throws_external) == 1


def test_module_level_throw_uses_module_pseudo_id() -> None:
    err = _cls("a.py", "ConfigError")
    files = [
        FileMap(
            "a.py",
            "python",
            symbols=[err],
            throws=[_throw(None, "a.py", "ConfigError", line=9)],
        )
    ]
    graph = resolve(files)
    module_id = f"a.py{MODULE_CALLER_SUFFIX}"
    assert graph.throws_out[module_id] == ["a.py::ConfigError"]


def test_bare_reraise_is_tracked_separately() -> None:
    fn = _fn("a.py", "load")
    files = [
        FileMap(
            "a.py",
            "python",
            symbols=[fn],
            throws=[_throw("a.py::load", "a.py", None, line=7)],
        )
    ]
    graph = resolve(files)
    assert graph.throws_out == {}
    assert graph.throws_external == []
    assert graph.throws_bare == [("a.py::load", "a.py", 7)]


# ---------------------------------------------------------------------
# resolve_catches()


def test_catch_site_preserves_type_names_regardless_of_resolution() -> None:
    fn = _fn("a.py", "load")
    files = [
        FileMap(
            "a.py",
            "python",
            symbols=[fn],
            catches=[_catch("a.py::load", "a.py", ["ValueError"], line=5)],
        )
    ]
    graph = resolve(files)
    assert len(graph.catches) == 1
    site = graph.catches[0]
    assert site.type_names == ["ValueError"]
    # ValueError never resolves to a repo symbol -- repo_types stays
    # empty, but the clause itself is still recorded (matching by name
    # is the query-time concern, not a resolution-time gate).
    assert site.repo_types == {}
    assert site.bare is False


def test_catch_site_populates_repo_types_when_resolvable() -> None:
    err = _cls("errors.py", "ConfigError")
    fn = _fn("a.py", "load")
    files = [
        FileMap("errors.py", "python", symbols=[err]),
        FileMap(
            "a.py",
            "python",
            symbols=[fn],
            imports=[
                Import(
                    path="a.py",
                    name="ConfigError",
                    source="errors.ConfigError",
                )
            ],
            catches=[_catch("a.py::load", "a.py", ["ConfigError"])],
        ),
    ]
    graph = resolve(files)
    site = graph.catches[0]
    assert site.repo_types == {"ConfigError": "errors.py::ConfigError"}


def test_bare_catch_all_has_no_types() -> None:
    fn = _fn("a.py", "load")
    files = [
        FileMap(
            "a.py",
            "python",
            symbols=[fn],
            catches=[_catch("a.py::load", "a.py", [], bare=True, line=8)],
        )
    ]
    graph = resolve(files)
    site = graph.catches[0]
    assert site.type_names == []
    assert site.repo_types == {}
    assert site.bare is True


def test_module_level_catch_uses_module_pseudo_id() -> None:
    fn = _fn("a.py", "helper")
    files = [
        FileMap(
            "a.py",
            "python",
            symbols=[fn],
            catches=[_catch(None, "a.py", ["ValueError"], line=2)],
        )
    ]
    graph = resolve(files)
    module_id = f"a.py{MODULE_CALLER_SUFFIX}"
    assert graph.catches[0].caller == module_id
