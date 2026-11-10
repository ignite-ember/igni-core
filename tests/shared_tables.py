"""The tables the extensions write into, as a test wires them.

Not a test module: no ``test_`` prefix, so pytest collects nothing.

In production the composition root builds these once and hands the
same tables to every session of the pool — one table per process,
not one per session. A test that wires a pool builds them the same
way, so the wiring it exercises is the wiring production runs.
"""

from __future__ import annotations

from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.fragments import PromptFragments
from igni_core.core.prompts.loader import PromptLoader
from igni_core.extensions.registry import ExtensionRegistry
from igni_core.extensions.rpc import ExtensionRpc


class SharedTables:
    """The process's tables: what the extensions did to the prompts,
    the anchors they fill, what their tools say they are, and the
    namespaced calls they answer."""

    def __init__(self, extensions: ExtensionRpc | None = None) -> None:
        self.contributions = PromptContributions()
        self.prompt_loader = PromptLoader(PromptFragments())
        self.tool_hints = ToolHintsRegistry()
        self.extensions = (
            extensions if extensions is not None else ExtensionRpc(ExtensionRegistry())
        )
