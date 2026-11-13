"""What an extension's answer means for the hook event it was asked about.

Read through :class:`HookAnswers` itself: an allow goes ahead with the
value as it is, a reject stops it with the extension's reason, a change
goes ahead with only what the event lets it change, and an answer the
event cannot take is the extension's bug (not a silence) while a handler
that raised is a silence.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.extension_spec import DECIDING
from igni_core.core.hooks.extension_values import (
    PlanSubmitValue,
    PreToolUseValue,
    StopValue,
)
from igni_core.extensions.host.answers import HookAnswers
from igni_core.extensions.host.hook_exchange import HookFailure
from igni_core.extensions.host.wire import HookAnswer, Outcome

PRE_TOOL = DECIDING[HookEvent.PRE_TOOL_USE]
STOP = DECIDING[HookEvent.STOP]
PLAN = DECIDING[HookEvent.PLAN_SUBMIT]

CALL = PreToolUseValue(session_id="s1", tool_name="shell", tool_args={"cmd": "ls"})


def answer(outcome: Outcome, *, reason: str = "", value: Any = None) -> HookAnswer:
    return HookAnswer(hook="h-1", outcome=outcome, reason=reason, value=value)


def test_an_allow_goes_ahead_with_the_value_as_it_is() -> None:
    verdict = HookAnswers.verdict(PRE_TOOL, "acme.gate", CALL, answer(Outcome.ALLOW))

    assert verdict.allowed
    assert verdict.value == CALL


def test_a_reject_stops_the_event_with_its_reason_and_names_the_extension() -> None:
    verdict = HookAnswers.verdict(
        PRE_TOOL, "acme.gate", CALL, answer(Outcome.REJECT, reason="  not on a Friday \n")
    )

    assert not verdict.allowed
    assert verdict.by == "acme.gate"
    assert verdict.reason == "not on a Friday (the extension acme.gate)"
    assert verdict.value == CALL


@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_a_reject_that_says_nothing_is_the_extensions_bug_not_a_silence(reason: str) -> None:
    with pytest.raises(HookFailure, match="rejected without saying why") as caught:
        HookAnswers.verdict(PRE_TOOL, "acme.gate", CALL, answer(Outcome.REJECT, reason=reason))

    assert not caught.value.unanswered


def test_a_handler_that_raised_is_a_silence_with_its_reason() -> None:
    with pytest.raises(HookFailure, match="it failed: boom") as caught:
        HookAnswers.verdict(PRE_TOOL, "acme.gate", CALL, answer(Outcome.FAILED, reason="boom"))

    assert caught.value.unanswered


def test_a_failure_with_no_reason_says_none_was_given() -> None:
    with pytest.raises(HookFailure) as caught:
        HookAnswers.verdict(PRE_TOOL, "acme.gate", CALL, answer(Outcome.FAILED))

    assert str(caught.value) == "it failed: no reason given"
    assert caught.value.unanswered


def test_a_change_applies_the_fields_the_event_lets_it_change() -> None:
    changed = {"session_id": "s1", "tool_name": "shell", "tool_args": {"cmd": "pwd"}}

    verdict = HookAnswers.verdict(
        PRE_TOOL, "acme.gate", CALL, answer(Outcome.MODIFY, value=changed)
    )

    assert verdict.allowed
    assert isinstance(verdict.value, PreToolUseValue)
    assert verdict.value.tool_args == {"cmd": "pwd"}
    assert verdict.value.tool_name == "shell"


def test_a_change_of_nothing_is_allowed_and_leaves_the_value() -> None:
    verdict = HookAnswers.verdict(PRE_TOOL, "acme.gate", CALL, answer(Outcome.MODIFY, value={}))

    assert verdict.allowed
    assert verdict.value == CALL


def test_a_change_echoing_a_field_it_may_not_change_is_tolerated() -> None:
    echoed = {"tool_name": "shell", "session_id": "s1", "tool_args": {"cmd": "pwd"}}

    verdict = HookAnswers.verdict(PRE_TOOL, "acme.gate", CALL, answer(Outcome.MODIFY, value=echoed))

    assert verdict.value.tool_name == "shell"  # type: ignore[attr-defined]


def test_a_change_echoing_the_fields_the_host_added_is_tolerated() -> None:
    echoed = {"tool_manifest_name": "acme_shell", "tool_args": {"cmd": "pwd"}}

    verdict = HookAnswers.verdict(PRE_TOOL, "acme.gate", CALL, answer(Outcome.MODIFY, value=echoed))

    assert verdict.allowed
    assert verdict.value.tool_args == {"cmd": "pwd"}  # type: ignore[attr-defined]


def test_a_change_to_a_field_the_event_keeps_names_every_stray_one_sorted() -> None:
    stray = {"tool_name": "rm", "session_id": "other", "tool_args": {}}

    with pytest.raises(HookFailure) as caught:
        HookAnswers.verdict(PRE_TOOL, "acme.gate", CALL, answer(Outcome.MODIFY, value=stray))

    assert str(caught.value) == "it changed session_id, tool_name, which cannot be changed"
    assert not caught.value.unanswered


def test_a_change_on_an_event_with_nothing_to_change_is_refused() -> None:
    with pytest.raises(HookFailure, match="it changed Stop, which has nothing to change") as caught:
        HookAnswers.verdict(
            STOP, "acme.gate", StopValue(response="done"), answer(Outcome.MODIFY, value={})
        )

    assert not caught.value.unanswered


@pytest.mark.parametrize("value", [None, "text", ["tool_args"], 3])
def test_a_change_that_is_not_an_object_is_refused(value: Any) -> None:
    with pytest.raises(HookFailure, match="not the event's value") as caught:
        HookAnswers.verdict(PRE_TOOL, "acme.gate", CALL, answer(Outcome.MODIFY, value=value))

    assert not caught.value.unanswered


def test_a_change_that_does_not_fit_the_event_is_refused_with_why() -> None:
    plan = PlanSubmitValue(plan="# Plan")

    with pytest.raises(HookFailure, match="its change does not fit PlanSubmit") as caught:
        HookAnswers.verdict(PLAN, "acme.gate", plan, answer(Outcome.MODIFY, value={"plan": 7}))

    assert not caught.value.unanswered
