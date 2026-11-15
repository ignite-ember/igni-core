"""``PreToolUse`` / ``PostToolUse`` around a sub-agent's or a team member's tool calls.

HOOKS.md: ``PreToolUse`` fires "before any tool call". The main agent's
calls go through :class:`~igni_core.core.hooks.tool_hook.ToolEventHook`;
an agent ``spawn_agent`` or ``spawn_team`` starts carries only the audit
hook, so its calls fired neither event and a PreToolUse security hook
never saw them, let alone blocked one (campaign R128).

The permission side of those calls is decided at their pause
(:class:`~igni_core.core.sub_agent_hitl.SubAgentHITLCoordinator`); this
hook adds only the configured hooks, with the answers the main agent's
``PreToolUse`` stage gives.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.hook_firer import HookFirer
from igni_core.core.hooks.queue.agno_adapter import AgnoCallableAdapter
from igni_core.core.hooks.schemas import PermissionDecision
from igni_core.core.hooks.tool_events import (
    PostToolUseFailurePayload,
    PostToolUsePayload,
    PreToolUsePayload,
)


class SubAgentToolHook(AgnoCallableAdapter):
    """Agno tool hook: the configured tool hooks, around one sub-agent call."""

    def __init__(self, executor: HookExecutor, session_id: str) -> None:
        self._mark_as_coroutine()
        self._firer = HookFirer(executor, session_id)

    @classmethod
    def on(
        cls, hooks: list[Any] | None, executor: HookExecutor | None, session_id: str
    ) -> list[Any]:
        """``hooks`` with this one first, outermost; unchanged with no executor."""
        existing = list(hooks or [])
        if executor is None or any(isinstance(h, cls) for h in existing):
            return existing
        return [cls(executor, session_id), *existing]

    async def __call__(
        self,
        name: str = "",
        func: Callable[..., Any] | None = None,
        args: dict[str, Any] | None = None,
        agent: Any = None,
        team: Any = None,
        **_: Any,
    ) -> Any:
        # Parameter names are Agno's (see AgnoCallableAdapter).
        call_args = args or {}
        pre = await self._firer.fire(
            HookEvent.PRE_TOOL_USE, name, PreToolUsePayload.from_call(name, call_args)
        )
        refusal = self._refusal(name, pre.permission_decision, pre.should_continue, pre.message)
        if refusal is not None:
            return refusal
        try:
            result = func(**call_args) if func is not None else None
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            await self._firer.fire(
                HookEvent.POST_TOOL_USE_FAILURE,
                name,
                PostToolUseFailurePayload.from_exception(name, call_args, exc),
            )
            raise
        await self._firer.fire(
            HookEvent.POST_TOOL_USE, name, PostToolUsePayload.from_result(name, call_args, result)
        )
        return result

    @staticmethod
    def _refusal(
        name: str, decision: PermissionDecision, should_continue: bool, message: str
    ) -> str | None:
        """What the main agent's ``PreToolUse`` stage answers for the same hook result."""
        if decision is PermissionDecision.DENY:
            return message or f"Blocked: '{name}' denied by PreToolUse hook"
        if decision is PermissionDecision.ASK:
            return message or (
                f"Blocked: a PreToolUse hook asked for confirmation of '{name}', "
                "and hooks have no way to prompt the user. Ask the user directly, or "
                "have the hook return allow/deny."
            )
        if not should_continue and decision is not PermissionDecision.ALLOW:
            return message or "Blocked by PreToolUse hook"
        return None


__all__ = ["SubAgentToolHook"]
