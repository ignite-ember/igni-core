"""``dump_wire_schema.py`` and ``export_contracts.py`` publish the contracts.

The currency tests compare the committed files with what the generators
produce; they never run the scripts a maintainer runs (``make contracts``).
So the part nobody checked was the scripts themselves: which files they
write, the line they print for each, and what they do when the wire
contract comes out empty — refuse with the error as the exit message,
rather than exit 0 over a destroyed file.
"""

from __future__ import annotations

import json
import pathlib
import re
import runpy
import subprocess
import sys

import pytest

from igni_core.contracts.wire_schema import EmptyWireContractError, WireSchemaPublisher

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DUMP = _ROOT / "scripts/dump_wire_schema.py"
_EXPORT = _ROOT / "scripts/export_contracts.py"
_DOCS = ["docs/host-schema.json", "docs/rpc-contract.json", "docs/wire-schema.json"]


@pytest.fixture
def docs_restored():
    """The scripts write the committed files; put them back whatever happens."""
    before = {name: (_ROOT / name).read_bytes() for name in _DOCS}
    try:
        yield before
    finally:
        for name, data in before.items():
            (_ROOT / name).write_bytes(data)


def _run(script: pathlib.Path, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(script)], capture_output=True, text=True, cwd=cwd, timeout=180
    )


def _refuse_an_empty_contract(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise EmptyWireContractError("the wire contract is empty: refusing to write it")

    monkeypatch.setattr(WireSchemaPublisher, "write", refuse)


class TestDumpWireSchema:
    def test_it_prints_how_many_messages_and_payloads_it_wrote(self, docs_restored, tmp_path):
        result = _run(_DUMP, tmp_path)

        assert result.returncode == 0, result.stderr
        written = json.loads((_ROOT / "docs/wire-schema.json").read_text())
        expected = (
            f"wrote {_ROOT / 'docs/wire-schema.json'} "
            f"({len(written['messages'])} messages, {len(written['rpc'])} rpc payloads)"
        )
        assert result.stdout.strip() == expected

    def test_an_empty_contract_exits_with_the_reason_and_writes_nothing(
        self, monkeypatch, capsys, docs_restored
    ):
        _refuse_an_empty_contract(monkeypatch)

        with pytest.raises(SystemExit) as exit_info:
            runpy.run_path(str(_DUMP), run_name="__main__")

        assert exit_info.value.code == "the wire contract is empty: refusing to write it"
        assert "wrote" not in capsys.readouterr().out
        assert (_ROOT / "docs/wire-schema.json").read_bytes() == docs_restored["docs/wire-schema.json"]


class TestExportContracts:
    def test_it_writes_the_three_published_files_and_says_so(self, docs_restored, tmp_path):
        result = _run(_EXPORT, tmp_path)

        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 3, lines
        assert lines[0] == f"wrote {_ROOT / 'docs/host-schema.json'}"
        rpc = json.loads((_ROOT / "docs/rpc-contract.json").read_text())
        assert lines[1] == (
            f"wrote {_ROOT / 'docs/rpc-contract.json'} "
            f"({len(rpc['methods'])} methods, {len(rpc['messages'])} messages)"
        )
        assert re.fullmatch(
            re.escape(f"wrote {_ROOT / 'docs/wire-schema.json'} ") + r"\(\d+ messages, \d+ rpc payloads\)",
            lines[2],
        )

    def test_the_files_it_writes_are_the_committed_ones(self, docs_restored, tmp_path):
        """Run from elsewhere, it still writes the same bytes the currency
        tests compare against — `make contracts` leaves no diff."""
        _run(_EXPORT, tmp_path)

        for name in _DOCS:
            assert (_ROOT / name).read_bytes() == docs_restored[name], name

    def test_an_empty_wire_contract_stops_it_after_the_first_two_files(
        self, monkeypatch, capsys, docs_restored
    ):
        _refuse_an_empty_contract(monkeypatch)

        with pytest.raises(SystemExit) as exit_info:
            runpy.run_path(str(_EXPORT), run_name="__main__")

        assert exit_info.value.code == "the wire contract is empty: refusing to write it"
        out = capsys.readouterr().out.splitlines()
        assert [line.split(" (")[0].split("/")[-1] for line in out] == [
            "host-schema.json",
            "rpc-contract.json",
        ]
