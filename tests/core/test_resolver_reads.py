"""``resolver.collect_reads``: the property-read aggregation pass."""

from dekko.core.model import FileMap, RawRead, Symbol
from dekko.core.resolver import MODULE_CALLER_SUFFIX, collect_reads


def _sym(path: str, name: str, kind: str = "function") -> Symbol:
    return Symbol(
        id=f"{path}::{name}",
        name=name,
        qualname=name,
        kind=kind,
        path=path,
        language="typescript",
    )


def _read(path: str, name: str, line: int, caller: str | None) -> RawRead:
    return RawRead(caller_id=caller, path=path, name=name, line=line)


def test_reads_are_kept_only_for_repo_callable_or_variable_names() -> None:
    files = [
        FileMap(
            path="cmd.ts",
            language="typescript",
            symbols=[
                _sym("cmd.ts", "isHidden"),
                _sym("cmd.ts", "config", kind="variable"),
                _sym("cmd.ts", "Command", kind="interface"),
            ],
        ),
        FileMap(
            path="use.ts",
            language="typescript",
            reads=[
                _read("use.ts", "isHidden", 3, "use.ts::pick"),
                _read("use.ts", "config", 4, "use.ts::pick"),
                _read("use.ts", "Command", 5, "use.ts::pick"),
                _read("use.ts", "length", 6, "use.ts::pick"),
            ],
        ),
    ]
    names = {s.name for s in collect_reads(files)}
    # A type's name is not a property anyone reads off a value, and
    # ``length`` matches nothing in the repo.
    assert names == {"isHidden", "config"}


def test_reads_group_lines_per_reader_and_name() -> None:
    files = [
        FileMap(
            path="cmd.ts",
            language="typescript",
            symbols=[_sym("cmd.ts", "isHidden")],
        ),
        FileMap(
            path="use.ts",
            language="typescript",
            reads=[
                _read("use.ts", "isHidden", 9, "use.ts::pick"),
                _read("use.ts", "isHidden", 3, "use.ts::pick"),
                _read("use.ts", "isHidden", 3, "use.ts::pick"),
                _read("use.ts", "isHidden", 12, None),
            ],
        ),
    ]
    sites = collect_reads(files)
    assert [(s.reader, s.lines) for s in sites] == [
        (f"use.ts{MODULE_CALLER_SUFFIX}", [12]),
        ("use.ts::pick", [3, 9]),
    ]
    assert all(s.name == "isHidden" for s in sites)
