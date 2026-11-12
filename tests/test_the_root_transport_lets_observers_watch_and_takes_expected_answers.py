"""The backend's root transport: observers on the way out, answers taken back.

Everything igni sends any window passes :class:`ObservedTransport`. Each
observer sees it first; a message whose id was registered with
``expect()`` goes to the waiting caller instead of the windows (a slash
command run for an extension has its answer taken back); and everything
else about the transport is its inner transport's.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable

from igni_core.backend.observed_transport import ObservedTransport
from igni_core.protocol import messages as msg
from igni_core.transport.base import ListeningTransport


class _Windows(ListeningTransport):
    def __init__(self) -> None:
        self.sent: list[msg.Message] = []
        self.events: list[object] = []
        self.watchers: list[Callable[[str, str], None]] = []
        self.closed = False
        self.clients = 2

    async def send(self, message: msg.Message) -> None:
        self.sent.append(message)

    async def receive(self) -> AsyncIterator[msg.Message]:
        yield msg.Typing(text="from a window", client_id="w")

    async def close(self) -> None:
        self.closed = True

    @property
    def is_closed(self) -> bool:
        return self.closed

    async def start(self) -> None:
        self.events.append("start")

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        self.events.append(("wait", timeout))

    @property
    def client_count(self) -> int:
        return self.clients

    def watch_connections(self, watcher: Callable[[str, str], None]) -> None:
        self.watchers.append(watcher)

    def owns_client(self, client_id: str) -> bool:
        return client_id == "mine"


def _rig() -> tuple[ObservedTransport, _Windows]:
    windows = _Windows()
    return ObservedTransport(windows), windows


async def test_every_message_goes_to_each_observer_then_to_the_windows():
    transport, windows = _rig()
    seen: list[tuple[str, str]] = []
    transport.observe(lambda m: seen.append(("a", m.type)))
    transport.observe(lambda m: seen.append(("b", m.type)))

    info = msg.Info(id="x", text="hello")
    await transport.send(info)

    assert seen == [("a", "info"), ("b", "info")]
    assert windows.sent == [info]


async def test_an_observer_that_raises_is_logged_and_neither_the_others_nor_the_send_stop(
    caplog,
):
    transport, windows = _rig()
    seen: list[str] = []

    def broken(_m: msg.Message) -> None:
        raise RuntimeError("observer broke")

    transport.observe(broken)
    transport.observe(lambda m: seen.append(m.type))

    with caplog.at_level(logging.ERROR):
        await transport.send(msg.Info(text="still sent"))

    assert seen == ["info"]
    assert [m.text for m in windows.sent] == ["still sent"]
    assert "an outbound observer failed on info" in caplog.text


async def test_an_observer_added_during_a_send_hears_the_next_message_not_this_one():
    transport, _ = _rig()
    late: list[str] = []
    transport.observe(lambda m: transport.observe(lambda n: late.append(n.text)))

    await transport.send(msg.Info(text="first"))
    assert late == []
    await transport.send(msg.Info(text="second"))
    assert late == ["second"]


async def test_an_expected_answer_is_handed_to_its_waiter_and_not_shown_or_observed():
    transport, windows = _rig()
    seen: list[str] = []
    transport.observe(lambda m: seen.append(m.type))
    future = transport.expect("cmd-1")

    answer = msg.Info(id="cmd-1", text="ran it")
    await transport.send(answer)

    assert future.result() is answer
    assert windows.sent == []
    assert seen == []


async def test_an_expectation_is_consumed_by_the_first_match():
    transport, windows = _rig()
    transport.expect("cmd-1")

    await transport.send(msg.Info(id="cmd-1", text="first"))
    await transport.send(msg.Info(id="cmd-1", text="second"))

    assert [m.text for m in windows.sent] == ["second"]


async def test_other_messages_pass_while_one_is_awaited_and_an_id_less_message_never_matches():
    transport, windows = _rig()
    future = transport.expect("cmd-1")

    await transport.send(msg.Info(id="other", text="a"))
    await transport.send(msg.Info(text="no id"))

    assert [m.text for m in windows.sent] == ["a", "no id"]
    assert not future.done()


async def test_a_forgotten_expectation_lets_the_answer_through():
    transport, windows = _rig()
    future = transport.expect("cmd-1")

    transport.forget("cmd-1")
    transport.forget("never-registered")
    await transport.send(msg.Info(id="cmd-1", text="late"))

    assert [m.text for m in windows.sent] == ["late"]
    assert not future.done()


async def test_an_answer_whose_waiter_gave_up_is_swallowed_not_shown():
    transport, windows = _rig()
    future = transport.expect("cmd-1")
    future.cancel()

    await transport.send(msg.Info(id="cmd-1", text="too late"))

    assert windows.sent == []
    assert transport.expect("cmd-1") is not future  # the registration was consumed


async def test_a_second_expectation_for_an_id_replaces_the_first():
    transport, _ = _rig()
    first = transport.expect("cmd-1")
    second = transport.expect("cmd-1")

    answer = msg.Info(id="cmd-1", text="x")
    await transport.send(answer)

    assert second.result() is answer
    assert not first.done()


async def test_the_rest_is_the_inner_transport_s():
    transport, windows = _rig()
    watcher = lambda kind, client: None  # noqa: E731

    assert transport.inner is windows
    assert transport.client_count == 2
    assert transport.owns_client("mine") and not transport.owns_client("other")
    transport.watch_connections(watcher)
    assert windows.watchers == [watcher]
    await transport.start()
    await transport.wait_for_connection()
    await transport.wait_for_connection(timeout=None)
    assert windows.events == ["start", ("wait", 30.0), ("wait", None)]
    assert [m.text async for m in transport.receive()] == ["from a window"]
    assert not transport.is_closed
    await transport.close()
    assert windows.closed and transport.is_closed
