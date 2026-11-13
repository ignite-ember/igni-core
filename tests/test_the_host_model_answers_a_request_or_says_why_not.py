"""``HostModel.handle``: one request in, one reply out, never a raise.

A refusal is a reply with its code; a failure nobody expected is a
``failed`` reply naming the member and what went wrong, and a line in the
log. Subscriptions go to the signal bus and need a connection to deliver on.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from igni_core.extensions.host.caller import Caller
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.tag import MemberKey
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.wire import Op, Request
from tests.host_model_helpers import HostRig, Sink


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


class Exploding:
    """A binder whose binding raises ``failure``."""

    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def bind(self, *args: Any) -> Any:
        raise self.failure


class TestRefusals:
    async def test_a_caller_that_is_not_installed_is_not_found(self, rig: HostRig) -> None:
        reply = await rig.ask("acme.gone", "get", "app.version")
        assert not reply.ok and reply.error is not None
        assert reply.error.code == ErrorCode.NOT_FOUND
        assert reply.error.message == "no extension 'acme.gone' is installed"
        assert reply.id == 1

    async def test_an_answer_is_not_a_host_model_request(self, rig: HostRig) -> None:
        ext = rig.install(host=["app"])
        reply = await rig.ask(ext, "answer", "")
        assert not reply.ok and reply.error is not None
        assert reply.error.code == ErrorCode.INVALID
        assert reply.error.message == "answer is not a host-model request"

    @pytest.mark.parametrize("op", [Op.SUBSCRIBE, Op.UNSUBSCRIBE])
    async def test_a_channel_with_no_subscriber_takes_no_subscriptions(
        self, rig: HostRig, op: Op
    ) -> None:
        ext = rig.install(host=["app"])
        reply = await rig.ask(ext, op.value, "")
        assert not reply.ok and reply.error is not None
        assert reply.error.code == ErrorCode.INVALID
        assert reply.error.message == "this channel does not take subscriptions"

    async def test_a_refusal_raised_in_the_model_is_the_reply_with_its_code(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["app"])
        assert rig.model is not None
        rig.model.binder = Exploding(HostModelError(ErrorCode.REFUSED, "not for you"))  # type: ignore[assignment]
        reply = await rig.ask(ext, "get", "app.version")
        assert not reply.ok and reply.error is not None
        assert (reply.error.code, reply.error.message) == (ErrorCode.REFUSED, "not for you")


class TestFailures:
    async def test_a_failure_nobody_expected_is_a_failed_reply_and_is_logged(
        self, rig: HostRig, caplog: pytest.LogCaptureFixture
    ) -> None:
        ext = rig.install(host=["app"])
        assert rig.model is not None
        rig.model.binder = Exploding(RuntimeError("kaboom"))  # type: ignore[assignment]
        with caplog.at_level(logging.ERROR):
            reply = await rig.ask(ext, "get", "app.version")
        assert not reply.ok and reply.error is not None
        assert reply.error.code == ErrorCode.FAILED
        assert reply.error.message == "app.version: kaboom"
        assert "host model: get app.version failed" in caplog.text


class TestSubscriptions:
    async def test_a_subscriber_is_told_which_signals_it_will_not_hear(self, rig: HostRig) -> None:
        ext = rig.install(host=["app"])
        assert rig.model is not None
        subscriber = rig.model.services.bus.attach(ext, Sink())
        request = Request(
            id=7, op=Op.SUBSCRIBE, signals=["app.display_changed", "chat.run_completed", "no.such"]
        )
        reply = await rig.model.handle(request, Caller.process(ext, subscriber))
        assert reply.ok and reply.id == 7
        assert reply.result == {
            "subscribed": ["app.display_changed"],
            "not_granted": ["chat.run_completed"],
            "unknown": ["no.such"],
        }

    async def test_unsubscribing_answers_nothing_and_stops_the_signals(self, rig: HostRig) -> None:
        ext = rig.install(host=["app"])
        assert rig.model is not None
        sink = Sink()
        subscriber = rig.model.services.bus.attach(ext, sink)
        caller = Caller.process(ext, subscriber)
        await rig.model.handle(
            Request(id=1, op=Op.SUBSCRIBE, signals=["app.display_changed"]), caller
        )
        assert "app.display_changed" in subscriber.wires
        reply = await rig.model.handle(
            Request(id=2, op=Op.UNSUBSCRIBE, signals=["app.display_changed"]), caller
        )
        assert reply.ok and reply.result is None
        assert "app.display_changed" not in subscriber.wires


class TestWhatItBuilds:
    async def test_a_get_is_answered_by_the_member_as_plain_json(self, rig: HostRig) -> None:
        ext = rig.install(host=["app"])
        reply = await rig.ask(ext, "get", "app.display")
        assert reply.ok
        assert set(reply.result) == {
            "verbose",
            "output_style",
            "output_styles",
            "color_theme",
            "show_reasoning",
            "show_tool_calls",
        }
        assert isinstance(reply.result["output_styles"], list)

    async def test_a_write_through_the_backend_is_told_to_subscribers_as_a_signal(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["app"])
        assert rig.model is not None
        sink = Sink()
        subscriber = rig.model.services.bus.attach(ext, sink)
        rig.model.services.bus.subscribe(subscriber, ["app.display_changed"])
        before = (await rig.ask(ext, "get", "app.display")).result["verbose"]
        reply = await rig.ask(ext, "call", "app.set_verbose", on=not before)
        assert reply.ok and reply.result["verbose"] is (not before)
        await rig.settle()
        assert sink.values("app.display_changed")
        assert sink.values("app.display_changed")[-1]["verbose"] is (not before)

    async def test_the_implemented_members_are_the_built_ones(self, rig: HostRig) -> None:
        assert rig.model is not None
        built = rig.model.implemented()
        assert MemberKey(op=Op.GET, path="app.display") in built
        assert MemberKey(op=Op.CALL, path="app.set_verbose") in built
        assert MemberKey(op=Op.GET, path="no.such") not in built
