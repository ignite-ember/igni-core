"""``host.extensions`` and ``host.self``: the installed extensions, and the caller."""

from __future__ import annotations

from typing import Any

from igni_core.extensions.busy import BusyLease
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.tag import call, get, put
from igni_core.extensions.host.spec import ErrorCode


class ExtensionMembers(Members):
    @get("extensions.installed")
    async def installed(self, c: Call) -> Any:
        return self.host.extensions.installed()

    @call("extensions.set_settings")
    async def set_settings(self, c: Call) -> Any:
        return await self.host.extensions.save_settings(c.extension_id, dict(c.params["values"]))


class SelfMembers(Members):
    @get("self.id")
    async def own_id(self, c: Call) -> str:
        return c.extension_id

    @get("self.settings")
    async def settings(self, c: Call) -> Any:
        return self.host.extensions.settings_of(c.extension_id)

    @put("self.settings")
    async def set_settings(self, c: Call) -> None:
        if not isinstance(c.value, dict):
            raise HostModelError(ErrorCode.INVALID, "settings are an object of key to value")
        await self.host.extensions.save_settings(c.extension_id, dict(c.value))

    @get("self.state")
    async def state(self, c: Call) -> Any:
        return self.host.extensions.info(c.extension_id)

    @get("self.web_url")
    async def web_url(self, c: Call) -> Any:
        return self.host.extensions.info(c.extension_id).web_url

    @call("self.busy")
    async def busy(self, c: Call) -> BusyLease:
        reason = c.params.get("reason")
        if not isinstance(reason, str) or not reason:
            raise HostModelError(ErrorCode.INVALID, "busy needs a reason")
        ttl = c.params.get("ttl_seconds")
        if ttl is not None and not isinstance(ttl, int | float):
            raise HostModelError(ErrorCode.INVALID, "ttl_seconds is a number of seconds")
        return self.host.busy.busy(c.extension_id, reason, ttl)

    @call("self.done")
    async def done(self, c: Call) -> None:
        self.host.busy.done(c.extension_id, str(c.params.get("reason", "")))


__all__ = ["ExtensionMembers", "SelfMembers"]
