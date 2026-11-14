"""``host.hooks``, ``host.loop``, ``host.plan`` and ``host.policy``.

What each member asks of igni, what the extension is told, and what is
refused when igni answers with something the member's model does not
describe. Driven through the real host model; the RPCs that must answer
something particular come from :func:`tests.scripted_backend.script`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import HostRig
from tests.scripted_backend import script

EVERYTHING = [p.name for p in HOST_SCHEMA.permissions if p.name != "workspace"]


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def ok(reply: Any) -> Any:
    assert reply.ok, f"refused: {reply.error}"
    return reply.result


def refused(reply: Any, code: str) -> str:
    assert not reply.ok, f"answered {reply.result!r}"
    assert reply.error.code == code, reply.error
    return reply.error.message


class TestTheHooks:
    async def test_the_hooks_igni_has_are_listed_with_what_each_runs(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(
            rig,
            {
                RpcMethod.GET_HOOKS_DETAILS: [
                    {"event": "PreToolUse", "type": "command", "command": "lint.sh"},
                    {"event": "Stop", "type": "http", "url": "https://hooks.acme.test/x"},
                ]
            },
        )
        rows = ok(await rig.ask(ext, "get", "hooks.details"))
        assert [(r["event"], r["type"]) for r in rows] == [
            ("PreToolUse", "command"),
            ("Stop", "http"),
        ]
        assert rows[0]["command"] == "lint.sh"
        assert rows[0]["url"] == ""
        assert rows[1]["url"] == "https://hooks.acme.test/x"

    async def test_no_hooks_is_an_empty_list(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        assert ok(await rig.ask(ext, "get", "hooks.details")) == []

    async def test_a_row_that_is_not_a_hook_is_refused_not_passed_on(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {RpcMethod.GET_HOOKS_DETAILS: [{"type": "command"}]})
        message = refused(await rig.ask(ext, "get", "hooks.details"), "failed")
        assert "hooks.details" in message
        assert "event" in message

    async def test_an_answer_that_is_not_a_list_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {RpcMethod.GET_HOOKS_DETAILS: {"event": "Stop"}})
        message = refused(await rig.ask(ext, "get", "hooks.details"), "failed")
        assert "not a list" in message

    async def test_reload_makes_igni_read_the_hooks_again(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        reloads: list[str] = []
        rig.session.controllers.panels.reload_hooks = lambda: reloads.append("again")
        assert ok(await rig.ask(ext, "call", "hooks.reload")) is None
        assert reloads == ["again"]

    async def test_the_session_start_hook_is_fired_in_the_callers_session(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=EVERYTHING)
        assert ok(await rig.ask(ext, "call", "hooks.fire_session_start")) is None
        assert rig.session.session_start_hooks == 1


class TestTheLoop:
    async def test_status_is_the_loop_status_snapshot(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        status = ok(await rig.ask(ext, "get", "loop.status"))
        assert status["active"] is False
        assert status["paused"] is False
        assert status["prompt"] == ""
        assert status["iteration_index"] == 0

    async def test_a_status_that_is_not_a_snapshot_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {RpcMethod.LOOP_STATUS: {"active": True}})
        message = refused(await rig.ask(ext, "get", "loop.status"), "failed")
        assert "loop.status" in message

    async def test_pause_says_whether_the_loop_was_paused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        assert ok(await rig.ask(ext, "call", "loop.pause")) is True
        script(rig, {RpcMethod.LOOP_PAUSE: None})
        assert ok(await rig.ask(ext, "call", "loop.pause")) is False

    async def test_resume_answers_what_igni_said(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        assert ok(await rig.ask(ext, "call", "loop.resume")) == "resumed"
        assert rig.session.loop.resumed == "resumed"

    async def test_resume_with_nothing_said_is_the_empty_string(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {RpcMethod.LOOP_RESUME: None})
        assert ok(await rig.ask(ext, "call", "loop.resume")) == ""

    async def test_cancel_pending_says_whether_something_was_cancelled(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        assert ok(await rig.ask(ext, "call", "loop.cancel_pending")) is True
        assert rig.session.loop.cancelled is True
        script(rig, {RpcMethod.CANCEL_PENDING_LOOP: False})
        assert ok(await rig.ask(ext, "call", "loop.cancel_pending")) is False

    async def test_an_iteration_that_is_not_one_is_refused_not_taken_for_nothing(
        self, rig: HostRig
    ) -> None:
        """Only ``None`` is "nothing waiting": an odd answer is a refusal."""
        ext = rig.install(host=EVERYTHING)
        script(rig, {RpcMethod.POP_PENDING_LOOP_ITERATION: {"kind": "sideways"}})
        message = refused(await rig.ask(ext, "call", "loop.pop_pending"), "failed")
        assert "loop.pop_pending" in message

    async def test_an_empty_advance_is_an_advance_and_not_nothing(self, rig: HostRig) -> None:
        """Only ``None`` is "nothing waiting": a falsy answer is still read as an advance."""
        ext = rig.install(host=EVERYTHING)
        script(rig, {RpcMethod.POP_PENDING_LOOP_ITERATION: {}})
        advance = ok(await rig.ask(ext, "call", "loop.pop_pending"))
        assert advance is not None
        assert advance["kind"] == "step"


class TestThePlan:
    async def test_the_latest_plan_is_the_plan_result(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(
            rig,
            {
                RpcMethod.GET_LATEST_PLAN: {
                    "latest": "1. read\n2. write",
                    "history": ["old"],
                    "tasks": [],
                    "state": "pending",
                }
            },
        )
        plan = ok(await rig.ask(ext, "get", "plan.latest"))
        assert plan["latest"] == "1. read\n2. write"
        assert plan["history"] == ["old"]
        assert plan["state"] == "pending"

    async def test_no_plan_yet_is_an_empty_result(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        plan = ok(await rig.ask(ext, "get", "plan.latest"))
        assert plan["latest"] == ""
        assert plan["history"] == []

    async def test_a_plan_that_is_not_one_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {RpcMethod.GET_LATEST_PLAN: {"latest": 7, "history": "x"}})
        message = refused(await rig.ask(ext, "get", "plan.latest"), "failed")
        assert "plan.latest" in message

    @pytest.mark.parametrize("member", ["plan.approve", "plan.dismiss"])
    async def test_a_decision_without_a_run_is_about_the_run_igni_names_empty(
        self, rig: HostRig, member: str
    ) -> None:
        """No ``run_id`` is the empty id, which igni reads as the latest plan."""
        ext = rig.install(host=EVERYTHING)
        ok(await rig.ask(ext, "call", member))
        verb = member.split(".")[1]
        assert rig.session.plan_decisions == [(verb, "")]

    @pytest.mark.parametrize(
        ("member", "method"),
        [("plan.approve", RpcMethod.APPROVE_PLAN), ("plan.dismiss", RpcMethod.DISMISS_PLAN)],
    )
    async def test_a_decision_that_is_not_one_is_refused(
        self, rig: HostRig, member: str, method: RpcMethod
    ) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {method: {"run_id": "r1"}})
        message = refused(await rig.ask(ext, "call", member, run_id="r1"), "failed")
        assert member in message


class TestThePolicy:
    async def test_no_group_is_an_empty_pack(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        pack = ok(await rig.ask(ext, "get", "policy.pack"))
        assert pack["group_id"] is None
        assert pack["group_name"] is None

    async def test_the_pack_in_force_names_its_group(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(
            rig,
            {
                RpcMethod.GET_GROUP_POLICY: {
                    "group_id": "g-1",
                    "group_name": "Platform",
                    "fetched_at": "2026-01-02T03:04:05Z",
                }
            },
        )
        pack = ok(await rig.ask(ext, "get", "policy.pack"))
        assert pack["group_id"] == "g-1"
        assert pack["group_name"] == "Platform"
        assert pack["fetched_at"] == "2026-01-02T03:04:05Z"

    async def test_a_pack_that_is_not_one_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING)
        script(rig, {RpcMethod.GET_GROUP_POLICY: ["not", "a", "pack"]})
        message = refused(await rig.ask(ext, "get", "policy.pack"), "failed")
        assert "policy.pack" in message
