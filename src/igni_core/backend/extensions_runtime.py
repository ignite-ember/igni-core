"""The process-level extension objects every session shares."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from igni_core.backend.command_handler import CommandHandler
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.loader import PromptLoader
from igni_core.extensions.contributions import (
    ExtensionConditionReceiver,
    ExtensionContributions,
)
from igni_core.extensions.faults import ExtensionFaults
from igni_core.extensions.health import ExtensionHealth
from igni_core.extensions.impact import IgniCommands
from igni_core.extensions.origin import ExtensionOrigins
from igni_core.extensions.registry import ExtensionRegistry
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.store import ExtensionStore
from igni_core.notifications.center import NotificationCenter
from igni_core.notifications.extension_notices import FaultNotices


class ExtensionsRuntime:
    """One registry, its RPC, the page origins, health and contributions.

    Process-level, not per-session: an extension is installed into a
    deployment, so a second session must not see a different set of them,
    and the boot RPC table is built before the orchestrator exists.
    """

    def __init__(
        self,
        *,
        rpc: ExtensionRpc,
        origins: ExtensionOrigins,
        health: ExtensionHealth,
        contributions: ExtensionContributions,
        prompts: PromptContributions,
    ) -> None:
        self._rpc = rpc
        self._origins = origins
        self._health = health
        self._contributions = contributions
        self._prompts = prompts

    @classmethod
    def build(
        cls,
        *,
        mcp: Any,
        plugins_dir: Path,
        store: ExtensionStore,
        prompts: PromptContributions,
        prompt_loader: PromptLoader,
    ) -> ExtensionsRuntime:
        rpc = ExtensionRpc(ExtensionRegistry())
        origins = ExtensionOrigins()
        # Keeps each extension's state true after install, and asks at once
        # when a call fails rather than waiting out its interval.
        health = ExtensionHealth(registry=rpc.registry, rpc=rpc, mcp=mcp)
        rpc.watch(health.on_call_failed)
        # What an extension adds to the agent (prompts, agents, anchors,
        # commands), tied to READY rather than to being installed, so a
        # dead service stops telling the model about data it cannot fetch.
        # Collisions are settled by the super admin's priority order.
        contributions = ExtensionContributions(
            registry=rpc.registry,
            rpc=rpc,
            plugins_dir=plugins_dir,
            priority=store.priority,
            igni_commands=IgniCommands.of(CommandHandler.builtin_names()),
            prompt_loader=prompt_loader,
            prompts=prompts,
        )
        contributions.attach()
        return cls(
            rpc=rpc,
            origins=origins,
            health=health,
            contributions=contributions,
            prompts=prompts,
        )

    @property
    def rpc(self) -> ExtensionRpc:
        return self._rpc

    @property
    def registry(self) -> ExtensionRegistry:
        return self._rpc.registry

    @property
    def origins(self) -> ExtensionOrigins:
        return self._origins

    @property
    def health(self) -> ExtensionHealth:
        return self._health

    @property
    def contributions(self) -> ExtensionContributions:
        return self._contributions

    @property
    def prompts(self) -> PromptContributions:
        """The table of what extensions have done to the prompts."""
        return self._prompts

    def attach_faults(self, faults: ExtensionFaults, notifications: NotificationCenter) -> None:
        """An extension that keeps timing out or crashing is restarted, and the person told."""
        faults.attach(self.registry)
        faults.on_faulty(self._health.restart_faulty)
        faults.on_faulty(FaultNotices(self.registry, notifications).told)

    def condition_receiver(self) -> ExtensionConditionReceiver:
        """What an extension says holds: the ``when`` of its prompt contributions."""
        return ExtensionConditionReceiver(
            registry=self.registry, prompts=self._prompts, refetch=self._contributions.refetch
        )
