"""The audit entry for a call a pool agent — any sub-agent — makes.

The main agent's calls are audited by its
:class:`~igni_core.core.hooks.tool_hook.ToolEventHook`, which also decides
them. A pool agent carries no such hook: whether its call may run was
settled when its tools were built — a tool its permissions ask about is
built to pause, and reaches the hooks only once the person confirmed it —
so this hook writes what it can see: who made the call, whether a person
was asked, how long it ran and whether it failed. A paused call the person
refused never runs; :class:`~igni_core.core.sub_agent_hitl.SubAgentHITLCoordinator`
writes its entry.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

from agno.tools.function import ToolResult

from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.queue.agno_adapter import AgnoCallableAdapter
from igni_core.core.hooks.untrusted import unwrap_untrusted
from igni_core.core.utils.tool_audit import DecidedBy, ToolAudit

#: The agent name written when a pool agent's call carries no name.
SUB_AGENT = "sub-agent"


def unanswered(result: Any) -> str | None:
    """Why an MCP server did not answer a call, or ``None`` when it did.

    Agno turns a call that raised — a timeout, a closed connection — into
    a result reading ``Error: …``, where a tool that reported an error of
    its own reads ``Error from MCP tool …``: that one answered.
    """
    if not isinstance(result, ToolResult) or not isinstance(result.content, str):
        return None
    # Read through the untrusted-output block: every MCP result is
    # wrapped by now, Agno's own "Error: …" included.
    content = unwrap_untrusted(result.content)
    if content.startswith("Error: "):
        return content.removeprefix("Error: ")
    return None


#: The ``error_type`` written for an MCP call its server never answered.
UNANSWERED = "McpUnanswered"


class ToolAuditHook(AgnoCallableAdapter):
    """Agno tool hook: one audit entry per call, around the call."""

    def __init__(self, audit: ToolAudit) -> None:
        self._mark_as_coroutine()
        self.audit = audit

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
        ctx = ToolCallContext(name=name, func=func, args=args or {}, agent=agent, team=team)
        decided_by = DecidedBy.PERSON if ctx.built_with_pause() else DecidedBy.POLICY
        started = time.perf_counter()
        try:
            result = func(**ctx.args) if func is not None else None
            if inspect.isawaitable(result):
                result = await result
        except BaseException as exc:
            self._ran(ctx, decided_by, started, type(exc).__name__)
            raise
        self._ran(ctx, decided_by, started, UNANSWERED if unanswered(result) else None)
        return result

    def _ran(
        self, ctx: ToolCallContext, decided_by: DecidedBy, started: float, error_type: str | None
    ) -> None:
        self.audit.ran(
            agent=ctx.caller_name(SUB_AGENT),
            tool=ctx.name,
            args=ctx.args,
            decided_by=decided_by,
            duration_ms=(time.perf_counter() - started) * 1000,
            error_type=error_type,
        )

    @classmethod
    def on(cls, hooks: list[Any] | None, audit: ToolAudit | None) -> list[Any]:
        """``hooks`` with this hook appended, writing to ``audit`` — a new
        list. Unchanged without an audit, or when one is already there."""
        existing = list(hooks or [])
        if audit is None or any(isinstance(hook, cls) for hook in existing):
            return existing
        return [*existing, cls(audit)]


__all__ = ["SUB_AGENT", "UNANSWERED", "ToolAuditHook", "unanswered"]
