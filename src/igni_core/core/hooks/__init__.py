"""Hooks system — pre/post tool execution hooks."""

from igni_core.core.hooks.envelope import HookEnvelope
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.load_result import HookLoadResult
from igni_core.core.hooks.loader import HookLoader
from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.hooks.schemas import (
    HookDefinition,
    HookLoadWarning,
    HookPayload,
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
    "HookRegistry",
    "HookType",
    "HookResult",
    "HookPayload",
    "HookEnvelope",
    "HookExecutor",
    "HookEvent",
    "MergeStrategy",
    "PermissionDecision",
]
