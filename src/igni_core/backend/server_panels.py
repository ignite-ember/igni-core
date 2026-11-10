"""Panel-details RPCs — thin composition facade.

Historically this module was a single :class:`PanelsController`
mixing five distinct concerns (agents, hooks, skills, slash
commands, output styles). The concerns have been split into
focused per-panel controllers under
:mod:`igni_core.backend.panels`; this module now only holds the
composition facade + backward-compatible re-exports so callers
can keep using ``BackendServer.panels.<x>()`` and the RPC router
unchanged.

Wire types (`OutputStylesResult`, `HookEntryView`,
`SlashCommandEntry`, `PromoteEphemeralResult`,
`DiscardEphemeralResult`) live in :mod:`schemas_panels`. Public
symbols are re-exported here for external callers whose imports
predate the split.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Re-exports so existing imports (``from igni_core.backend.server_panels
# import OutputStylesResult``) keep working across the split.
from igni_core.backend.panels import (
    AgentsPanelController,
    HooksPanelController,
    OutputStylesCatalog,
    SkillsPanelController,
    SlashCommandsCatalog,
)
from igni_core.backend.schemas_panels import (  # noqa: F401 — public re-export
    DiscardEphemeralResult,
    HookEntryView,
    OutputStyleInfo,
    OutputStylesResult,
    PromoteEphemeralResult,
    SlashCommandEntry,
)
from igni_core.backend.schemas_rpc import (
    GroupPolicyPackResult,
)
from igni_core.core.agents import AgentInfo
from igni_core.core.config.group_policy.cache import GroupPolicyCache
from igni_core.core.skills import SkillPool
from igni_core.core.skills.parser import SkillInfo
from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.core.session import Session


class PanelsController:
    """Composition facade — one member per panel controller.

    Preserves the ``backend.panels.<method>()`` call shape used by
    :class:`BackendServer` and the RPC router. Each method is a
    one-line delegate into the appropriate per-panel controller
    (see :mod:`igni_core.backend.panels`), which is where the
    real logic lives now.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._agents = AgentsPanelController(session)
        self._hooks = HooksPanelController(session)
        self._skills = SkillsPanelController(session)
        self._slash_commands = SlashCommandsCatalog(session)
        self._output_styles = OutputStylesCatalog(session)

    # ── Direct access to per-panel controllers ───────────────────

    @property
    def agents(self) -> AgentsPanelController:
        return self._agents

    @property
    def hooks(self) -> HooksPanelController:
        return self._hooks

    @property
    def skills(self) -> SkillsPanelController:
        return self._skills

    @property
    def slash_commands_catalog(self) -> SlashCommandsCatalog:
        return self._slash_commands

    @property
    def output_styles_catalog(self) -> OutputStylesCatalog:
        return self._output_styles

    # ── Flat delegators (stable BackendServer.panels.x() shape) ──

    def agent_details(self) -> list[AgentInfo]:
        return self._agents.snapshot()

    def promote_ephemeral_agent(self, name: str, target: str = "project") -> PromoteEphemeralResult:
        return self._agents.promote(name, target)

    def discard_ephemeral_agent(self, name: str) -> DiscardEphemeralResult:
        return self._agents.discard(name)

    def hooks_details(self) -> list[HookEntryView]:
        return self._hooks.snapshot()

    def reload_hooks(self) -> msg.Info:
        return self._hooks.reload()

    def skill_details(self) -> list[SkillInfo]:
        return self._skills.snapshot()

    def skill_pool(self) -> SkillPool:
        return self._skills.pool()

    def skill_names(self) -> list[str]:
        return self._skills.names()

    def slash_commands(self) -> list[SlashCommandEntry]:
        return self._slash_commands.entries()

    def output_styles(self) -> OutputStylesResult:
        return self._output_styles.snapshot()

    def group_policy(self) -> GroupPolicyPackResult:
        """The group this person is in.

        No longer carries pending conflicts, because there are none to
        have: the loaders read the pack from the policy cache and a
        same-named project file simply outranks it, so nothing is copied
        anywhere and nothing can diverge.
        """
        data_dir = self._session.settings.storage.data_dir
        meta = GroupPolicyCache.for_data_dir(data_dir).meta.load()
        if meta is None:
            return GroupPolicyPackResult()
        return GroupPolicyPackResult(
            group_id=meta.group_id,
            group_name=meta.group_name,
            fetched_at=meta.fetched_at.isoformat() if meta.fetched_at else None,
            entry_count=meta.entry_count,
            default_model=meta.default_model,
        )
