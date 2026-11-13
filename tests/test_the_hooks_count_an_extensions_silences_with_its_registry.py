"""``ExtensionHooks``: who is asked, in what order, and what silence costs.

The hooks are one instance over one registry. How often an extension failed
to answer is that registry's count (``registry.faults``), shared with the
tool calls the agent makes of the same extension and with no other
registry; the deadline is igni's, the order the super admin's.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.extension_values import PreToolUseValue
from igni_core.core.hooks.schemas import HookPayload
from igni_core.core.mcp.naming import tool_name_prefix
from igni_core.extensions.faults import ExtensionFaults
from igni_core.extensions.host.hook_exchange import HookFailure
from igni_core.extensions.host.hooks import ExtensionHooks
from igni_core.extensions.host.wire import HookAnswer
from igni_core.extensions.install_service import server_name
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from tests.host_model_helpers import manifest

CALL = PreToolUseValue(session_id="s-1", tool_name="run_shell_command", tool_args={"command": "ls"})


def install(registry: ExtensionRegistry, extension_id: str, *events: str, **kwargs: Any) -> None:
    registry.install(manifest(extension_id, hooks=list(events)), **kwargs)


def tool_of(extension_id: str, bare: str = "lookup") -> str:
    return f"{tool_name_prefix(server_name(extension_id))}_{bare}"


class Frames:
    """A process's socket: keeps the frames, answers none."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def __call__(self, frame: str) -> None:
        self.frames.append(json.loads(frame))


class Body(BaseModel):
    kept: str
    left_out: str | None = None


class TestDeadline:
    async def test_the_deadline_is_igni_s_two_seconds_unless_the_builder_gives_another(
        self,
    ) -> None:
        assert ExtensionHooks.TIMEOUT_S == 2.0

    async def test_a_silent_extension_is_waited_for_as_long_as_the_hooks_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ExtensionHooks, "TIMEOUT_S", 0.02)
        registry = ExtensionRegistry()
        install(registry, "acme.a", "PreToolUse")
        hooks = ExtensionHooks(registry=registry)
        hooks.connect("acme.a", Frames())
        verdict = await hooks.ask(CALL)
        assert not verdict.allowed
        assert "could not answer (no answer within 0.02s)" in verdict.reason

    async def test_the_builders_deadline_wins_over_the_default(self) -> None:
        registry = ExtensionRegistry()
        install(registry, "acme.a", "PreToolUse")
        hooks = ExtensionHooks(registry=registry, timeout=0.03)
        hooks.connect("acme.a", Frames())
        verdict = await hooks.ask(CALL)
        assert "no answer within 0.03s" in verdict.reason


class TestWhoHooksWhat:
    def test_listed_extensions_come_first_in_the_super_admins_order_then_the_rest_by_id(
        self,
    ) -> None:
        registry = ExtensionRegistry()
        for extension_id in ("acme.d", "acme.c", "acme.b", "acme.a"):
            install(registry, extension_id, "PreToolUse")
        hooks = ExtensionHooks(registry=registry)
        hooks.set_priority(["acme.c", "acme.gone", "acme.a"])
        assert hooks.hookers(HookEvent.PRE_TOOL_USE) == ["acme.c", "acme.a", "acme.b", "acme.d"]

    def test_a_changed_priority_replaces_the_earlier_one(self) -> None:
        registry = ExtensionRegistry()
        install(registry, "acme.a", "PreToolUse")
        install(registry, "acme.b", "PreToolUse")
        hooks = ExtensionHooks(registry=registry)
        hooks.set_priority(["acme.b"])
        hooks.set_priority(["acme.a"])
        assert hooks.hookers(HookEvent.PRE_TOOL_USE) == ["acme.a", "acme.b"]

    def test_only_an_extension_that_hooks_the_event_and_is_not_disabled_counts(self) -> None:
        registry = ExtensionRegistry()
        install(registry, "acme.on", "PreToolUse")
        install(registry, "acme.off", "PreToolUse", state=ExtensionState.DISABLED)
        install(registry, "acme.broken", "PreToolUse", state=ExtensionState.FAILED)
        install(registry, "acme.other", "SessionStart")
        hooks = ExtensionHooks(registry=registry)
        assert hooks.hookers(HookEvent.PRE_TOOL_USE) == ["acme.broken", "acme.on"]
        assert hooks.hooks(HookEvent.PRE_TOOL_USE) is True
        assert hooks.hooks(HookEvent.STOP) is False


