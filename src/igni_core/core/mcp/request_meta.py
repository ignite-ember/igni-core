"""An MCP client session that puts igni's ``_meta`` on every tool call, and is busy during it.

Agno calls ``session.call_tool(name, arguments)`` with no ``meta``, and a
server that serves several callers needs to know which one a call is
for. The session is the one place every call passes through — Agno's
for the agent, igni's own for a page or a slash command — so the meta
goes on here, from a provider asked at the moment of each call.

The provider is the caller's to decide: the MCP layer does not know
what an extension or a principal is. What it returns is merged over
any meta the caller passed itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp import ClientSession, types
from mcp.shared.exceptions import MCPError
from mcp.types import REQUEST_TIMEOUT

from igni_core.core.activity import ActivityRegistry, BusyKind
from igni_core.core.mcp.person_waits import person_waits
from igni_core.core.mcp.tool_errors import error_text, record_failure

#: What the session's own clock is given while igni keeps the deadline: a
#: day, so only igni's watchdog below ends a call.
_NO_DEADLINE = 86_400.0

#: Asked on every tool call; ``None`` adds nothing.
RequestMeta = Callable[[], dict[str, Any] | None]


class RequestMetaSession(ClientSession):
    """A :class:`ClientSession` whose tool calls carry the provider's ``_meta``."""

    def __init__(
        self,
        *args: Any,
        request_meta: RequestMeta | None = None,
        busy_holder: str = "mcp",
        person_wait_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._request_meta = request_meta
        self._busy_holder = busy_holder
        #: The server name a wait on the person is held under
        #: (:mod:`~igni_core.core.mcp.person_waits`); ``None`` keeps the
        #: session's own deadline.
        self._person_wait_key = person_wait_key
        self._deadline = float(kwargs.get("read_timeout_seconds") or 0) or None

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *args: Any,
        meta: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> types.CallToolResult:
        added = self._request_meta() if self._request_meta is not None else None
        if added:
            meta = {**(meta or {}), **added}
        # Busy while the server works on it (``core/activity``).
        with ActivityRegistry.shared().hold(BusyKind.MCP_CALL, self._busy_holder, name):
            try:
                if self._person_wait_key is not None and self._deadline is not None and not args:
                    result = await self._call_waiting_on_people(name, arguments, meta, kwargs)
                else:
                    result = await super().call_tool(name, arguments, *args, meta=meta, **kwargs)
            except Exception as exc:
                record_failure(f"calling {name} failed: {exc}", answered=False)
                raise
        if result.is_error:
            record_failure(error_text(result), answered=True)
        return result

    async def _call_waiting_on_people(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None,
        kwargs: dict[str, Any],
    ) -> types.CallToolResult:
        """The call, with its deadline counted only while nobody is asked.

        The session's read timeout bounds a call the server is working on;
        while the server waits for the person (a dialog), the deadline
        stops, and it starts again from its full length once they answer.
        """
        key, deadline = self._person_wait_key or "", self._deadline or 0.0
        kwargs = {**kwargs, "read_timeout_seconds": _NO_DEADLINE}
        result: types.CallToolResult | None = None
        with person_waits.timed(key, deadline) as scope:
            result = await super().call_tool(name, arguments, meta=meta, **kwargs)
        if scope.cancelled_caught or result is None:
            raise MCPError(code=REQUEST_TIMEOUT, message="Request 'tools/call' timed out")
        return result
