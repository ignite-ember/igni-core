"""Which window and which session an extension's request is about.

:class:`CallScope` over the real windows: a named window must exist and
the object must belong to windows; with none named the caller's own
window, else the active one; the session is the one the window shows,
or the backend's default when the window shows none or cannot be read.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.core.session.client_state import (
    READ_FAILED,
    ClientStateRead,
    ClientStateResult,
)
from igni_core.extensions.host.call_scope import CallScope
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.session_target import SessionTarget
from igni_core.extensions.host.spec import ErrorCode, ObjectSpec, PropertySpec
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.windows import Windows
from igni_core.extensions.host.wire import Op, Request
from tests.host_model_helpers import manifest

PER_WINDOW = ObjectSpec(path="host.ui", doc="", permission=None, per_window=True)
NOT_PER_WINDOW = ObjectSpec(path="host.chat", doc="", permission=None)


class MemoryStore:
    """Client state in a dict: what each window stored."""

    def __init__(self, *, unreadable: bool = False) -> None:
        self.values: dict[str, dict[str, str]] = {}
        self.unreadable = unreadable

    def get_for_client(self, client_id: str) -> ClientStateRead:
        if self.unreadable:
            return ClientStateRead(ok=False, error=READ_FAILED)
        return ClientStateRead(ok=True, values=dict(self.values.get(client_id, {})))

    def set_value(self, client_id: str, key: str, value: str) -> ClientStateResult:
        self.values.setdefault(client_id, {})[key] = value
        return ClientStateResult(ok=True)

    def delete_value(self, client_id: str, key: str) -> ClientStateResult:
        self.values.get(client_id, {}).pop(key, None)
        return ClientStateResult(ok=True)


class Backend:
    def default_session(self) -> str:
        return "boot"


class Targets:
    """Resolves to whatever is on screen: what the scope hands it."""

    async def resolve(self, *, on_screen: str, **_: Any) -> SessionTarget:
        return SessionTarget(session_id=on_screen, on_screen=True)


class Rig:
    def __init__(self, store: MemoryStore | None = None) -> None:
        self.store = store or MemoryStore()
        self.windows = Windows(self.store, types=SchemaTypes(HOST_SCHEMA), push=lambda change: None)
        self.scope = CallScope(self.windows, Backend(), Targets())  # type: ignore[arg-type]

    def open(self, window: str, *, session: str = "", connection: str = "") -> None:
        self.windows.registry.attached(window, connection_id=connection or f"c-{window}")
        if session:
            self.store.set_value(window, "session-id", session)

    def window(self, request_window: str | None, home: str | None, spec: ObjectSpec = PER_WINDOW):
        return self.scope.window(
            spec, Request(id=1, op=Op.GET, path="x", window=request_window), home
        )

    async def session(self, window: str | None) -> str:
        target = await self.scope.session(
            grants=HostGrants(manifest("ext.one")),
            spec=PER_WINDOW,
            member=PropertySpec(name="route", type="list[Page]", doc=""),
            request=Request(id=1, op=Op.GET, path="host.ui"),
            window=window,
        )
        return target.session_id


def test_a_named_window_that_is_open_is_the_one_used() -> None:
    rig = Rig()
    rig.open("w-1")
    rig.open("w-2")

    assert rig.window("w-1", home="w-2") == "w-1"


def test_a_named_window_on_an_object_that_is_not_per_window_is_invalid() -> None:
    rig = Rig()
    rig.open("w-1")

    with pytest.raises(HostModelError) as refused:
        rig.window("w-1", None, NOT_PER_WINDOW)

    assert refused.value.code is ErrorCode.INVALID
    assert refused.value.message == "'host.chat' does not belong to a window"


@pytest.mark.parametrize("name", ["w-gone", ""])
def test_a_named_window_that_is_not_open_is_not_found(name: str) -> None:
    rig = Rig()
    rig.open("w-1")

    with pytest.raises(HostModelError) as refused:
        rig.window(name, home="w-1")

    assert refused.value.code is ErrorCode.NOT_FOUND
    assert refused.value.message == f"no window {name!r} is open"


def test_a_window_whose_connection_closed_is_not_found() -> None:
    rig = Rig()
    rig.open("w-1", connection="c-1")
    rig.windows.registry.detached("c-1")

    with pytest.raises(HostModelError) as refused:
        rig.window("w-1", None)

    assert refused.value.code is ErrorCode.NOT_FOUND


def test_with_none_named_the_callers_own_window_is_used_while_it_is_open() -> None:
    rig = Rig()
    rig.open("w-home")
    rig.open("w-active")  # the last one touched is the active one

    assert rig.window(None, home="w-home") == "w-home"


def test_with_none_named_and_the_callers_window_closed_the_active_one_is_used() -> None:
    rig = Rig()
    rig.open("w-home", connection="c-home")
    rig.open("w-active")
    rig.windows.registry.detached("c-home")

    assert rig.window(None, home="w-home") == "w-active"


def test_a_caller_with_no_window_gets_the_active_one() -> None:
    rig = Rig()
    rig.open("w-1")
    rig.open("w-2")
    rig.windows.registry.touch("w-1")

    assert rig.window(None, home=None) == "w-1"


def test_with_no_window_open_there_is_none() -> None:
    assert Rig().window(None, home=None) is None
    assert Rig().window(None, home="w-never-opened") is None


def test_an_object_that_is_not_per_window_still_has_the_callers_window() -> None:
    rig = Rig()
    rig.open("w-1")

    assert rig.window(None, home="w-1", spec=NOT_PER_WINDOW) == "w-1"


async def test_the_session_is_the_one_the_window_shows() -> None:
    rig = Rig()
    rig.open("w-1", session="s-42")

    assert await rig.session("w-1") == "s-42"


async def test_a_window_showing_no_session_gets_the_default_one() -> None:
    rig = Rig()
    rig.open("w-1")

    assert await rig.session("w-1") == "boot"
    assert await rig.session(None) == "boot"


async def test_a_window_whose_state_cannot_be_read_gets_the_default_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig(MemoryStore(unreadable=True))

    assert await rig.session("w-1") == "boot"
    assert "the state of window 'w-1' could not be read" in caplog.text
