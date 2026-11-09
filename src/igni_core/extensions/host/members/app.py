"""``host.app``, ``host.app.state`` and ``host.windows``."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ValidationError

from igni_core import __version__
from igni_core.backend.activity import Activity
from igni_core.core.session.client_state import ClientStateRead
from igni_core.extensions.host.display import Display, DisplayReader
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.external_link import ExternalLink
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.services import HostServices
from igni_core.extensions.host.signal_values import Signal
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.values import Connection, UpdateInfo
from igni_core.protocol.rpc import RpcMethod


class AppStateChanged(Signal):
    """``app.state.changed``: one of an extension's own keys, set or removed."""

    WIRE: ClassVar[str] = "app.state.changed"

    key: str
    value: str | None


class AppMembers(Members):
    def __init__(self, host: HostServices) -> None:
        super().__init__(host)
        self._display = DisplayReader(host.gateway)

    @get("app.display")
    async def display(self, c: Call) -> Display:
        return await self._display.read(c.session_id)

    @get("app.version")
    async def version(self, c: Call) -> str:
        return __version__

    @get("app.update_available")
    async def update_available(self, c: Call) -> UpdateInfo | None:
        return self.host.state.update

    @get("app.activity")
    async def activity(self, c: Call) -> Activity:
        return self.host.backend.activity()

    @get("app.connection")
    async def connection(self, c: Call) -> Connection:
        return (await self.read_status(c)).connection()

    @call("app.set_verbose")
    async def set_verbose(self, c: Call) -> Display:
        current = await self._display.read(c.session_id)
        if current.verbose != bool(c.params["on"]):
            # ``app.display_changed`` follows from the toggle itself.
            await self.rpc(RpcMethod.TOGGLE_VERBOSE, c=c)
        return await self._display.read(c.session_id)

    @call("app.set_output_style")
    async def set_output_style(self, c: Call) -> Display:
        name = str(c.params["name"]).strip()
        if not name or any(ch.isspace() for ch in name):
            raise HostModelError(ErrorCode.INVALID, f"{name!r} is not an output style name")
        outcome = await self.host.gateway.command(f"/output-style {name}", session_id=c.session_id)
        if outcome.kind == "error":
            raise HostModelError(ErrorCode.REFUSED, str(outcome.content or "refused"))
        display = await self._display.read(c.session_id)
        self.signals.send(display)
        return display

    @call("app.check_for_update")
    async def check_for_update(self, c: Call) -> UpdateInfo | None:
        found = await self.rpc(RpcMethod.CHECK_FOR_UPDATE, c=c)
        update = UpdateInfo.from_check(found)
        self.host.state.record_update(update)
        self.signals.value(UpdateInfo.CHANGED, update)
        return update

    @call("app.open_external")
    async def open_external(self, c: Call) -> None:
        window = c.require_window()
        try:
            link = ExternalLink(url=c.params["url"])
        except ValidationError as exc:
            raise HostModelError(ErrorCode.INVALID, exc.errors()[0]["msg"]) from None
        self.host.pushes.open_external(window, link.url, source=c.extension_id)


class AppStateMembers(Members):
    """An extension's own keys, in the store a window's state lives in,
    under its own client id (:attr:`Call.own_client_id`)."""

    @get("app.state.values")
    async def values(self, c: Call) -> dict[str, str]:
        return self._state(c).values

    @call("app.state.get")
    async def get_value(self, c: Call) -> str | None:
        return self._state(c).values.get(c.params["key"])

    @call("app.state.set")
    async def set_value(self, c: Call) -> None:
        key = str(c.params["key"])
        if not key:
            raise HostModelError(ErrorCode.INVALID, "a state key cannot be empty")
        value = str(c.params["value"])
        result = self.host.backend.client_state.set_value(c.own_client_id, key, value)
        if not result.ok:
            raise HostModelError(ErrorCode.FAILED, result.error)
        self.signals.send(AppStateChanged(key=key, value=value), only=c.extension_id)

    @call("app.state.delete")
    async def delete_value(self, c: Call) -> None:
        key = str(c.params["key"])
        result = self.host.backend.client_state.delete_value(c.own_client_id, key)
        if not result.ok:
            raise HostModelError(ErrorCode.FAILED, result.error)
        self.signals.send(AppStateChanged(key=key, value=None), only=c.extension_id)

    def _state(self, c: Call) -> ClientStateRead:
        """The extension's own state, read from the store.

        A read that failed is a failure: the extension is told, rather
        than handed an empty state it would read as "nothing stored".
        """
        read = self.host.backend.client_state.get_for_client(c.own_client_id)
        if not read.ok:
            raise HostModelError(ErrorCode.FAILED, read.error)
        return read


class WindowMembers(Members):
    @get("windows.list")
    async def window_list(self, c: Call) -> Any:
        return self.host.windows.registry.describe_all()

    @get("windows.focused")
    async def focused(self, c: Call) -> Any:
        return self.host.windows.registry.focused()


__all__ = ["AppMembers", "AppStateChanged", "AppStateMembers", "WindowMembers"]
