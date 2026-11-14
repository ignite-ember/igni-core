"""One handler per kind of entry: where it is written, and what a stale one looks like."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.hooks import HookSet
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.plugins import PluginInstallCoordinator

logger = logging.getLogger(__name__)


class ActiveNames(BaseModel):
    """The entry names each kind wrote this pass — everything else in its directory is stale."""

    by_kind: dict[str, set[str]] = Field(default_factory=dict)

    def add(self, kind: str, name: str) -> None:
        self.by_kind.setdefault(kind, set()).add(name)

    def of(self, kind: str) -> set[str]:
        return self.by_kind.get(kind, set())


class MaterializeContext(BaseModel):
    """What one materialise pass accumulates while each entry is written."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    layout: GroupPolicyLayout
    hooks: HookSet = Field(default_factory=HookSet)
    active: ActiveNames = Field(default_factory=ActiveNames)
    #: A start-up rebuild: no network, so no plugin is cloned.
    rebuild: bool = False


class McpServersFile(BaseModel):
    """The ``{mcpServers: {...}}`` envelope :class:`MCPConfigLoader` reads."""

    mcp_servers: dict[str, JsonValue] = Field(alias="mcpServers")


class EntryKind(ABC):
    """A kind of entry a pack can carry."""

    #: Whether a file of this kind left over from an earlier pack is removed.
    prunes = True
    #: Whether this kind's directory is derived from the pack (and so cleared with it).
    owns_directory = True

    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def directory(self) -> str:
        return self.name

    @abstractmethod
    def write(self, entry: GroupPolicyEntry, ctx: MaterializeContext) -> None: ...

    def is_stale(self, path: Path, active: set[str]) -> bool:
        return False


class PlainFileKind(EntryKind):
    """One entry, one file, at the filename convention its loader expects."""

    def __init__(
        self,
        name: str,
        template: str,
        suffix: str,
        *,
        executable: bool = False,
        model: bool = False,
    ) -> None:
        super().__init__(name)
        self.template = template
        self.suffix = suffix
        #: Written mode 0700. A hook command runs through ``bash -c``; a script
        #: arriving mode 0644 fails with 127, which the hook runner treats as
        #: non-blocking — a silent no-op rather than an error anyone sees.
        self.executable = executable
        #: Agents: the entry's model is written into the frontmatter.
        self.model = model

    def write(self, entry: GroupPolicyEntry, ctx: MaterializeContext) -> None:
        placement = ctx.layout.place(self.directory, self.template, entry)
        if placement.path is None:
            logger.warning("Group policy entry skipped: %s", placement.reason)
            return
        placement.path.parent.mkdir(parents=True, exist_ok=True)
        placement.path.write_text(self.content(entry), encoding="utf-8")
        if self.executable:
            # Not 0o755: nothing else on the machine needs to run code that
            # arrived over the network.
            placement.path.chmod(0o700)
        ctx.active.add(self.name, entry.safe_name().name)

    def content(self, entry: GroupPolicyEntry) -> str:
        return entry.rendered_content() if self.model else entry.content

    def is_stale(self, path: Path, active: set[str]) -> bool:
        return path.suffix == self.suffix and path.name[: -len(self.suffix)] not in active


class SkillKind(PlainFileKind):
    """A directory with ``SKILL.md`` in it; a stale one goes as a directory."""

    def is_stale(self, path: Path, active: set[str]) -> bool:
        return path.is_dir() and path.name not in active


class McpKind(PlainFileKind):
    """One server definition, wrapped under its entry name in the loader's envelope."""

    def content(self, entry: GroupPolicyEntry) -> str:
        envelope = McpServersFile(mcpServers={entry.entry_name: entry.mcp_server_config()})
        return json.dumps(envelope.model_dump(by_alias=True))


class HooksKind(EntryKind):
    """Declarations keyed by event, merged into one file (:class:`HookSet`)."""

    prunes = False

    def write(self, entry: GroupPolicyEntry, ctx: MaterializeContext) -> None:
        ctx.hooks.add(entry)


class PluginKind(EntryKind):
    """A ``source_url`` entry is installed; one without is its legacy YAML file."""

    def __init__(self, plugins: PluginInstallCoordinator) -> None:
        super().__init__("plugins")
        self._plugins = plugins
        self._legacy = PlainFileKind("plugins", "{name}.yaml", ".yaml")

    def write(self, entry: GroupPolicyEntry, ctx: MaterializeContext) -> None:
        if not entry.source_url:
            self._legacy.write(entry, ctx)
            return
        if not ctx.rebuild:
            self._plugins.install(entry)
        ctx.active.add(self.name, entry.safe_name().name)

    def is_stale(self, path: Path, active: set[str]) -> bool:
        # Installed plugins are directories, pruned by ``prune_planted``.
        return path.suffix == ".yaml" and path.stem not in active


class ExtensionKind(EntryKind):
    """Withdrawn: extensions come from their services through igni server."""

    prunes = False
    owns_directory = False

    def write(self, entry: GroupPolicyEntry, ctx: MaterializeContext) -> None:
        # Said, so an admin whose pack still carries one learns why nothing arrives.
        logger.warning(
            "Group policy entry %r is an extension, and a group policy "
            "no longer installs extensions — they arrive from their "
            "services through igni server. Ignored.",
            entry.entry_name,
        )


class KindRegistry:
    """The kinds a pack can carry, by name. ``settings`` is not one: it is merged, not written."""

    def __init__(self, kinds: list[EntryKind]) -> None:
        self._kinds = {kind.name: kind for kind in kinds}

    @classmethod
    def default(cls, plugins: PluginInstallCoordinator) -> KindRegistry:
        return cls(
            [
                PlainFileKind("agents", "{name}.md", ".md", model=True),
                SkillKind("skills", "{name}/SKILL.md", ""),
                PlainFileKind("commands", "{name}.md", ".md"),
                PlainFileKind("workflows", "{name}.mjs", ".mjs"),
                PlainFileKind("rules", "{name}.md", ".md"),
                PlainFileKind("output-styles", "{name}.md", ".md"),
                PlainFileKind("tools", "{name}.py", ".py"),
                PlainFileKind("scripts", "{name}.sh", ".sh", executable=True),
                McpKind("mcps", "{name}.json", ".json"),
                PluginKind(plugins),
                HooksKind("hooks"),
                ExtensionKind("extensions"),
            ]
        )

    def get(self, name: str) -> EntryKind | None:
        return self._kinds.get(name)

    def pruned(self) -> list[EntryKind]:
        return [kind for kind in self._kinds.values() if kind.prunes]

    def file_directories(self) -> list[str]:
        """Every directory derived from a pack, for clearing it."""
        return sorted(kind.directory for kind in self._kinds.values() if kind.owns_directory)
