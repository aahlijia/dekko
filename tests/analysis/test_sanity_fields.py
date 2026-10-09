"""``sanity`` on a call through a field: the field's type says why the
map has no edge here."""

import json
from pathlib import Path

import pytest

from dekko.analysis import sanity
from dekko.integrations import cli

from conftest import RepoFactory


def _rows(
    root: Path, target: str, capsys: pytest.CaptureFixture
) -> dict[tuple[str, int], str]:
    assert cli.main(["sanity", target, "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    return {(r["file"], r["line"]): r["cause"] for r in doc["grep_only"]}


def test_foreign_typed_field_miss_gets_its_own_cause(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "api.ts": "export class Api { get(u: string) {} }\n",
            "w.ts": (
                "import { AxiosInstance } from 'axios';\n"
                "export class W {\n"
                "  private client: AxiosInstance;\n"
                '  f() { this.client.get("x"); }\n'
                "}\n"
            ),
        }
    )
    rows = _rows(root, "api.ts::Api.get", capsys)
    assert rows[("w.ts", 4)] == sanity.CAUSE_FOREIGN_RECEIVER_TYPE


def test_untyped_field_miss_cause(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    root = make_mapped_repo(
        {
            "api.py": (
                "class Api:\n    def fetch(self):\n        return 1\n\n\n"
                "class Other:\n    def fetch(self):\n        return 2\n"
            ),
            "w.py": (
                "def make():\n    return None\n\n\n"
                "class W:\n"
                "    def __init__(self):\n"
                "        self.c = make()\n\n"
                "    def f(self):\n"
                "        return self.c.fetch()\n"
            ),
        }
    )
    rows = _rows(root, "api.py::Api.fetch", capsys)
    assert rows[("w.py", 10)] == sanity.CAUSE_UNTYPED_FIELD


def test_interface_member_without_implementor_cause(
    make_mapped_repo: RepoFactory, capsys: pytest.CaptureFixture
) -> None:
    # A TS interface's methods aren't symbols, and no class implements
    # ``Store``: the call stays ambiguous between the two ``save``s.
    root = make_mapped_repo(
        {
            "store.ts": "export interface Store { save(): void }\n",
            "repo.ts": "export class Repo { save() {} }\n",
            "other.ts": "export class Other { save() {} }\n",
            "w.ts": (
                "import { Store } from './store';\n"
                "export class W {\n"
                "  constructor(private store: Store) {}\n"
                "  f() { this.store.save(); }\n"
                "}\n"
            ),
        }
    )
    rows = _rows(root, "repo.ts::Repo.save", capsys)
    assert rows[("w.ts", 4)] == sanity.CAUSE_INTERFACE_MEMBER_UNIMPLEMENTED


def test_classify_miss_prefers_field_state_over_qualified_call() -> None:
    def cause(state: str | None) -> str:
        return sanity.classify_miss(
            '  f() { this.client.get("x"); }',
            "get",
            is_test_file=False,
            unsupported_language=False,
            tests_excluded=False,
            receiver_field_state=state,
        )

    assert cause(None) == sanity.CAUSE_QUALIFIED_CALL
    assert cause("foreign") == sanity.CAUSE_FOREIGN_RECEIVER_TYPE
    assert cause("untyped") == sanity.CAUSE_UNTYPED_FIELD
    assert cause("interface") == sanity.CAUSE_INTERFACE_MEMBER_UNIMPLEMENTED
