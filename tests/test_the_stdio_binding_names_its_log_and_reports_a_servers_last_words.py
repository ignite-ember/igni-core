"""The stdio binding: where a server's stderr goes, and what is read back from it.

Real stdio servers, opened through the real :class:`StdioMCPBinding`.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest
from agno.tools.function import FunctionCall

from igni_core.core.mcp.config import MCPServerConfig
from igni_core.core.mcp.stdio_binding import StdioMCPBinding

pytestmark = pytest.mark.asyncio

PRELUDE = """\
import os, sys
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
"""

SAY_THEN_SERVE = (
    PRELUDE
    + """
for line in os.environ.get("SAY", "").split("|"):
    print(line, file=sys.stderr, flush=True)
server = MCPServer("said")

@server.tool()
def hello(x: str) -> str:
    "Greets."
    return "hello " + x

@server.tool()
def boom() -> str:
    "Always fails."
    raise ToolError("the ticket is gone")

@server.tool()
def where() -> str:
    "Reports the working directory and a variable."
    return os.getcwd() + "|" + os.environ.get("PROBE", "unset")

server.run()
"""
)


def _config(tmp_path: Path, script: str = SAY_THEN_SERVE, **kwargs: object) -> MCPServerConfig:
    path = tmp_path / "server.py"
    path.write_text(script)
    return MCPServerConfig(name="said", command=sys.executable, args=[str(path)], **kwargs)  # type: ignore[arg-type]


async def _close(tools: object) -> None:
    await tools._context.aclose()  # type: ignore[attr-defined]


async def test_the_default_log_is_igni_s_own_file_in_the_system_temp_dir():
    assert (
        StdioMCPBinding.default_errlog_path() == Path(tempfile.gettempdir()) / "igni_mcp_stderr.log"
    )


async def test_a_server_never_opened_has_no_tail(tmp_path):
    binding = StdioMCPBinding(tmp_path / "log")
    assert binding.errlog_path == tmp_path / "log"
    assert binding.stderr_tail("said") == ""


async def test_the_tail_is_the_last_lines_the_latest_process_wrote(tmp_path):
    log = tmp_path / "log"
    log.write_text("from an earlier process\n")
    binding = StdioMCPBinding(log)
    config = _config(tmp_path, env={"SAY": "one|two||   |three|four"})
    tools = await binding.open("said", config)
    try:
        assert binding.stderr_tail("said") == "one | two | three | four"
        assert binding.stderr_tail("said", lines=2) == "three | four"
        assert binding.stderr_tail("said", max_chars=9) == "ee | four"
    finally:
        await _close(tools)


async def test_a_server_that_wrote_nothing_has_an_empty_tail(tmp_path):
    binding = StdioMCPBinding(tmp_path / "log")
    tools = await binding.open("said", _config(tmp_path, env={"SAY": ""}))
    try:
        assert binding.stderr_tail("said") == ""
    finally:
        await _close(tools)


async def test_a_restarted_server_is_read_from_where_its_new_process_began(tmp_path):
    binding = StdioMCPBinding(tmp_path / "log")
    first = await binding.open("said", _config(tmp_path, env={"SAY": "first life"}))
    await _close(first)
    assert binding.stderr_tail("said") == "first life"

    second = await binding.open("said", _config(tmp_path, env={"SAY": "second life"}))
    try:
        assert binding.stderr_tail("said") == "second life"
    finally:
        await _close(second)


async def test_bytes_that_are_not_utf8_are_replaced_and_a_missing_log_reads_empty(tmp_path):
    log = tmp_path / "log"
    binding = StdioMCPBinding(log)
    tools = await binding.open("said", _config(tmp_path, env={"SAY": "ok"}))
    try:
        with open(log, "ab") as handle:
            handle.write(b"bad \xff byte\n")
        assert binding.stderr_tail("said") == "ok | bad � byte"
        log.unlink()
        assert binding.stderr_tail("said") == ""
    finally:
        await _close(tools)


async def test_the_tools_are_registered_under_the_servers_prefix(tmp_path):
    binding = StdioMCPBinding(tmp_path / "log")
    tools = await binding.open("said", _config(tmp_path, env={"SAY": "x"}))
    try:
        assert sorted(tools.functions) == ["mcp_said_boom", "mcp_said_hello", "mcp_said_where"]
        call = FunctionCall(function=tools.functions["mcp_said_hello"], arguments={"x": "you"})
        await call.aexecute()
        assert "hello you" in str(call.result)
    finally:
        await _close(tools)


async def test_a_tool_call_that_fails_raises_instead_of_returning_agnos_repr_text(tmp_path):
    binding = StdioMCPBinding(tmp_path / "log")
    tools = await binding.open("said", _config(tmp_path, env={"SAY": "x"}))
    try:
        call = FunctionCall(function=tools.functions["mcp_said_boom"], arguments={})
        await call.aexecute()
        assert call.error is not None
        assert "the ticket is gone" in call.error
        assert "TextContent(" not in call.error
    finally:
        await _close(tools)


async def test_a_servers_directory_and_environment_reach_the_process(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    binding = StdioMCPBinding(tmp_path / "log")
    config = _config(tmp_path, env={"SAY": "x", "PROBE": "seen"}, cwd=str(work))
    tools = await binding.open("said", config)
    try:
        call = FunctionCall(function=tools.functions["mcp_said_where"], arguments={})
        await call.aexecute()
        assert str(work.resolve()) in str(call.result)
        assert "|seen" in str(call.result)
    finally:
        await _close(tools)


async def test_a_command_that_cannot_start_raises_to_the_opener(tmp_path):
    binding = StdioMCPBinding(tmp_path / "log")
    config = MCPServerConfig(name="gone", command=str(tmp_path / "no-such-command"))
    with pytest.raises(OSError):
        await binding.open("gone", config)


async def test_a_server_that_exits_at_start_up_opens_with_no_tools_and_its_last_words_are_in_the_log(
    tmp_path,
):
    binding = StdioMCPBinding(tmp_path / "log")
    script = "import sys\nprint('config file missing', file=sys.stderr)\nsys.exit(3)\n"
    tools = await binding.open("crashy", _config(tmp_path, script))
    try:
        assert not tools.functions
        assert binding.stderr_tail("crashy") == "config file missing"
    finally:
        await _close(tools)


async def test_an_initialisation_that_fails_closes_the_connection_and_raises(tmp_path, monkeypatch):
    async def broken(self: object) -> None:
        raise RuntimeError("initialisation failed")

    monkeypatch.setattr("agno.tools.mcp.MCPTools.initialize", broken)
    binding = StdioMCPBinding(tmp_path / "log")

    with pytest.raises(RuntimeError, match="initialisation failed"):
        await binding.open("said", _config(tmp_path, env={"SAY": "x"}))

    owners = [t for t in asyncio.all_tasks() if t.get_name() == "mcp-stdio:said"]
    assert all(t.done() for t in owners)
