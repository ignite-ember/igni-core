"""The wire path a tool event takes to the hooks, and what a hook load hands back.

``HookFirer.fire`` stamps the session onto the payload, runs the event through
the live executor and never lets a hook's failure stop the tool pipeline.
``HookLoadResult.merge`` joins the warnings of two loads and keeps the one
registry both loads fill.
"""

from __future__ import annotations

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.hook_firer import HookFirer
from igni_core.core.hooks.load_result import HookLoadResult
from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.hooks.schemas import (
    HookDefinition,
    HookLoadWarning,
    HookPayload,
    HookResult,
    PermissionDecision,
)
from igni_core.core.hooks.tool_events import PreToolUsePayload


class Recorder(HookExecutor):
    """An executor that records what it is asked and answers what it was given."""

    def __init__(self, answer: HookResult | BaseException) -> None:
        super().__init__({})
        self.answer = answer
        self.calls: list[tuple[str, PreToolUsePayload, str]] = []

    async def execute(self, event, payload, target=""):  # type: ignore[override]
        self.calls.append((event, payload, target))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class TestFiring:
    async def test_the_session_is_stamped_on_the_payload_before_the_event_runs(self):
        executor = Recorder(HookResult(should_continue=True))
        payload = PreToolUsePayload(tool_name="Bash", session_id="somebody else's")

        await HookFirer(executor, "s-1").fire(HookEvent.PRE_TOOL_USE, "Bash", payload)

        assert executor.calls == [("PreToolUse", payload, "Bash")]
        assert payload.session_id == "s-1"

    async def test_an_empty_session_overwrites_too(self):
        executor = Recorder(HookResult(should_continue=True))
        payload = PreToolUsePayload(tool_name="Bash", session_id="old")

        await HookFirer(executor, "").fire(HookEvent.PRE_TOOL_USE, "Bash", payload)

        assert payload.session_id == ""

    async def test_the_merged_result_comes_back_unchanged(self):
        answer = HookResult(
            should_continue=False, message="no", permission_decision=PermissionDecision.DENY
        )

        result = await HookFirer(Recorder(answer), "s").fire(
            HookEvent.PRE_TOOL_USE, "Bash", PreToolUsePayload(tool_name="Bash")
        )

        assert result is answer

    async def test_a_hook_that_fails_does_not_stop_the_pipeline(self):
        result = await HookFirer(Recorder(RuntimeError("flaky")), "s").fire(
            HookEvent.PRE_TOOL_USE, "Bash", PreToolUsePayload(tool_name="Bash")
        )

        assert result.should_continue is True
        assert result.message == ""
        assert result.permission_decision == PermissionDecision.NONE

    async def test_hooks_added_after_the_firer_was_made_are_run(self):
        executor = HookExecutor({})
        firer = HookFirer(executor, "s")
        payload = PreToolUsePayload(tool_name="Bash")

        assert (await firer.fire(HookEvent.PRE_TOOL_USE, "Bash", payload)).should_continue

        executor.hooks["PreToolUse"] = [
            HookDefinition(type="command", command="echo no >&2; exit 2")
        ]

        assert not (await firer.fire(HookEvent.PRE_TOOL_USE, "Bash", payload)).should_continue

    async def test_what_the_executor_hands_the_hooks_carries_the_stamped_session(self):
        seen: list[HookPayload] = []

        class Spy(HookExecutor):
            async def _run_hook(self, hook, event, payload):  # type: ignore[override]
                seen.append(payload)
                return HookResult(should_continue=True)

        executor = Spy({"PreToolUse": [HookDefinition(type="command", command="true")]})

        await HookFirer(executor, "s-9").fire(
            HookEvent.PRE_TOOL_USE, "Bash", PreToolUsePayload(tool_name="Bash")
        )

        assert seen[0].to_wire_dict()["session_id"] == "s-9"


def _warning(text: str) -> HookLoadWarning:
    return HookLoadWarning(source="settings.json", kind="invalid_json", detail=text)


class TestMergingLoads:
    def test_a_result_has_no_warnings_unless_given_some(self):
        assert HookLoadResult(registry=HookRegistry()).warnings == []

    def test_the_warnings_join_with_this_loads_first(self):
        registry = HookRegistry()
        first = HookLoadResult(registry=registry, warnings=[_warning("a"), _warning("b")])
        second = HookLoadResult(registry=HookRegistry(), warnings=[_warning("c")])

        merged = first.merge(second)

        assert [w.detail for w in merged.warnings] == ["a", "b", "c"]

    def test_the_merged_result_keeps_this_loads_registry_itself(self):
        mine, theirs = HookRegistry(), HookRegistry()

        merged = HookLoadResult(registry=mine).merge(HookLoadResult(registry=theirs))

        assert merged.registry is mine

    def test_merging_changes_neither_input(self):
        first = HookLoadResult(registry=HookRegistry(), warnings=[_warning("a")])
        second = HookLoadResult(registry=HookRegistry(), warnings=[_warning("b")])

        merged = first.merge(second)

        assert [w.detail for w in first.warnings] == ["a"]
        assert [w.detail for w in second.warnings] == ["b"]
        assert merged is not first
        assert merged.warnings is not first.warnings
