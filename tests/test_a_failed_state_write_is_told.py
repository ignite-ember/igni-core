"""A state that was not stored is told, not swallowed.

The client's state lives in one table, and the store used to answer
every failure the same way: an empty result and a line in the debug
log. A window that was not told showed its defaults and then wrote
over what was really there. The store answers with a result now, and
the window's model says so when the state cannot be read or written.

What the store says is a sentence about the store, never the driver's
own words — those stay in the log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from igni_core.backend.headless_extensions import RunState
from igni_core.backend.pool_rpcs import HostModelRpcs
from igni_core.core.db.database import Database
from igni_core.core.session.client_state import (
    DELETE_FAILED,
    READ_FAILED,
    WRITE_FAILED,
    ClientStateRead,
    ClientStateResult,
    ClientStateStore,
    ClientStateStoreProtocol,
)
from igni_core.extensions.host.call_scope import CallScope
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.members.app import AppStateMembers
from igni_core.extensions.host.members.call import Arguments, Call
from igni_core.extensions.host.session_target import SessionTarget
from igni_core.extensions.host.spec import ErrorCode, ObjectSpec, PropertySpec
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.window_errors import WindowStateError
from igni_core.extensions.host.window_rpc import WindowModelRpc
from igni_core.extensions.host.windows import Windows
from igni_core.extensions.host.wire import Op, Request
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import manifest

GONE = "the database is gone"


class BrokenStore:
    """A store whose database is not there any more: reads and writes fail."""

    def get_for_client(self, client_id: str) -> ClientStateRead:
        return ClientStateRead(ok=False, error=READ_FAILED)

    def set_value(self, client_id: str, key: str, value: str) -> ClientStateResult:
        return ClientStateResult(ok=False, error=WRITE_FAILED)

    def delete_value(self, client_id: str, key: str) -> ClientStateResult:
        return ClientStateResult(ok=False, error=DELETE_FAILED)


class UnwritableStore:
    """A store that reads fine and cannot be written to.

    The two failures are told apart on purpose: a window writes its
    state by reading it first, so a store that cannot read is never
    reached on the write path.
    """

    def get_for_client(self, client_id: str) -> ClientStateRead:
        return ClientStateRead(ok=True)

    def set_value(self, client_id: str, key: str, value: str) -> ClientStateResult:
        return ClientStateResult(ok=False, error=WRITE_FAILED)

    def delete_value(self, client_id: str, key: str) -> ClientStateResult:
        return ClientStateResult(ok=False, error=WRITE_FAILED)


class _Bus:
    """The signals the host model emits."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def emit(self, wire: str, payload: dict[str, Any], **_: Any) -> None:
        self.sent.append((wire, payload))


class _Host:
    """What an extension's own-state members reach of the host."""

    def __init__(self, store: ClientStateStoreProtocol) -> None:
        self.backend = type("_Backend", (), {"client_state": store})()
        self.bus = _Bus()


class _PoolWindows:
    """The windows the pool asks about."""

    @property
    def registry(self) -> _PoolWindows:
        return self

    def by_connection(self, connection_id: str) -> str | None:
        return None


class _PoolServices:
    def __init__(self, window_rpc: WindowModelRpc) -> None:
        self.window_rpc = window_rpc
        self.windows = _PoolWindows()


class _PoolModel:
    """The host model, as the pool reaches it."""

    def __init__(self, window_rpc: WindowModelRpc) -> None:
        self.services = _PoolServices(window_rpc)


class _Targets:
    """The session targets, told which session is on screen."""

    async def resolve(self, *, on_screen: str, **_: Any) -> SessionTarget:
        return SessionTarget(session_id=on_screen, on_screen=True)


class _DefaultBackend:
    """The backend, for the session a run starts with."""

    def default_session(self) -> str:
        return "boot"


def _call(**params: Any) -> Call:
    return Call(
        extension_id="ext.one",
        grants=HostGrants(manifest("ext.one")),
        window="w-1",
        target=SessionTarget(session_id="s1", on_screen=True),
        arguments=Arguments(params=params),
    )


def _windows(store: ClientStateStoreProtocol) -> Windows:
    return Windows(store, types=SchemaTypes(HOST_SCHEMA), push=lambda change: None)


