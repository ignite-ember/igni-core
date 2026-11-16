"""Every session of a backend is offered the extensions it runs.

An extension runs once per backend, connected on the boot session's MCP
manager. A session a window opened with ``sessions.create()`` had its own
manager, so it got none of the extensions' tools (campaign R301, R295,
R276). A session's manager now offers the extension clients connected on
the boot session's, which keeps connecting and disconnecting them.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.mcp.config import MCPPolicy, MCPServerConfig


def _manager(tmp_path: Path) -> MCPClientManager:
    return MCPClientManager(project_dir=tmp_path, policy=MCPPolicy())


def _owner(tmp_path: Path) -> tuple[MCPClientManager, object, object]:
    owner = _manager(tmp_path / "boot")
    owner.register_server(MCPServerConfig(name="ext:fast", command="x", source="extension"))
    owner.register_server(MCPServerConfig(name="userserver", command="x"))
    ext_client, user_client = object(), object()
    owner._clients = {"ext:fast": ext_client, "userserver": user_client}  # type: ignore[dict-item]
    return owner, ext_client, user_client


def test_a_session_is_offered_the_extensions_and_nothing_else(tmp_path: Path) -> None:
    owner, ext_client, _ = _owner(tmp_path)
    session = _manager(tmp_path / "created")

    session.share_extensions_of(owner)

    assert session.list_connected() == ["ext:fast"]
    assert session.get_client("ext:fast") is ext_client
    assert session.get_client("userserver") is None
    assert session.all_clients() == {"ext:fast": ext_client}


def test_without_an_owner_nothing_is_shared(tmp_path: Path) -> None:
    _owner(tmp_path)
    assert _manager(tmp_path / "created").list_connected() == []


async def test_the_session_never_disconnects_what_it_was_offered(tmp_path: Path) -> None:
    owner, ext_client, _ = _owner(tmp_path)
    session = _manager(tmp_path / "created")
    session.share_extensions_of(owner)

    await session.disconnect_all()

    assert owner.get_client("ext:fast") is ext_client


def test_an_extension_the_owner_disconnects_is_gone_from_the_session(tmp_path: Path) -> None:
    owner, _, _ = _owner(tmp_path)
    session = _manager(tmp_path / "created")
    session.share_extensions_of(owner)

    del owner._clients["ext:fast"]

    assert session.list_connected() == []
