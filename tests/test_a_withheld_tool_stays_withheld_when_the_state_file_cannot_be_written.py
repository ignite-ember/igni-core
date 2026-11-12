"""An extension's undeclared tools are withheld from the agent even when
the project's tool-state file cannot be written.

Switching a tool off now fails when its choice cannot be kept, so the
install path that withholds an extension's undeclared tools must not stop
at the first such failure and leave the rest offered to the model.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.paths import CONFIG_DIR
from igni_core.extensions.install_service import ExtensionConnection, server_name


def test_every_undeclared_tool_is_withheld_though_the_state_file_cannot_be_written(
    tmp_path: Path,
) -> None:
    (tmp_path / CONFIG_DIR).write_text("a file where the folder should be")
    manager = MCPClientManager(project_dir=tmp_path)
    server = server_name("acme.scanner")
    live = SimpleNamespace(
        functions={f"mcp_{server}_{name}": object() for name in ("list", "wipe", "format")}
    )
    manager._clients[server] = live  # type: ignore[assignment]
    manager.get_tools = lambda name: list(live.functions)  # type: ignore[method-assign]
    permissions = SimpleNamespace(
        tools=SimpleNamespace(refused=lambda offered: [t for t in offered if t != "list"])
    )
    connection = ExtensionConnection(
        mcp=manager, rpc=SimpleNamespace(), tool_hints=SimpleNamespace()
    )  # type: ignore[arg-type]

    connection.restrict_tools(SimpleNamespace(id="acme.scanner"), server, permissions)  # type: ignore[arg-type]

    assert list(live.functions) == [f"mcp_{server}_list"]
