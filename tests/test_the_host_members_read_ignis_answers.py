"""The host members read igni's answers through the models that made them.

A member does not hand an extension whatever igni answered: it reads the
answer back through the model that produced it, and says so when the
answer does not fit. A field igni renames is fixed once, in that model,
and the extension sees the model's field — never a half-read answer.

These drive the members over the real rig: the real RPC tables, the
real host model, stand-in sessions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from igni_core.core.session.schemas import LoopAdvance
from igni_core.extensions.host.definition import HOST_SCHEMA
from tests.host_model_helpers import HostRig

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


class TestTheModelRegistryRow:
    async def test_the_key_is_not_published(self, rig: HostRig) -> None:
        """The row says whether the entry has a key, and never the key,
        nor where it comes from."""
        ext = rig.install(host=EVERYTHING, workspace="read")
        registry = ok(await rig.ask(ext, "get", "models.registry"))
        large = registry["registry"]["large"]
        assert "api_key" not in large
        assert "api_key_cmd" not in large
        assert "api_key_env" not in large
        assert large["has_api_key"] is True
        assert "sk-secret" not in str(registry)
        assert "LARGE_KEY" not in str(registry)

    async def test_an_unknown_model_is_refused(self, rig: HostRig) -> None:
        """A model igni does not have is refused by name, not switched to."""
        ext = rig.install(host=EVERYTHING, workspace="read")
        message = refused(await rig.ask(ext, "set", "models.current", to="gigantic"), "refused")
        assert "gigantic" in message
        assert ok(await rig.ask(ext, "get", "models.current")) == "small"

    async def test_a_configured_model_is_switched_to(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING, workspace="read")
        ok(await rig.ask(ext, "set", "models.current", to="large"))
        assert ok(await rig.ask(ext, "get", "models.current")) == "large"


class TestThePermissionLevel:
    async def test_a_level_igni_knows_is_answered(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING, workspace="read")
        rig.session.permissions.level = "ask"
        answer = ok(
            await rig.ask(ext, "call", "permissions.check", tool_name="Bash", func_name="bash")
        )
        assert answer == "ask"

    async def test_a_level_that_is_not_one_is_refused(self, rig: HostRig) -> None:
        """A fourth level is not a level: it is refused by name."""
        ext = rig.install(host=EVERYTHING, workspace="read")
        rig.session.permissions.level = "sometimes"
        message = refused(
            await rig.ask(ext, "call", "permissions.check", tool_name="Bash", func_name="bash"),
            "invalid",
        )
        assert "sometimes" in message
        assert "allow" in message and "deny" in message

    async def test_a_rule_is_saved_under_its_level(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING, workspace="read")
        ok(await rig.ask(ext, "call", "permissions.save_rule", rule="Bash(*)", level="deny"))
        assert rig.session.permissions.rules == [("Bash(*)", "deny")]

    async def test_a_rule_is_not_saved_under_a_level_that_is_not_one(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING, workspace="read")
        refused(
            await rig.ask(ext, "call", "permissions.save_rule", rule="Bash(*)", level="perhaps"),
            "invalid",
        )
        assert rig.session.permissions.rules == []


class TestTheCommands:
    async def test_a_command_that_is_not_one_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING, workspace="read")
        message = refused(await rig.ask(ext, "call", "commands.run", command="hello"), "invalid")
        assert "slash command" in message
        assert rig.session.commands == []

    async def test_the_commands_are_one_shape(self, rig: HostRig) -> None:
        """Whatever kind a command is, the extension sees the same fields."""
        ext = rig.install(host=EVERYTHING, workspace="read")
        [command] = ok(await rig.ask(ext, "get", "commands.list"))
        assert command["source"] == "builtin"
        assert command["name"] == "/ctx"
        assert command["description"] == "context"
        assert command["argument_hint"] == ""


class TestTheTasks:
    async def test_a_task_carries_the_names_the_model_goes_out_under(self, rig: HostRig) -> None:
        """The todo row is camelCase on the wire — the model's own alias."""
        ext = rig.install(host=EVERYTHING, workspace="read")
        [todo] = ok(await rig.ask(ext, "get", "tasks.todos"))
        assert todo["content"] == "write tests"
        assert todo["status"] == "pending"
        assert todo["activeForm"] == "Writing tests"


class TestTheLoop:
    async def test_nothing_waiting_is_an_answer(self, rig: HostRig) -> None:
        """No iteration waiting is ``None``, not a gap."""
        ext = rig.install(host=EVERYTHING, workspace="read")
        assert ok(await rig.ask(ext, "call", "loop.pop_pending")) is None

    async def test_the_waiting_iteration_is_answered(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING, workspace="read")
        rig.session.loop.pending = LoopAdvance(kind="step", prompt="again", iteration=2)
        advance = ok(await rig.ask(ext, "call", "loop.pop_pending"))
        assert advance["prompt"] == "again"
        assert ok(await rig.ask(ext, "call", "loop.pop_pending")) is None


class TestTheScheduler:
    async def test_a_task_is_run_by_what_it_carries(self, rig: HostRig) -> None:
        """The scheduler runs a description, so the member finds the task
        first and passes on what it carries."""
        ext = rig.install(host=EVERYTHING, workspace="read")
        assert ok(await rig.ask(ext, "call", "scheduler.run_now", task_id="t1")) == "ran nightly"
        assert rig.session.loop.scheduler.executed == ["nightly"]

    async def test_a_task_that_is_not_there_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING, workspace="read")
        message = refused(
            await rig.ask(ext, "call", "scheduler.run_now", task_id="t9"), "not_found"
        )
        assert "t9" in message
        assert rig.session.loop.scheduler.executed == []


class TestTheAgents:
    async def test_a_target_that_is_not_one_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING, workspace="read")
        message = refused(
            await rig.ask(ext, "call", "agents.promote_ephemeral", name="a", target="elsewhere"),
            "invalid",
        )
        assert "elsewhere" in message
        assert rig.session.promoted == []

    async def test_a_promoted_agent_says_where_it_went(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING, workspace="read")
        result = ok(await rig.ask(ext, "call", "agents.promote_ephemeral", name="a"))
        assert result["ok"] is True
        assert result["name"] == "a"
        assert result["dest"] == ".igni/agents/a.md"


class TestThePlan:
    async def test_an_approved_plan_says_what_was_decided(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING, workspace="read")
        decision = ok(await rig.ask(ext, "call", "plan.approve", run_id="r1"))
        assert decision["run_id"] == "r1"
        assert decision["decision"] == "approved"
        assert decision["ok"] is True
        assert rig.session.plan_decisions == [("approve", "r1")]

    async def test_a_dismissed_plan_says_what_was_decided(self, rig: HostRig) -> None:
        ext = rig.install(host=EVERYTHING, workspace="read")
        decision = ok(await rig.ask(ext, "call", "plan.dismiss", run_id="r2"))
        assert decision["decision"] == "dismissed"
        assert rig.session.plan_decisions == [("dismiss", "r2")]


class TestAnAnswerThatDoesNotFit:
    async def test_it_is_refused_rather_than_handed_on(self, rig: HostRig) -> None:
        """An answer the model does not describe is said so, by field."""
        ext = rig.install(host=EVERYTHING, workspace="read")
        rig.session.get_mcp_servers = lambda: [{"name": "github"}]  # type: ignore[method-assign]
        message = refused(await rig.ask(ext, "get", "mcp.servers"), "failed")
        assert "mcp.servers" in message
        assert "connected" in message
