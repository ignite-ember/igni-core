"""Whose tool is a registered tool name, and what its manifest calls it.

An extension's tools are registered under a name of igni's own making —
``mcp_ext:acme.tickets_lookup_ticket`` — so two extensions cannot claim
the same one. The manifest declares them its own way:
``acme.tickets.lookup_ticket``. Both names travel: the registered one is
what igni's own hooks, permission rules and transcripts use, so a matcher
keeps matching, and the manifest's is what ``permissions.tools``
declares.

The rules, not the state: the installed extensions are read from the
registry this is given, and it holds it rather than borrowing it on
every call.
"""

from __future__ import annotations

from pydantic import BaseModel

from igni_core.core.mcp.naming import tool_name_prefix
from igni_core.extensions.installer import server_name
from igni_core.extensions.registry import ExtensionRegistry


class ExtensionTools:
    """Whose tool a registered name is, and what its manifest calls it."""

    def __init__(self, registry: ExtensionRegistry) -> None:
        """
        Args:
            registry: The installed extensions whose tools these are. A
                name is read against them to find whose it is.
        """
        self._registry = registry

    def owner(self, tool_name: str) -> str | None:
        """The extension a registered tool name is from: ``mcp_ext:<id>_<tool>``."""
        owners = [
            extension_id
            for extension_id in self._registry.ids()
            if tool_name.startswith(f"{tool_name_prefix(server_name(extension_id))}_")
        ]
        # The longest id: ``acme.a`` and ``acme.a_b`` both prefix ``mcp_ext:acme.a_b_x``.
        return max(owners, key=len) if owners else None

    def manifest_name(self, tool_name: str) -> str:
        """An extension's tool as its manifest names it, or ``""``.

        ``mcp_ext:acme.tickets_lookup_ticket`` → ``acme.tickets.lookup_ticket``:
        the form ``permissions.tools`` declares. A tool that is no
        extension's has no manifest name.
        """
        extension_id = self.owner(tool_name)
        if extension_id is None:
            return ""
        prefix = f"{tool_name_prefix(server_name(extension_id))}_"
        return f"{extension_id}.{tool_name[len(prefix) :]}"

    def of(self, value: BaseModel) -> str | None:
        """The manifest name of the tool the value carries, if it carries one."""
        tool_name = getattr(value, "tool_name", None)
        if not isinstance(tool_name, str):
            return None
        return self.manifest_name(tool_name)


__all__ = ["ExtensionTools"]
