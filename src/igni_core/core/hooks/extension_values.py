"""What an extension is handed, per deciding hook event.

Each value names the event it is for (:attr:`HookValue.hook_event`), so
asking with one is asking about that event
(:mod:`igni_core.core.hooks.extension_gate`). The values are the real
ones, never clipped.
"""

from __future__ import annotations

from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from igni_core.core.hooks.events import HookEvent


class HookValue(BaseModel):
    """What an extension is handed. The values are the real ones, never clipped."""

    model_config = ConfigDict(extra="forbid")

    #: The deciding event this value is asked about.
    hook_event: ClassVar[HookEvent]

    session_id: str = ""


class PreToolUseValue(HookValue):
    """A tool the agent is about to run, before igni's own checks."""

    hook_event: ClassVar[HookEvent] = HookEvent.PRE_TOOL_USE

    tool_name: str
    #: Its arguments. A change is what igni's checks, and the tool, then see.
    tool_args: dict[str, Any] = Field(default_factory=dict)


class PostToolUseValue(HookValue):
    """A tool that ran, before the model reads what it returned."""

    hook_event: ClassVar[HookEvent] = HookEvent.POST_TOOL_USE

    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    #: What it returned, as text. A change is what the model reads.
    result: str


class UserPromptSubmitValue(HookValue):
    """A message about to start a run."""

    hook_event: ClassVar[HookEvent] = HookEvent.USER_PROMPT_SUBMIT

    #: What the agent will be sent. A change is what it is sent.
    message: str


class StopValue(HookValue):
    """The agent wants to end its turn."""

    hook_event: ClassVar[HookEvent] = HookEvent.STOP

    #: Its answer, when the place it stops has one; empty otherwise.
    response: str = ""


class PlanSubmitValue(HookValue):
    """A plan the agent is about to put to the person (``exit_plan_mode``)."""

    hook_event: ClassVar[HookEvent] = HookEvent.PLAN_SUBMIT

    #: The plan, in markdown. A change is what the person sees.
    plan: str
    #: The tasks the agent suggested with it, as they will be
    #: committed — the rows the plan tool validated. The extension
    #: sees them; the ``PlanSubmit`` spec does not let it change them.
    tasks: list[JsonValue] = Field(default_factory=list)
    #: Plans rejected since the agent entered plan mode: 0 for the first.
    attempt: int = 0


#: A deciding event's value: what it is asked with, and what it goes ahead with.
HookValueT = TypeVar("HookValueT", bound=HookValue)


__all__ = [
    "HookValue",
    "HookValueT",
    "PlanSubmitValue",
    "PostToolUseValue",
    "PreToolUseValue",
    "StopValue",
    "UserPromptSubmitValue",
]
