"""``PageChannel``, ``PageRequest``, ``PageSink`` and ``WindowPushes``: the page's way in, and the windows' way out.

A page's request arrives as the RPC's arguments; a malformed one, or a hook
answer, is refused with its id; anything else reaches the model as a page
of that extension in its window. What the model signals goes back to the
windows as ``host_signal`` pushes addressed to the extension.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.extensions.host.caller import Caller
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.page_channel import PageChannel, PageRequest, PageSink
from igni_core.extensions.host.pushes import ExternalOpen, HostSignalPush, WindowPushes
from igni_core.extensions.host.signals import SignalBus
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.window_changes import WindowChange
from igni_core.extensions.host.wire import Reply, Request, SignalMessage
from igni_core.protocol import messages as msg


class Windows:
    """The broadcast: every message the windows were sent."""

    def __init__(self) -> None:
        self.sent: list[msg.Message] = []

    def __call__(self, message: msg.Message) -> None:
        self.sent.append(message)

    def pushes(self, channel: str) -> list[dict[str, Any]]:
        return [
            m.payload
            for m in self.sent
            if isinstance(m, msg.PushNotification) and m.channel == channel
        ]


class Model:
    """What the channel hands a request to: it keeps what it was given."""

    def __init__(self) -> None:
        self.asked: list[tuple[Request, Caller]] = []

    async def handle(self, request: Request, caller: Caller) -> Reply:
        self.asked.append((request, caller))
        return Reply.answered(request.id, "handled")


class Rig:
    def __init__(self) -> None:
        self.windows = Windows()
        self.model = Model()
        self.bus = SignalBus(HOST_SCHEMA, lambda extension_id: None)
        self.channel = PageChannel(self.bus, WindowPushes(self.windows), self.model.handle)


GET = {"id": 4, "op": "get", "path": "chat.processing"}


async def test_a_request_reaches_the_model_as_a_page_of_that_extension_in_its_window() -> None:
    rig = Rig()

    reply = await rig.channel.answer("acme.one", {**GET, "home_window": "w-main"})

    assert reply == Reply.answered(4, "handled")
    request, caller = rig.model.asked[0]
    assert (request.id, request.op.value, request.path) == (4, "get", "chat.processing")
    assert caller.extension_id == "acme.one"
    assert caller.home_window == "w-main"
    assert caller.subscriber is rig.channel.subscriber_for("acme.one")


async def test_a_request_without_an_id_is_answered_under_the_id_page() -> None:
    rig = Rig()

    reply = await rig.channel.answer("acme.one", {"op": "get", "path": "chat.processing"})

    assert reply.id == "page"


@pytest.mark.parametrize("home", [None, "", 0, False])
def test_an_absent_or_empty_home_window_is_none(home: Any) -> None:
    assert PageRequest.model_validate({**GET, "home_window": home}).home_window is None


def test_a_numeric_home_window_is_read_as_its_text() -> None:
    assert PageRequest.model_validate({**GET, "home_window": 7}).home_window == "7"


def test_a_page_request_has_no_home_window_unless_the_window_adds_one() -> None:
    assert PageRequest.model_validate(GET).home_window is None


def test_a_page_request_refuses_a_field_it_does_not_know() -> None:
    with pytest.raises(ValidationError):
        PageRequest.model_validate({**GET, "surprise": 1})


@pytest.mark.parametrize(
    "args, id",
    [
        ({"id": 9, "path": "chat.processing"}, "9"),
        ({"id": "abc", "op": "teleport"}, "abc"),
        ({"op": "get", "path": 5}, "page"),
        ({}, "page"),
    ],
)
async def test_a_malformed_request_is_refused_and_its_id_echoed(
    args: dict[str, Any], id: str
) -> None:
    rig = Rig()

    reply = await rig.channel.answer("acme.one", args)

    assert reply.ok is False
    assert reply.id == id
    assert reply.error is not None and reply.error.code is ErrorCode.INVALID
    assert reply.error.message.startswith("not a host-model request: ")
    assert rig.model.asked == []


async def test_a_page_cannot_answer_a_hook() -> None:
    rig = Rig()

    reply = await rig.channel.answer(
        "acme.one", {"id": 3, "op": "answer", "params": {"hook": "h1", "outcome": "allow"}}
    )

    assert reply.ok is False and reply.id == 3
    assert reply.error is not None and reply.error.code is ErrorCode.INVALID
    assert reply.error.message == "only an extension's process answers a hook event; a page cannot"
    assert rig.model.asked == []


async def test_the_pages_of_one_extension_share_a_subscriber_and_two_extensions_do_not() -> None:
    rig = Rig()

    await rig.channel.answer("acme.one", GET)
    await rig.channel.answer("acme.one", {**GET, "home_window": "w-2"})
    await rig.channel.answer("acme.two", GET)

    first, second, third = (caller.subscriber for _, caller in rig.model.asked)
    assert first is second
    assert first is not third
    assert first is not None and first.extension_id == "acme.one"
    assert third is not None and third.extension_id == "acme.two"


def test_a_subscriber_is_made_once_per_extension_and_attached_to_the_bus() -> None:
    rig = Rig()

    one = rig.channel.subscriber_for("acme.one")

    assert rig.channel.subscriber_for("acme.one") is one
    assert isinstance(one.sink, PageSink)
    assert one in rig.bus._subscribers


# ── What goes back to the windows ──────────────────────────────────


def test_a_page_hears_a_signal_as_a_host_signal_push_addressed_to_its_extension() -> None:
    windows = Windows()
    sink = PageSink(WindowPushes(windows), "acme.one")

    sink.deliver(SignalMessage(signal="chat.run_completed", value={"run_id": "r1"}))

    assert windows.pushes("host_signal") == [
        {"extension_id": "acme.one", "signal": "chat.run_completed", "value": {"run_id": "r1"}}
    ]


def test_a_signal_without_a_value_is_pushed_with_a_null_value() -> None:
    windows = Windows()

    WindowPushes(windows).host_signal("acme.one", SignalMessage(signal="session.switched"))

    assert windows.pushes("host_signal") == [
        {"extension_id": "acme.one", "signal": "session.switched", "value": None}
    ]


def test_a_link_is_pushed_with_its_window_url_and_the_extension_that_asked() -> None:
    windows = Windows()

    WindowPushes(windows).open_external("w-main", "https://example.org/é?q=1", source="acme.one")

    assert windows.pushes("open_external") == [
        {"window": "w-main", "url": "https://example.org/é?q=1", "source": "acme.one"}
    ]


def test_a_window_change_is_pushed_as_json() -> None:
    windows = Windows()
    change = WindowChange(
        window="w-main", field="route", value=[{"kind": "chat"}], rev=3, token="t"
    )

    WindowPushes(windows).window_changed(change)

    assert windows.pushes("window_model_changed") == [change.model_dump(mode="json")]
    assert windows.pushes("window_model_changed")[0]["value"] == [{"kind": "chat"}]


def test_the_pushed_payloads_have_the_published_shapes() -> None:
    assert list(ExternalOpen.model_fields) == ["window", "url", "source"]
    assert list(HostSignalPush.model_fields) == ["extension_id", "signal", "value"]
    assert HostSignalPush(extension_id="a", signal="s").value is None


def test_a_window_change_is_pushed_in_json_mode() -> None:
    windows = Windows()

    WindowPushes(windows).window_changed(
        WindowChange(window="w-main", field="when", value=datetime(2026, 1, 2, tzinfo=UTC), rev=1)
    )

    assert windows.pushes("window_model_changed")[0]["value"] == "2026-01-02T00:00:00Z"
