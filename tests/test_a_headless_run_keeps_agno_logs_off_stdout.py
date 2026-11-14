"""``igni -m`` / ``igni -p``: stdout is the reply, agno's logs go to stderr.

Agno's loggers carry their own stdout handler, so a model that named a tool
the run did not offer (the memory write-back naming ``save_file`` in a
--read-only run) printed ``ERROR   Function save_file not found`` above the
reply, and a failed turn printed agno's "Error in Agent run" there too.
"""

from __future__ import annotations

import logging

import pytest
from agno.utils.functions import get_function_call
from click.testing import CliRunner

import igni_core.cli as cli_module
from igni_core.cli.headless_run import HeadlessRun
from igni_core.cli.logging import AgnoLogs


@pytest.fixture
def agno_handlers_put_back():
    saved = {name: list(logging.getLogger(name).handlers) for name in AgnoLogs.LOGGERS}
    yield
    for name, handlers in saved.items():
        logging.getLogger(name).handlers = handlers


def test_a_headless_run_sends_agnos_unknown_tool_error_to_stderr(
    agno_handlers_put_back, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    def run(_self, coro, **_kw):
        coro.close()
        assert get_function_call("save_file", "{}", functions={}) is None

    monkeypatch.setattr(HeadlessRun, "run", run)
    result = CliRunner().invoke(cli_module.cli, ["-m", "hello"])
    assert result.exit_code == 0, result.output

    captured = capfd.readouterr()
    assert "Function save_file not found" not in captured.out
    assert "Function save_file not found" not in result.stdout
    assert "Function save_file not found" in captured.err + result.stderr


def test_every_agno_logger_writes_to_stderr(agno_handlers_put_back) -> None:
    import sys

    AgnoLogs.to_stderr()

    for name in AgnoLogs.LOGGERS:
        (handler,) = logging.getLogger(name).handlers
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stderr