class TestFrames:
    async def test_a_told_event_leaves_out_what_is_unset_and_an_asked_one_does_not(self) -> None:
        registry = ExtensionRegistry()
        install(registry, "acme.a", "SessionStart")
        hooks = ExtensionHooks(registry=registry, timeout=0.02)
        socket = Frames()
        hooks.connect("acme.a", socket)
        hooks.tell(HookEvent.SESSION_START, HookPayload(body=Body(kept="x")))
        with pytest.raises(HookFailure):
            await hooks.ask_one("acme.a", "ServiceEvent", Body(kept="x"), timeout=0.02)
        told, asked = socket.frames
        assert told["id"] is None and told["event"] == "SessionStart"
        assert told["value"] == {"kept": "x"}
        assert asked["id"] == "h-1" and asked["event"] == "ServiceEvent"
        assert asked["value"] == {"kept": "x", "left_out": None}

    async def test_an_extension_that_is_not_connected_is_not_told_and_nothing_raises(
        self,
    ) -> None:
        registry = ExtensionRegistry()
        install(registry, "acme.a", "SessionStart")
        hooks = ExtensionHooks(registry=registry)
        hooks.tell(HookEvent.SESSION_START, HookPayload(body=Body(kept="x")))

    async def test_a_frame_names_the_manifest_name_of_the_tool_it_is_about(self) -> None:
        registry = ExtensionRegistry()
        install(registry, "acme.a", "PreToolUse")
        hooks = ExtensionHooks(registry=registry, timeout=0.02)
        socket = Frames()
        hooks.connect("acme.a", socket)
        await hooks.ask(
            PreToolUseValue(session_id="s", tool_name=tool_of("acme.a", "lookup_ticket"))
        )
        [frame] = socket.frames
        assert frame["value"]["tool_manifest_name"] == "acme.a.lookup_ticket"

    async def test_a_deciding_hook_that_is_answered_is_given_the_ask_ones_answer(self) -> None:
        registry = ExtensionRegistry()
        install(registry, "acme.a", "PreToolUse")
        hooks = ExtensionHooks(registry=registry)

        def send(frame: str) -> None:
            hooks.answered("acme.a", HookAnswer(hook=json.loads(frame)["id"], outcome="allow"))

        hooks.connect("acme.a", send)
        answer = await hooks.ask_one("acme.a", "ServiceEvent", Body(kept="x"), timeout=1)
        assert answer.hook == "h-1" and answer.outcome == "allow"


class TestTheCountBelongsToTheRegistry:
    def test_the_hooks_use_the_counter_their_registry_keeps(self) -> None:
        registry = ExtensionRegistry()
        assert isinstance(registry.faults, ExtensionFaults)
        install(registry, "acme.a")
        hooks = ExtensionHooks(registry=registry)
        for _ in range(ExtensionFaults.LIMIT):
            hooks.tool_called(tool_of("acme.a"), answered=False, detail="boom")
        entry = registry.get("acme.a")
        assert entry is not None and entry.faulty
        assert registry.faults.faulty("acme.a")

    async def test_hook_silences_and_tool_failures_of_one_extension_add_up(self) -> None:
        registry = ExtensionRegistry()
        install(registry, "acme.a", "PreToolUse")
        hooks = ExtensionHooks(registry=registry, timeout=0.01)
        hooks.connect("acme.a", Frames())
        await hooks.ask(CALL)
        await hooks.ask(CALL)
        assert not registry.faults.faulty("acme.a")
        hooks.tool_called(tool_of("acme.a"), answered=False, detail="timed out")
        assert registry.faults.faulty("acme.a")

    async def test_another_registry_does_not_share_the_count(self) -> None:
        one, two = ExtensionRegistry(), ExtensionRegistry()
        install(one, "acme.a", "PreToolUse")
        install(two, "acme.a", "PreToolUse")
        hooks_one = ExtensionHooks(registry=one)
        hooks_two = ExtensionHooks(registry=two)
        for _ in range(ExtensionFaults.LIMIT):
            hooks_one.tool_called(tool_of("acme.a"), answered=False, detail="boom")
        assert one.faults.faulty("acme.a")
        assert not two.faults.faulty("acme.a")
        assert hooks_two.hooks(HookEvent.PRE_TOOL_USE)


class TestToolCalled:
    def test_a_failed_tool_call_says_which_tool_and_why_in_the_faulty_reason(self) -> None:
        registry = ExtensionRegistry()
        install(registry, "acme.a")
        hooks = ExtensionHooks(registry=registry)
        name = tool_of("acme.a", "lookup")
        hooks.tool_called(name, answered=False, detail="timed out")
        hooks.tool_called(name, answered=False, detail="timed out")
        hooks.tool_called(name, answered=False, detail="")
        entry = registry.get("acme.a")
        assert entry is not None
        assert f"(the last, its tool {name}: no answer)" in entry.faulty

    def test_an_answer_even_an_error_one_starts_the_count_again(self) -> None:
        registry = ExtensionRegistry()
        install(registry, "acme.a")
        hooks = ExtensionHooks(registry=registry)
        name = tool_of("acme.a")
        for _ in range(ExtensionFaults.LIMIT - 1):
            hooks.tool_called(name, answered=False, detail="x")
        hooks.tool_called(name, answered=True, detail="")
        for _ in range(ExtensionFaults.LIMIT - 1):
            hooks.tool_called(name, answered=False, detail="x")
        assert not registry.faults.faulty("acme.a")

    def test_the_first_answer_after_the_limit_clears_the_mark(self) -> None:
        registry = ExtensionRegistry()
        install(registry, "acme.a")
        hooks = ExtensionHooks(registry=registry)
        name = tool_of("acme.a")
        for _ in range(ExtensionFaults.LIMIT):
            hooks.tool_called(name, answered=False, detail="x")
        assert registry.faults.faulty("acme.a")
        hooks.tool_called(name, answered=True, detail="")
        assert not registry.faults.faulty("acme.a")

    def test_a_tool_that_is_no_extensions_counts_against_nobody(self) -> None:
        registry = ExtensionRegistry()
        install(registry, "acme.a")
        hooks = ExtensionHooks(registry=registry)
        for _ in range(ExtensionFaults.LIMIT * 2):
            hooks.tool_called("run_shell_command", answered=False, detail="x")
            hooks.tool_called("mcp_other_server_tool", answered=False, detail="x")
        assert not registry.faults.faulty("acme.a")
