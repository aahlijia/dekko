"""Direct unit tests for ``source.read_lines``.

No test anywhere in the suite called ``read_lines`` directly before
this (sc:analyze post-0.31.1 fixes plan item 4) -- its happy path is
likely touched indirectly through ``outline``/``contextpack``, but the
explicit ``OSError``-swallowing fallback (the function's whole reason
for existing, per its own docstring: "Reads never raise") was not
pinned by any existing test.
"""

from pathlib import Path

from dekko.source import read_lines


def test_read_lines_reads_a_real_file(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("line one\nline two\nline three\n")
    assert read_lines(tmp_path, "a.py") == [
        "line one",
        "line two",
        "line three",
    ]


def test_read_lines_returns_empty_list_for_missing_file(
    tmp_path: Path,
) -> None:
    assert read_lines(tmp_path, "does_not_exist.py") == []
