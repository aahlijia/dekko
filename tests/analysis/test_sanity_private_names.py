"""``sanity`` on an ES private method (``#name``)."""

import json

import pytest

from dekko.analysis import sanity
from dekko.integrations import cli

from conftest import RepoFactory


def test_the_printed_grep_command_quotes_the_name() -> None:
    """Pasted into a shell, an unquoted ``#priv`` turns into a comment."""
    assert "'#priv'" in sanity._grep_command_text("#priv")
    assert sanity._grep_command_text("target").endswith("-- target .")


def test_sanity_runs_on_a_private_method(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "log.ts": (
                "export class Logger {\n"
                "  #priv(): number { return 1 }\n"
                "  callPriv(): number { return this.#priv() }\n"
                "}\n"
            ),
        }
    )
    target = "log.ts::Logger.#priv"
    assert cli.main(["sanity", target, "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "'#priv'" in doc["grep_command"]
    assert not doc["grep_only"]
