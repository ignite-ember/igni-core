"""The seam the core asks extensions through, the five events they decide, and their values.

Nothing installed means nobody objects. Only the five deciding events are asked;
every other event is told, and only to an extension that hooks it. The values an
extension is handed name their event and refuse a field they do not have.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.extension_gate import (
    ExtensionHookGate,
    ExtensionHookVerdict,
    extension_hook_gate,
)
from igni_core.core.hooks.extension_spec import DECIDING, HookFailMode
from igni_core.core.hooks.extension_values import (
    HookValue,
    PlanSubmitValue,
    PostToolUseValue,
    PreToolUseValue,
    StopValue,
    UserPromptSubmitValue,
)
from igni_core.core.hooks.schemas import HookPayload


class Body(BaseModel):
    session_id: str = "s"


class Answerer:
    """What the host would install: hooks the events it is given, records the rest."""

    def __init__(self, hooked: set[HookEvent] | None = None, verdict: Any = None) -> None:
        self.hooked = hooked if hooked is not None else set(HookEvent)
        self.verdict = verdict
        self.asked: list[HookValue] = []
        self.told: list[tuple[HookEvent, HookPayload]] = []
        self.tools: list[tuple[str, bool, str]] = []

    def hooks(self, event: HookEvent) -> bool:
        return event in self.hooked

    async def ask(self, value: Any) -> Any:
        self.asked.append(value)
        return self.verdict

    def tell(self, event: HookEvent, payload: HookPayload) -> None:
        self.told.append((event, payload))

    def tool_called(self, tool_name: str, *, answered: bool, detail: str) -> None:
        self.tools.append((tool_name, answered, detail))


PAYLOAD = HookPayload(body=Body())


class TestAsking:
    async def test_with_nothing_installed_a_deciding_event_goes_ahead_unchanged(self):
        value = StopValue(response="done")

        verdict = await ExtensionHookGate().ask(value)

        assert verdict.allowed is True
        assert verdict.value is value
        assert (verdict.reason, verdict.by) == ("", "")

    async def test_when_no_extension_hooks_the_event_it_goes_ahead_unasked(self):
        answerer = Answerer(hooked={HookEvent.STOP})
        gate = ExtensionHookGate()
        gate.install(answerer)
        value = UserPromptSubmitValue(message="hi")

        verdict = await gate.ask(value)

        assert verdict.allowed is True
        assert verdict.value is value
        assert answerer.asked == []

    async def test_when_an_extension_hooks_it_the_answerers_verdict_is_the_answer(self):
        value = PreToolUseValue(tool_name="Bash")
        theirs = ExtensionHookVerdict(allowed=False, value=value, reason="no", by="policy")
        answerer = Answerer(verdict=theirs)
        gate = ExtensionHookGate()
        gate.install(answerer)

        verdict = await gate.ask(value)

        assert verdict is theirs
        assert answerer.asked == [value]

    async def test_removing_the_answerer_lets_everything_through_again(self):
        value = StopValue()
        gate = ExtensionHookGate()
        gate.install(Answerer(verdict=ExtensionHookVerdict(allowed=False, value=value)))
        gate.install(None)

        assert (await gate.ask(value)).allowed is True
        assert gate.hooked(HookEvent.STOP) is False

    def test_a_verdict_cannot_be_changed_once_made(self):
        verdict = ExtensionHookVerdict(allowed=True, value=StopValue())

        with pytest.raises(ValidationError):
            verdict.allowed = False  # type: ignore[misc]


class TestHooked:
    def test_nothing_is_hooked_with_no_answerer(self):
        assert ExtensionHookGate().hooked(HookEvent.PRE_TOOL_USE) is False

    def test_it_is_whatever_the_answerer_says_per_event(self):
        gate = ExtensionHookGate()
        gate.install(Answerer(hooked={HookEvent.STOP}))

        assert gate.hooked(HookEvent.STOP) is True
        assert gate.hooked(HookEvent.SESSION_START) is False


class TestTelling:
    def test_an_event_that_decides_nothing_is_told_to_the_answerer(self):
        answerer = Answerer()
        gate = ExtensionHookGate()
        gate.install(answerer)

        gate.tell("SessionStart", PAYLOAD)

        assert answerer.told == [(HookEvent.SESSION_START, PAYLOAD)]

    @pytest.mark.parametrize("event", [e.value for e in DECIDING])
    def test_a_deciding_event_is_never_told(self, event: str):
        answerer = Answerer()
        gate = ExtensionHookGate()
        gate.install(answerer)

        gate.tell(event, PAYLOAD)

        assert answerer.told == []

    def test_an_event_nobody_hooks_is_not_told(self):
        answerer = Answerer(hooked=set())
        gate = ExtensionHookGate()
        gate.install(answerer)

        gate.tell("SessionStart", PAYLOAD)

        assert answerer.told == []

    @pytest.mark.parametrize("event", ["", "NotAnEvent", "sessionstart"])
    def test_a_name_that_is_not_an_event_is_ignored(self, event: str):
        answerer = Answerer()
        gate = ExtensionHookGate()
        gate.install(answerer)

        gate.tell(event, PAYLOAD)

        assert answerer.told == []

    def test_telling_with_nothing_installed_does_nothing(self):
        ExtensionHookGate().tell("SessionStart", PAYLOAD)


class TestToolCalled:
    def test_the_answerer_hears_whether_the_tool_answered_and_why_not(self):
        answerer = Answerer()
        gate = ExtensionHookGate()
        gate.install(answerer)

        gate.tool_called("fs_read", answered=False, detail="timed out")
        gate.tool_called("fs_read", answered=True)

        assert answerer.tools == [("fs_read", False, "timed out"), ("fs_read", True, "")]

    def test_with_nothing_installed_it_is_nobodys_business(self):
        ExtensionHookGate().tool_called("fs_read", answered=False)


def test_the_process_has_one_gate_and_it_starts_with_nobody_answering():
    assert isinstance(extension_hook_gate, ExtensionHookGate)
    assert extension_hook_gate.hooked(HookEvent.PRE_TOOL_USE) is False


class TestTheDecidingEvents:
    def test_exactly_five_events_decide(self):
        assert set(DECIDING) == {
            HookEvent.PRE_TOOL_USE,
            HookEvent.POST_TOOL_USE,
            HookEvent.USER_PROMPT_SUBMIT,
            HookEvent.STOP,
            HookEvent.PLAN_SUBMIT,
        }

    @pytest.mark.parametrize(
        ("event", "value", "modify", "fails"),
        [
            (HookEvent.PRE_TOOL_USE, PreToolUseValue, ("tool_args",), HookFailMode.CLOSED),
            (HookEvent.POST_TOOL_USE, PostToolUseValue, ("result",), HookFailMode.OPEN),
            (
                HookEvent.USER_PROMPT_SUBMIT,
                UserPromptSubmitValue,
                ("message",),
                HookFailMode.CLOSED,
            ),
            (HookEvent.STOP, StopValue, (), HookFailMode.OPEN),
            (HookEvent.PLAN_SUBMIT, PlanSubmitValue, ("plan",), HookFailMode.OPEN),
        ],
    )
    def test_what_each_hands_over_may_change_and_means_by_a_silence(
        self, event: HookEvent, value: type[HookValue], modify: tuple[str, ...], fails: HookFailMode
    ):
        spec = DECIDING[event]

        assert spec.event is event
        assert spec.value is value
        assert value.hook_event is event
        assert spec.modify == modify
        assert spec.fails is fails
        assert spec.on_reject

    def test_every_field_that_may_change_is_a_field_of_the_value(self):
        for spec in DECIDING.values():
            assert set(spec.modify) <= set(spec.value.model_fields)

    def test_a_spec_cannot_be_changed(self):
        with pytest.raises(ValidationError):
            DECIDING[HookEvent.STOP].modify = ("response",)  # type: ignore[misc]

    def test_the_fail_modes_read_closed_and_open(self):
        assert (HookFailMode.CLOSED.value, HookFailMode.OPEN.value) == ("closed", "open")


class TestTheValues:
    @pytest.mark.parametrize(
        "value",
        [PreToolUseValue, PostToolUseValue, UserPromptSubmitValue, StopValue, PlanSubmitValue],
    )
    def test_an_unknown_field_is_refused(self, value: type[HookValue]):
        required: dict[str, Any] = {
            "tool_name": "Bash",
            "result": "r",
            "message": "m",
            "plan": "p",
        }
        fields = {k: v for k, v in required.items() if k in value.model_fields}

        with pytest.raises(ValidationError):
            value(**fields, surprise=1)

    @pytest.mark.parametrize(
        ("value", "missing"),
        [
            (PreToolUseValue, "tool_name"),
            (PostToolUseValue, "result"),
            (UserPromptSubmitValue, "message"),
            (PlanSubmitValue, "plan"),
        ],
    )
    def test_the_field_an_event_is_about_is_required(self, value: type[HookValue], missing: str):
        with pytest.raises(ValidationError, match=missing):
            value()

    def test_the_defaults(self):
        assert PreToolUseValue(tool_name="t").model_dump() == {
            "session_id": "",
            "tool_name": "t",
            "tool_args": {},
        }
        assert PostToolUseValue(tool_name="t", result="r").tool_args == {}
        assert StopValue().response == ""
        assert StopValue().session_id == ""
        plan = PlanSubmitValue(plan="p")
        assert (plan.tasks, plan.attempt) == ([], 0)

    def test_a_plans_tasks_may_be_any_json_and_nothing_is_clipped(self):
        tasks: list[Any] = [{"title": "a", "n": 1}, "text", 3, None, ["x"]]
        big = "é" * 100_000

        plan = PlanSubmitValue(plan=big, tasks=tasks, attempt=2)

        assert plan.tasks == tasks
        assert plan.plan == big
        assert plan.attempt == 2

    def test_a_result_must_be_text(self):
        with pytest.raises(ValidationError):
            PostToolUseValue(tool_name="t", result={"a": 1})  # type: ignore[arg-type]
