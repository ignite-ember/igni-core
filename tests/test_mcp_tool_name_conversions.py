"""How an MCP tool is named once registered, and how the name is read back.

``mcp_<server>_<tool>`` is the registered name; the server's own name for
the tool is the bare one. Everything comparing the two (the permission
check, a hook call, the panel) converts through these three functions.
"""

from __future__ import annotations

import pytest

from igni_core.core.mcp.naming import bare_tool_name, registered_tool_name, tool_name_prefix


def test_the_prefix_is_mcp_then_the_server_name() -> None:
    assert tool_name_prefix("filesystem") == "mcp_filesystem"
    assert tool_name_prefix("ext:acme.scanner") == "mcp_ext:acme.scanner"


def test_a_bare_name_is_registered_under_the_prefix_and_separator() -> None:
    assert registered_tool_name("fs", "read_file") == "mcp_fs_read_file"
    assert registered_tool_name("ext:acme.scanner", "list") == "mcp_ext:acme.scanner_list"


def test_a_name_already_registered_is_not_prefixed_twice() -> None:
    once = registered_tool_name("fs", "read_file")
    assert registered_tool_name("fs", once) == once


def test_a_registered_name_is_read_back_as_the_servers_own() -> None:
    assert bare_tool_name("fs", "mcp_fs_read_file") == "read_file"
    assert bare_tool_name("ext:acme.scanner", "mcp_ext:acme.scanner_list") == "list"


def test_a_bare_name_and_another_servers_name_come_back_unchanged() -> None:
    assert bare_tool_name("fs", "read_file") == "read_file"
    assert bare_tool_name("fs", "mcp_other_read_file") == "mcp_other_read_file"


def test_a_server_whose_name_starts_another_is_not_mistaken_for_it() -> None:
    # The trailing separator is what tells ``a`` from ``ab``.
    assert bare_tool_name("a", "mcp_ab_tool") == "mcp_ab_tool"
    assert registered_tool_name("a", "mcp_ab_tool") == "mcp_a_mcp_ab_tool"


@pytest.mark.parametrize("server", ["fs", "ext:acme.scanner", "a_b"])
@pytest.mark.parametrize("tool", ["read_file", "x", "with_underscores_in_it"])
def test_registering_and_reading_back_round_trips(server: str, tool: str) -> None:
    assert bare_tool_name(server, registered_tool_name(server, tool)) == tool


def test_an_empty_tool_name_is_registered_as_just_the_prefix_and_separator() -> None:
    assert registered_tool_name("fs", "") == "mcp_fs_"
    assert bare_tool_name("fs", "mcp_fs_") == ""
