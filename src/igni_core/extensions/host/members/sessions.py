"""``host.sessions``."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.tag import call, get, put
from igni_core.extensions.host.members.window_values import WindowFieldName
from igni_core.extensions.host.rpc_wire.sessions import SwitchSessionArgs
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.values import SessionInfo
from igni_core.protocol.rpc import RpcMethod


class SessionSwitched(BaseModel):
    """The session a switch landed on, and the window that shows it."""

    session_id: str
    window: str | None


class SessionMembers(Members):
    @get("sessions.list")
    async def session_list(self, c: Call) -> list[SessionInfo]:
        result = await self.rpc(RpcMethod.LIST_SESSIONS, c=c)
        sessions = result.get("sessions", []) if isinstance(result, dict) else result
        return [SessionInfo.from_entry(s) for s in sessions or []]

    @get("sessions.current")
    async def current(self, c: Call) -> str:
        return c.session_id

    @put("sessions.current")
    async def set_current(self, c: Call) -> None:
        await self.switch(c, str(c.value or ""))

    @call("sessions.open")
    async def open(self, c: Call) -> SessionSwitched:
        return await self.switch(c, str(c.params["session_id"]))

    async def switch(self, c: Call, session_id: str) -> SessionSwitched:
        if not session_id:
            raise HostModelError(ErrorCode.INVALID, "a session id is required")
        if c.window is None:
            # No window is open: the TUI's way, which switches the boot
            # session itself.
            await self.rpc_checked(
                RpcMethod.SWITCH_SESSION, SwitchSessionArgs(session_id=session_id), c=c
            )
            self.host.observations.session_switched(session_id, None)
            return SessionSwitched(session_id=session_id, window=None)
        # A window binds a session as the window does: attach (which
        # refuses another project's session), then the window's state.
        attached = await self.host.gateway.attach(session_id, None)
        window = c.window
        # The window's model says which session it shows; the write is
        # the signal (``session.switched``) and what moves the window.
        self.host.windows.write(window, WindowFieldName.SESSION.value, attached.session_id)
        return SessionSwitched(session_id=attached.session_id, window=window)

    @call("sessions.create")
    async def create(self, c: Call) -> Any:
        title = c.params.get("title")
        if title is not None and not str(title).strip():
            raise HostModelError(ErrorCode.INVALID, "sessions.create(title): an empty title")
        project_dir = c.params.get("project_dir") or str(self.host.backend.project_dir)
        attached = await self.host.gateway.attach("", project_dir)
        if title is not None:
            # Before anything runs in it, as a service's new chat is
            # titled, so no auto-name replaces it.
            titled = await self.host.backend.title(session_id=attached.session_id, title=str(title))
            if not titled.ok:
                raise HostModelError(
                    ErrorCode.FAILED,
                    f"session {attached.session_id} was created but not titled: {titled.error}",
                )
        if c.params.get("focus") and c.window is not None:
            # The folder first: a window moving to the session reads it.
            self.host.windows.write(
                c.window, WindowFieldName.PROJECT_DIR.value, attached.project_dir
            )
            self.host.windows.write(c.window, WindowFieldName.SESSION.value, attached.session_id)
        return attached

    @call("sessions.attach")
    async def attach(self, c: Call) -> Any:
        return await self.host.gateway.attach(
            str(c.params["session_id"]), c.params.get("project_dir")
        )


__all__ = ["SessionMembers", "SessionSwitched"]
