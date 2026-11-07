"""Hooks system — pre/post tool execution hooks."""

from igni_core.core.hooks.envelope import HookEnvelope
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.loader import HookLoader
from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.hooks.schemas import (
    GenericHookPayload,
    HookDefinition,
    HookDefinitionBase,
    HookLoadResult,
    HookLoadWarning,
    HookPayload,
    HookPayloadBase,
    HookResult,
    HookType,
    MergeStrategy,
    PermissionDecision,
)

__all__ = [
    "HookLoader",
    "HookLoadResult",
    "HookLoadWarning",
    "HookDefinition",
    "HookDefinitionBase",
    "HookRegistry",
    "HookType",
    "HookResult",
    "HookPayload",
    "HookPayloadBase",
    "GenericHookPayload",
    "HookEnvelope",
    "HookExecutor",
    "HookEvent",
    "MergeStrategy",
    "PermissionDecision",
]
