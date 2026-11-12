"""``PoolWiring``: one set of collaborators for the process, wired per runtime.

A runtime gets its own backend and bridge but the process's login, extensions,
origins, store and notifications; its root transport stamps its session id; a
dispatcher is built over the runtime's own table and queue. The tables are
required, and the wiring is frozen.
"""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from igni_core.backend.pool_wiring import PoolWiring
from igni_core.protocol import messages as msg


class _Wire:
    def __init__(self) -> None:
        self.sent: list[msg.Message] = []

    async def send(self, message: msg.Message) -> None:
        self.sent.append(message)


def _wiring(**override) -> PoolWiring:
    fields = {
        "transport": _Wire(),
        "push_bridge": object(),
        "login": object(),
        "supervisors": object(),
        "gate": object(),
        "contributions": object(),
        "prompt_loader": object(),
        "tool_hints": object(),
        "extensions": object(),
    }
    fields.update(override)
    return PoolWiring(**fields)  # type: ignore[arg-type]


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.project_dir = Path(tempfile.mkdtemp())
    return backend


def _context(router):
    return router._handlers[0]._ctx


def test_a_router_gets_its_own_backend_and_bridge_and_the_processs_shared_rest():
    origins, store, notifications = object(), object(), object()
    wiring = _wiring(origins=origins, extension_store=store, notifications=notifications)
    backend, bridge = _backend(), object()

    ctx = _context(wiring.router_for(backend, bridge))  # type: ignore[arg-type]

    assert ctx.backend is backend
    assert ctx.push is bridge
    assert ctx.login is wiring.login
    assert ctx.extensions is wiring.extensions
    assert (ctx.origins, ctx.extension_store, ctx.notifications) == (origins, store, notifications)


def test_what_a_wiring_was_not_given_reaches_the_router_as_none():
    ctx = _context(_wiring().router_for(_backend(), object()))  # type: ignore[arg-type]

    assert (ctx.origins, ctx.extension_store, ctx.notifications) == (None, None, None)


def test_two_runtimes_share_the_login_and_extensions_but_not_the_backend_or_bridge():
    wiring = _wiring()
    first = _context(wiring.router_for(_backend(), object()))  # type: ignore[arg-type]
    second = _context(wiring.router_for(_backend(), object()))  # type: ignore[arg-type]

    assert first.login is second.login is wiring.login
    assert first.extensions is second.extensions is wiring.extensions
    assert first.backend is not second.backend
    assert first.push is not second.push


def test_a_dispatcher_runs_over_the_runtimes_own_table_and_queue_and_the_shared_rest():
    wiring = _wiring()
    runtime = SimpleNamespace(
        backend=object(), transport=object(), rpc_table={"m": object()}, queue=["q"]
    )

    dispatcher = wiring.dispatcher_for(runtime)  # type: ignore[arg-type]

    assert dispatcher._backend is runtime.backend
    assert dispatcher._transport is runtime.transport
    assert dispatcher._rpc_table is runtime.rpc_table
    assert dispatcher._queue is runtime.queue
    assert dispatcher._login is wiring.login
    assert dispatcher._extensions is wiring.extensions


def test_each_dispatcher_is_a_fresh_object():
    wiring = _wiring()
    runtime = SimpleNamespace(backend=object(), transport=object(), rpc_table={}, queue=[])

    assert wiring.dispatcher_for(runtime) is not wiring.dispatcher_for(runtime)  # type: ignore[arg-type]


async def test_the_stamped_transport_puts_the_backends_session_id_on_what_it_sends():
    wire = _Wire()
    backend = SimpleNamespace(session_id="sess-1")
    stamped = _wiring(transport=wire).stamped(backend)  # type: ignore[arg-type]

    await stamped.send(msg.Info(text="hi"))
    backend.session_id = "sess-2"
    await stamped.send(msg.Info(text="again"))
    await stamped.send(msg.Info(text="own", session_id="mine"))

    assert [m.session_id for m in wire.sent] == ["sess-1", "sess-2", "mine"]


@pytest.mark.parametrize(
    "table",
    ["supervisors", "gate", "contributions", "prompt_loader", "tool_hints", "extensions"],
)
def test_the_per_process_tables_cannot_be_left_out(table):
    fields = {
        name: object()
        for name in (
            "transport",
            "push_bridge",
            "login",
            "supervisors",
            "gate",
            "contributions",
            "prompt_loader",
            "tool_hints",
            "extensions",
        )
    }
    del fields[table]

    with pytest.raises(TypeError, match=table):
        PoolWiring(**fields)  # type: ignore[arg-type]


def test_a_wiring_is_keyword_only_and_frozen():
    with pytest.raises(TypeError):
        PoolWiring(*([object()] * 9))  # type: ignore[misc]
    wiring = _wiring()
    with pytest.raises(dataclasses.FrozenInstanceError):
        wiring.login = object()  # type: ignore[misc]
