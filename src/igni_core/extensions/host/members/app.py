"""``host.app``, ``host.app.state`` and ``host.windows``."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from igni_core import __version__
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.answers import AppStateChange, Connection, Display
from igni_core.extensions.host.members.base import Call, Members, call, get
from igni_core.extensions.host.members.window_values import ExternalLink
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.values import UpdateInfo
from igni_core.protocol.rpc import RpcMethod


class AppMembers(Members):
    async def display_value(self, session_id: str) -> Display:
        config = await self.rpc_in(RpcMethod.GET_DISPLAY_CONFIG, None, session_id)
        styles = await self.rpc_in(RpcMethod.GET_OUTPUT_STYLES, None, session_id)
        return Display.from_rpc(config, styles)

    @get("app.display")
    async def display(self, c: Call) -> Display:
        return await self.display_value(c.session_id)

    @get("app.version")
    async def version(self, c: Call) -> str:
        return __version__

    @get("app.update_available")
    async def update_available(self, c: Call) -> UpdateInfo | None:
        return self.host.state.update

    @get("app.activity")
    async def activity(self, c: Call) -> Any:
        return self.host.backend.activity()

    @get("app.connection")
    async def connection(self, c: Call) -> Connection:
        return Connection.from_status(await self.rpc(RpcMethod.GET_STATUS, c=c))

    @call("app.set_verbose")
    async def set_verbose(self, c: Call) -> Display:
        current = await self.display_value(c.session_id)
        if current.verbose != bool(c.params["on"]):
            # ``app.display_changed`` follows from the toggle itself.
            await self.rpc(RpcMethod.TOGGLE_VERBOSE, c=c)
        return await self.display_value(c.session_id)

    @call("app.set_output_style")
    async def set_output_style(self, c: Call) -> Display:
        name = str(c.params["name"]).strip()
        if not name or any(ch.isspace() for ch in name):
            raise HostModelError(ErrorCode.INVALID, f"{name!r} is not an output style name")
        outcome = await self.host.gateway.command(f"/output-style {name}", session_id=c.session_id)
        if outcome.kind == "error":
            raise HostModelError(ErrorCode.REFUSED, str(outcome.content or "refused"))
        display = await self.display_value(c.session_id)
        self.host.bus.emit("app.display_changed", display.model_dump())
        return display

    @call("app.check_for_update")
    async def check_for_update(self, c: Call) -> UpdateInfo | None:
        found = await self.rpc(RpcMethod.CHECK_FOR_UPDATE, c=c)
        update = UpdateInfo.from_check(found)
        self.host.state.record_update(update)
        self.host.bus.emit("app.update_changed", update.model_dump() if update else None)
        return update

    @call("app.open_external")
    async def open_external(self, c: Call) -> None:
        window = self.window(c)
        try:
            link = ExternalLink(url=c.params["url"])
        except ValidationError as exc:
            raise HostModelError(ErrorCode.INVALID, exc.errors()[0]["msg"]) from None
        self.host.pushes.open_external(window, link.url, source=c.extension_id)


class AppStateMembers(Members):
    """An extension's own keys, in the store a window's state lives in."""

    @staticmethod
    def client_id(c: Call) -> str:
        # Its own client id: no window and no other extension can read
        # or write these keys, and these keys cannot reach a window's.
        return f"ext:{c.extension_id}"

    @get("app.state.values")
    async def values(self, c: Call) -> Any:
        return self.host.backend.client_state.get_for_client(self.client_id(c))

    @call("app.state.get")
    async def get_value(self, c: Call) -> Any:
        return self.host.backend.client_state.get_for_client(self.client_id(c)).get(c.params["key"])

    @call("app.state.set")
    async def set_value(self, c: Call) -> None:
        key = str(c.params["key"])
        if not key:
            raise HostModelError(ErrorCode.INVALID, "a state key cannot be empty")
        value = str(c.params["value"])
        self.host.backend.client_state.set_value(self.client_id(c), key, value)
        self.host.bus.emit(
            "app.state.changed",
            AppStateChange(key=key, value=value).model_dump(),
            only=c.extension_id,
        )

    @call("app.state.delete")
    async def delete_value(self, c: Call) -> None:
        key = str(c.params["key"])
        self.host.backend.client_state.delete_value(self.client_id(c), key)
        self.host.bus.emit(
            "app.state.changed",
            AppStateChange(key=key, value=None).model_dump(),
            only=c.extension_id,
        )


class WindowMembers(Members):
    @get("windows.list")
    async def window_list(self, c: Call) -> Any:
        return self.host.windows.describe_all()

    @get("windows.focused")
    async def focused(self, c: Call) -> Any:
        return self.host.windows.focused()


__all__ = ["AppMembers", "AppStateMembers", "WindowMembers"]