def _break_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every session the store opens fails from now on."""

    def broken(self: Database) -> None:
        raise RuntimeError(GONE)

    monkeypatch.setattr(Database, "sync_session", broken)


def test_a_read_that_fails_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ClientStateStore(tmp_path / "client_state.db")
    _break_the_database(monkeypatch)

    read = store.get_for_client("w")

    assert not read.ok
    assert read.error == READ_FAILED
    # The driver's own words stay in the log: they can carry SQL, a
    # file name and a user, and they go on the wire.
    assert GONE not in read.error


def test_a_write_that_fails_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ClientStateStore(tmp_path / "client_state.db")
    _break_the_database(monkeypatch)

    written = store.set_value("w", "k", "v")
    deleted = store.delete_value("w", "k")

    assert not written.ok
    assert written.error == WRITE_FAILED
    assert not deleted.ok
    assert deleted.error == DELETE_FAILED
    assert GONE not in written.error
    assert GONE not in deleted.error


def test_a_write_that_worked_says_so(tmp_path: Path) -> None:
    store = ClientStateStore(tmp_path / "client_state.db")

    written = store.set_value("w", "k", "v")

    assert written.ok
    assert written.error == ""
    assert store.get_for_client("w").values == {"k": "v"}


def test_a_write_without_a_client_is_not_a_write(tmp_path: Path) -> None:
    """A key under no client is stored nowhere — so it is a failure, not
    a write that happened."""
    store = ClientStateStore(tmp_path / "client_state.db")

    assert not store.set_value("", "k", "v").ok
    assert not store.set_value("w", "", "v").ok
    assert not store.get_for_client("").ok


def test_a_window_whose_state_could_not_be_read_is_told() -> None:
    windows = _windows(BrokenStore())

    with pytest.raises(WindowStateError) as failure:
        windows.get("w-1", "session")

    assert "could not be read" in str(failure.value)


def test_a_window_whose_state_could_not_be_written_is_told() -> None:
    """The write path, not the read before it: a window writes its state
    by reading it first, so the store reads and the write fails."""
    windows = _windows(UnwritableStore())

    with pytest.raises(WindowStateError) as failure:
        windows.write("w-1", "sidebar", {"open": True})

    assert "could not be written" in str(failure.value)


async def test_a_window_is_told_over_the_pool_that_its_write_failed() -> None:
    """The window's own path: the pool carries the failure as a refusal
    the window can show, not as an exception."""
    rpcs = HostModelRpcs(_PoolModel(WindowModelRpc(_windows(UnwritableStore()))))

    answer = await rpcs.answer(
        msg.RPCRequest(
            id="1",
            method=RpcMethod.WINDOW_MODEL,
            args={"op": "write", "window": "w-1", "field": "sidebar", "value": {"open": True}},
        )
    )

    assert answer.result is None
    assert answer.error == "window_model: window 'w-1' state could not be written"


async def test_a_window_is_told_over_the_pool_that_its_state_could_not_be_read() -> None:
    rpcs = HostModelRpcs(_PoolModel(WindowModelRpc(_windows(BrokenStore()))))

    answer = await rpcs.answer(
        msg.RPCRequest(
            id="1",
            method=RpcMethod.WINDOW_MODEL,
            args={"op": "hydrate", "window": "w-1"},
        )
    )

    assert answer.result is None
    assert answer.error == "window_model: window 'w-1' state could not be read"


async def test_a_window_whose_state_cannot_be_read_does_not_stop_the_extension_surface() -> None:
    """The window's state is the window's problem. The extension's work
    goes on, in the session the run started with."""
    scope = CallScope(_windows(BrokenStore()), _DefaultBackend(), _Targets())

    target = await scope.session(
        grants=HostGrants(manifest("ext.one")),
        spec=ObjectSpec(path="host.ui", doc="", permission=None),
        member=PropertySpec(name="route", type="list[Page]", doc=""),
        request=Request(id=1, op=Op.GET, path="host.ui"),
        window="w-1",
    )

    assert target.session_id == "boot"


async def test_an_extension_is_told_its_state_was_not_stored() -> None:
    """The extension's own state: a write that did not happen is a
    failure the extension is told about, not a change it is told about."""
    members = AppStateMembers(_Host(BrokenStore()))

    with pytest.raises(HostModelError) as failure:
        await members.set_value(_call(key="cursor", value="42"))

    assert failure.value.code == ErrorCode.FAILED
    assert failure.value.message == WRITE_FAILED


async def test_an_extension_is_told_its_state_could_not_be_read() -> None:
    members = AppStateMembers(_Host(BrokenStore()))

    with pytest.raises(HostModelError) as failure:
        await members.get_value(_call(key="cursor"))

    assert failure.value.code == ErrorCode.FAILED
    assert failure.value.message == READ_FAILED


async def test_an_extension_keeps_its_state_when_the_write_worked(tmp_path: Path) -> None:
    store = ClientStateStore(tmp_path / "client_state.db")
    members = AppStateMembers(_Host(store))

    await members.set_value(_call(key="cursor", value="42"))

    assert await members.get_value(_call(key="cursor")) == "42"
    assert await members.values(_call()) == {"cursor": "42"}


def test_a_run_with_no_window_keeps_the_state_it_is_given() -> None:
    """The headless store is a store: a write that worked is a write
    that worked, as in the database one."""
    state = RunState()

    written = state.set_value("w", "k", "v")

    assert written.ok
    assert state.get_for_client("w").values == {"k": "v"}
    assert state.delete_value("w", "k").ok
    assert state.get_for_client("w").values == {}
