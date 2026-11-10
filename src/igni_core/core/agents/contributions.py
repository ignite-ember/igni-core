"""Every agent as extensions have shaped it: overridden, edited, or both."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.core.agents.markdown import AgentMarkdownFile
from igni_core.core.agents.plugin_policy import PluginRestrictionPolicy
from igni_core.core.agents.schemas import AgentDefinition, AgentEntry, AgentPriority
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.loader import PromptLoader

if TYPE_CHECKING:
    from igni_core.core.agents.pool import AgentPool

logger = logging.getLogger(__name__)


class AgentContributionApplier:
    """Applies what extensions contribute to agents onto a loaded pool.

    Run once everything is loaded — igni's agents, the group's, the
    project's and the plugins' — because an edit applies to whichever
    definition won. An override stands at
    :attr:`AgentPriority.EXTENSION_OVERRIDE`, above every file a person
    or an organisation wrote, and defines the agent even where igni has
    none; the moment the extension stops, or its condition stops
    holding, it is gone from the table and the agent is whatever it was
    before.

    Text an extension wrote for an agent's header gets the plugin
    security envelope a plugin's own agents get.
    """

    def __init__(self, contributions: PromptContributions, *, prompt_loader: PromptLoader) -> None:
        self._contributions = contributions
        self._prompt_loader = prompt_loader

    def apply_to(self, pool: AgentPool) -> None:
        for name in self._contributions.agents():
            entry = self._shaped(name, pool.get_entry(name))
            if entry is not None:
                pool.upsert_entry(entry)

    def _shaped(self, name: str, entry: AgentEntry | None) -> AgentEntry | None:
        """The entry ``name`` becomes, or ``None`` when it stays as it is."""
        source = entry.definition.source_path if entry is not None else None
        try:
            composition = self._contributions.compose(f"agent:{name}", self._read(name, source))
        except ValueError as exc:
            logger.warning("agent %s: what extensions do to it does not fit: %s", name, exc)
            return None
        if composition is None:
            logger.info("agent %s: edited by an extension, and nobody defines it", name)
            return None
        path = composition.override_path or source or Path(f"{name}.md")
        md = AgentMarkdownFile(
            path, content=composition.document.render(), prompt_loader=self._prompt_loader
        )
        try:
            definition: AgentDefinition = md.parse().model_copy(update={"name": name})
        except ValueError as exc:
            logger.warning("agent %s, as extensions shaped it, does not load: %s", name, exc)
            return None
        if composition.frontmatter_owner is not None:
            definition = PluginRestrictionPolicy.strict().apply(
                definition, md.raw_frontmatter_keys(), plugin_name=composition.frontmatter_owner
            )
        priority = (
            AgentPriority.EXTENSION_OVERRIDE
            if composition.override_owner is not None or entry is None
            else entry.priority
        )
        return AgentEntry(definition=definition, priority=priority)

    @staticmethod
    def _read(name: str, source: Path | None) -> str | None:
        if source is None:
            return None
        try:
            return source.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("agent %s: %s could not be read: %s", name, source, exc)
            return None


__all__ = ["AgentContributionApplier"]
