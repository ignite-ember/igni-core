"""Typed readers for the files a plugin directory bundles.

:class:`PluginDirectoryScanner` reads third-party files out of a plugin
checkout: ``hooks/hooks.json``, ``.mcp.json`` and the frontmatter of
``SKILL.md`` / agent markdown. Each shape is validated into a model
here, and each model turns itself into the wire entries
:class:`PluginContents` carries.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from igni_core.backend.plugin_schemas import PluginHookInfo, PluginMCPServerInfo


class MarkdownFrontmatter(BaseModel):
    """The ``key: value`` lines of a leading ``---``-delimited block.

    Keys are lower-cased; the first occurrence of a key wins. Values are
    stripped of surrounding whitespace and double quotes. Read line by
    line rather than as YAML so an unquoted description containing a
    colon still yields its text.
    """

    model_config = ConfigDict(frozen=True)

    values: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def parse(cls, md_text: str) -> MarkdownFrontmatter:
        """Parse *md_text*; an absent or unterminated block is empty."""
        if not md_text.startswith("---"):
            return cls()
        end = md_text.find("\n---", 4)
        if end <= 0:
            return cls()
        values: dict[str, str] = {}
        for line in md_text[4:end].splitlines():
            key, sep, value = line.partition(":")
            if sep:
                values.setdefault(key.lower(), value.strip().strip('"'))
        return cls(values=values)

    def value(self, key: str) -> str:
        """The value for *key* (case-insensitive), or ``""``."""
        return self.values.get(key.lower(), "")


class PluginHooksFile(BaseModel):
    """``hooks/hooks.json``: handler lists keyed by event name."""

    hooks: dict[str, object] | None = None

    def hook_infos(self) -> list[PluginHookInfo]:
        """One entry per event whose handlers are a list; other values
        are not handler lists and are skipped."""
        return [
            PluginHookInfo(event=event, count=len(handlers))
            for event, handlers in (self.hooks or {}).items()
            if isinstance(handlers, list)
        ]


class PluginMCPServerEntry(BaseModel):
    """One server under ``mcpServers`` in ``.mcp.json``."""

    type: str | None = None
    command: str | None = None
    url: str | None = None

    def to_info(self, name: str) -> PluginMCPServerInfo:
        """The wire entry: stdio unless a transport is named, and the
        command (or URL for remote servers) as the launch target."""
        return PluginMCPServerInfo(
            name=name,
            transport=self.type or "stdio",
            command=self.command or self.url or "",
        )


class PluginMCPFile(BaseModel):
    """``.mcp.json`` / ``mcp.json``: the ``mcpServers`` envelope."""

    model_config = ConfigDict(populate_by_name=True)

    mcp_servers: dict[str, PluginMCPServerEntry] | None = Field(default=None, alias="mcpServers")

    def server_infos(self) -> list[PluginMCPServerInfo]:
        """One wire entry per declared server, in file order."""
        return [entry.to_info(name) for name, entry in (self.mcp_servers or {}).items()]
