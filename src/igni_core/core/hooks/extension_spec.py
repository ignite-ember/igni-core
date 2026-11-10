"""The five hook events an extension's answer decides, and what each allows.

An extension may let a deciding event go ahead, stop it with a reason,
or — where the event has something to change — change it. Every other
event is told (:mod:`igni_core.core.hooks.extension_gate`).

**A silence is igni's to decide**, never the extension's: no answer in
time, a process that is not running, an answer the event cannot take.
``CLOSED`` — the event does not happen — for a tool call and for a
message to the model, because nothing should run or reach the model
unchecked by a policy that is meant to check it. ``OPEN`` elsewhere.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.extension_values import (
    HookValue,
    PlanSubmitValue,
    PostToolUseValue,
    PreToolUseValue,
    StopValue,
    UserPromptSubmitValue,
)


class HookFailMode(StrEnum):
    """What a silence means for an event."""

    #: The event does not happen.
    CLOSED = "closed"
    #: The event happens as if that extension had let it.
    OPEN = "open"


class ExtensionHookSpec(BaseModel):
    """One deciding event: what an extension is handed, may change, and what a reject does."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    event: HookEvent
    value: type[HookValue]
    #: The fields of the value a change may touch.
    modify: tuple[str, ...] = ()
    fails: HookFailMode = HookFailMode.OPEN
    #: What a reject does, in a sentence.
    on_reject: str


#: The events an extension's answer decides. Every other event is told.
DECIDING: dict[HookEvent, ExtensionHookSpec] = {
    spec.event: spec
    for spec in (
        ExtensionHookSpec(
            event=HookEvent.PRE_TOOL_USE,
            value=PreToolUseValue,
            modify=("tool_args",),
            fails=HookFailMode.CLOSED,
            on_reject="The tool does not run; the agent is told why, as for igni's own refusals.",
        ),
        ExtensionHookSpec(
            event=HookEvent.POST_TOOL_USE,
            value=PostToolUseValue,
            modify=("result",),
            fails=HookFailMode.OPEN,
            on_reject="The model is not shown the result, only that it was withheld and why.",
        ),
        ExtensionHookSpec(
            event=HookEvent.USER_PROMPT_SUBMIT,
            value=UserPromptSubmitValue,
            modify=("message",),
            fails=HookFailMode.CLOSED,
            on_reject="The run does not start; the reason is shown as an error.",
        ),
        ExtensionHookSpec(
            event=HookEvent.STOP,
            value=StopValue,
            fails=HookFailMode.OPEN,
            on_reject=(
                "As a Stop hook's block: where igni can send the agent back (a turn "
                "run through the session), it revises its answer with the reason, up "
                "to three times; elsewhere the reason is shown after the run."
            ),
        ),
        ExtensionHookSpec(
            event=HookEvent.PLAN_SUBMIT,
            value=PlanSubmitValue,
            modify=("plan",),
            fails=HookFailMode.OPEN,
            on_reject="The plan goes back to the agent with the reason; it may submit again.",
        ),
    )
}


__all__ = ["DECIDING", "ExtensionHookSpec", "HookFailMode"]
