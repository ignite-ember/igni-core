"""Agent discovery, build, and pool management.

Public surface — re-exports the canonical class names so external
callers can ``from igni_core.core.agents import AgentPool`` without
knowing the internal layout. Mirrors the sibling packages
``core/mcp/`` and ``core/plugins/``.
"""

from igni_core.core.agents.builder import AgentBuilder
from igni_core.core.agents.contributions import AgentContributionApplier
from igni_core.core.agents.ephemeral import EphemeralAgentStore
from igni_core.core.agents.loader import AgentDefinitionLoader
from igni_core.core.agents.markdown import AgentMarkdownFile
from igni_core.core.agents.plugin_policy import PluginRestrictionPolicy
from igni_core.core.agents.pool import AgentPool
from igni_core.core.agents.schemas import (
    AgentBuildContext,
    AgentConstructorArgs,
    AgentDefinition,
    AgentEntry,
    AgentInfo,
    AgentPriority,
    LoadError,
    LoadReport,
)

__all__ = [
    "AgentBuildContext",
    "AgentBuilder",
    "AgentContributionApplier",
    "AgentConstructorArgs",
    "AgentDefinition",
    "AgentDefinitionLoader",
    "AgentEntry",
    "AgentInfo",
    "AgentMarkdownFile",
    "AgentPool",
    "AgentPriority",
    "EphemeralAgentStore",
    "LoadError",
    "LoadReport",
    "PluginRestrictionPolicy",
]
