"""Agent Pool — a thin cache over :class:`AgentEntry` s.

Two-phase lifecycle:

1. :meth:`AgentPool.load_definitions` — parse .md files, resolve
   priorities. Delegates to :class:`AgentDefinitionLoader`.
2. :meth:`AgentPool.build_agents` — construct :class:`Agent`
   objects. Delegates to :class:`AgentBuilder`.

Agents are built lazily on first :meth:`get`, so startup only
pays the cost of parsing ``.md`` files (~50ms), not importing
LLM provider modules (~350ms). Ephemeral CRUD lives on
:class:`EphemeralAgentStore` and is reached via :attr:`ephemeral`.
What extensions do to the loaded agents is
:class:`AgentContributionApplier`'s job; the prompt catalog and the
tool check read the pool's definitions from their own modules.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.core.agents.builder import AgentBuilder
from igni_core.core.agents.ephemeral import EphemeralAgentStore
from igni_core.core.agents.loader import AgentDefinitionLoader
from igni_core.core.agents.plugin_policy import PluginRestrictionPolicy
from igni_core.core.agents.schemas import (
    AgentBuildContext,
    AgentDefinition,
    AgentEntry,
    AgentPriority,
    DbHandle,
    LoadReport,
    McpClient,
)
from igni_core.core.broadcast_schema import BroadcastSink
from igni_core.core.config.settings import Settings

if TYPE_CHECKING:
    from agno.agent import Agent


class AgentPool:
    """Cache + orchestrator for the agent definitions and their
    built :class:`Agent` instances.

    Discovery, build, and ephemeral CRUD are delegated to the three
    collaborators (:class:`AgentDefinitionLoader`,
    :class:`AgentBuilder`, :class:`EphemeralAgentStore`). Everything a
    build needs (settings, base dir, MCP clients, DB, broadcast) is
    one :class:`AgentBuildContext`.

    A name nobody defines is an expected miss, not an error: the
    lookups return ``None`` and :meth:`missing` words the miss for
    whoever has to report it.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        project_dir: Path | None = None,
        db: DbHandle | None = None,
        broadcast: BroadcastSink | None = None,
    ) -> None:
        self._entries: dict[str, AgentEntry] = {}
        self._agents: dict[str, Agent] = {}
        self._builder: AgentBuilder | None = None
        self._ephemeral: EphemeralAgentStore | None = None
        # ``settings`` may be ``None`` until :meth:`load_definitions`;
        # :meth:`_settings` falls back to defaults for the
        # single-directory loads that can run before it.
        self._context = AgentBuildContext(
            settings=settings,
            base_dir=str(project_dir) if project_dir is not None else None,
            db=db,
            broadcast=broadcast,
        )

    # ── Entries ──────────────────────────────────────────────────

    def iter_entries(self) -> Iterable[AgentEntry]:
        """Iterate the current :class:`AgentEntry` values."""
        return self._entries.values()

    def get_entry(self, name: str) -> AgentEntry | None:
        return self._entries.get(name)

    def missing(self, name: str) -> str:
        """The message for a lookup of ``name`` that found nothing."""
        available = ", ".join(sorted(self._entries))
        return f"Agent '{name}' not found. Available: {available}"

    def upsert_entry(self, entry: AgentEntry) -> None:
        """Insert ``entry`` if new, or overwrite the existing
        same-name entry regardless of priority.

        Used by :class:`EphemeralAgentStore` to (re)register
        ephemerals and re-tag them after a ``promote()``, and by
        :class:`AgentContributionApplier` for what extensions did.
        """
        self._entries[entry.definition.name] = entry
        # Invalidate the cached Agent so the next ``get()`` rebuilds
        # with the new definition.
        self._agents.pop(entry.definition.name, None)

    def remove(self, name: str) -> None:
        """Drop an entry (and its cached agent) from the pool."""
        self._entries.pop(name, None)
        self._agents.pop(name, None)

    # ── Phase 1: load ────────────────────────────────────────────

    def load_definitions(
        self,
        settings: Settings,
        project_dir: Path | None = None,
        group_dir: Path | None = None,
    ) -> LoadReport:
        """Parse all agent ``.md`` files and resolve priorities.

        No :class:`Agent` objects are created — just data. Returns
        the :class:`LoadReport` so callers can surface parse
        errors (``report.errors``) to the FE / audit log.
        """
        if project_dir is None:
            project_dir = Path.cwd()
        self._context = self._context.model_copy(
            update={"settings": settings, "base_dir": str(project_dir)}
        )
        loader = AgentDefinitionLoader(
            settings=settings,
            project_dir=project_dir,
            group_dir=group_dir,
        )
        report = loader.load()
        self._merge(report)
        return report

    def load_directory(
        self,
        path: Path,
        priority: AgentPriority | int,
        *,
        namespace: str | None = None,
        restriction_policy: PluginRestrictionPolicy | None = None,
    ) -> LoadReport:
        """Merge the agents of one directory at ``priority``.

        ``restriction_policy`` is applied as-is: ``None`` means no
        plugin envelope. :meth:`load_plugin_directory` is the call that
        picks the strict one for plugins; the ephemeral store rehydrates
        through here at :attr:`AgentPriority.EPHEMERAL`.
        """
        base_dir = self._context.base_dir
        loader = AgentDefinitionLoader(
            settings=self._settings(),
            project_dir=Path(base_dir) if base_dir else Path.cwd(),
            restriction_policy=restriction_policy,
        )
        report = loader.load_directory(path, priority, namespace=namespace)
        self._merge(report)
        return report

    def load_plugin_directory(
        self,
        path: Path,
        priority: AgentPriority | int,
        namespace: str,
        restriction_policy: PluginRestrictionPolicy | None = None,
    ) -> LoadReport:
        """Load a plugin's agent directory under the plugin
        security envelope.

        Applies ``restriction_policy`` (strict by default — see
        :class:`PluginRestrictionPolicy`) and namespaces every
        loaded agent as ``<namespace>:<name>``. Used by
        :class:`PluginLoader`."""
        return self.load_directory(
            path,
            priority,
            namespace=namespace,
            restriction_policy=restriction_policy or PluginRestrictionPolicy.strict(),
        )

    # ── Phase 2: build ───────────────────────────────────────────

    def build_agents(self, mcp_clients: dict[str, McpClient] | None = None) -> None:
        """Construct :class:`Agent` objects lazily from all loaded
        definitions.

        Stores ``mcp_clients`` in the build context so the next
        :meth:`get` rebuilds against the current MCP set.
        """
        assert self._context.settings is not None, "Call load_definitions() first"
        self._context = self._context.model_copy(update={"mcp_clients": mcp_clients})
        if self._builder is None:
            self._builder = AgentBuilder(self._context)
        else:
            self._builder.replace_context(self._context)
        self._agents.clear()

    # ── Access ────────────────────────────────────────────────────

    def get(self, name: str) -> Agent | None:
        """The agent called ``name``, built on first use; ``None`` when
        no definition has that name."""
        if name not in self._agents:
            entry = self._entries.get(name)
            if entry is None:
                return None
            self._agents[name] = self._ensure_builder().build(entry.definition)
        return self._agents[name]

    def list_agents(self) -> list[AgentDefinition]:
        return [entry.definition for entry in self._entries.values()]

    @property
    def db(self) -> DbHandle | None:
        """The Agno DB handle threaded through the pool. Used by
        :class:`SpawnRunner` when it builds sub-teams so paused runs
        land in the same store as the parent."""
        return self._context.db

    # ── Ephemeral store ──────────────────────────────────────────

    @property
    def ephemeral(self) -> EphemeralAgentStore:
        """Access the ephemeral store. Raises if not initialised."""
        if self._ephemeral is None:
            raise RuntimeError("Ephemeral agents not initialized. Call init_ephemeral() first.")
        return self._ephemeral

    @property
    def has_ephemeral(self) -> bool:
        """Whether :meth:`init_ephemeral` has run (it does only when
        ``orchestration.generate_ephemeral`` is on)."""
        return self._ephemeral is not None

    def init_ephemeral(
        self,
        project_dir: Path,
        max_ephemeral: int = EphemeralAgentStore.DEFAULT_MAX_EPHEMERAL,
    ) -> None:
        """Set up the ephemeral store + rehydrate leftovers."""
        self._ephemeral = EphemeralAgentStore(
            project_dir=project_dir,
            pool=self,
            max_ephemeral=max_ephemeral,
        )
        self._ephemeral.init()

    # ── Internals ────────────────────────────────────────────────

    def _merge(self, report: LoadReport) -> None:
        """Merge a :class:`LoadReport` into ``_entries`` honouring
        priority."""
        for name, entry in report.entries.items():
            existing = self._entries.get(name)
            if existing is None or entry.priority > existing.priority:
                self._entries[name] = entry

    def _ensure_builder(self) -> AgentBuilder:
        """Return the builder, constructing one from the current
        context if ``build_agents`` was never called."""
        if self._builder is None:
            self._builder = AgentBuilder(
                self._context.model_copy(update={"settings": self._settings()})
            )
        return self._builder

    def _settings(self) -> Settings:
        """The settings in the build context, or defaults.

        Defaults serve only a pool that was given no settings and
        never had ``load_definitions`` called on it — in production
        the session hands the pool its settings before anything else.
        """
        settings = self._context.settings
        return settings if settings is not None else Settings()


__all__ = ["AgentPool"]
