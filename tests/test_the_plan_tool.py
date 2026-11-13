"""The plan toolkit: what it registers, what the model reads, and how it asks.

``PlanTool`` is the adapter between the agent's two plan-mode tools and
the transactions behind them. The transactions are pinned elsewhere;
this pins the adapter: the toolkit's name, the two functions it
offers, the descriptions the model reads, and that a ``PlanSubmit``
question really goes to the process's extensions through it.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterator
from typing import Any

import pytest

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.extension_gate import ExtensionHookVerdict, extension_hook_gate
from igni_core.core.session.schemas import PermissionModeSwitch
from igni_core.core.tools.plan import PlanTool
from tests.session_coordinators import plan_session, wire_coordinators


class _RejectingExtension:
    """An extension that hooks every event and refuses what it is asked."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.asked: list[Any] = []
        extension_hook_gate.install(self)

    def hooks(self, event: HookEvent) -> bool:
        return event is HookEvent.PLAN_SUBMIT

    async def ask(self, value: Any) -> ExtensionHookVerdict[Any]:
        self.asked.append(value)
        return ExtensionHookVerdict(allowed=False, value=value, reason=self.reason)

    def tell(self, event: HookEvent, payload: Any) -> None:
        return None

    def tool_called(self, tool_name: str, *, answered: bool, detail: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_extensions() -> Iterator[None]:
    extension_hook_gate.install(None)
    yield
    extension_hook_gate.install(None)


def _tool() -> tuple[PlanTool, Any]:
    session = wire_coordinators(
        plan_session(),
        set_permission_mode=lambda mode: PermissionModeSwitch(text=mode),
    )
    return PlanTool(session), session


def _digest(function: Any) -> str:
    return hashlib.sha256(inspect.getdoc(function).encode()).hexdigest()  # type: ignore[arg-type]


def test_the_toolkit_offers_exactly_the_two_plan_tools_under_the_igni_name() -> None:
    tool, _ = _tool()

    assert tool.name == "igni_plan"
    assert sorted(tool.async_functions) == ["enter_plan_mode", "exit_plan_mode"]


def test_the_model_reads_the_descriptions_it_was_trained_on() -> None:
    tool, _ = _tool()
    enter = tool.async_functions["enter_plan_mode"]
    exit_ = tool.async_functions["exit_plan_mode"]
    enter.process_entrypoint()
    exit_.process_entrypoint()

    assert enter.description.startswith("Enter plan mode AND spawn the plan_researcher sub-agent.")
    assert exit_.description.startswith("Submit a plan for the user's review.")
    assert _digest(PlanTool.enter_plan_mode) == (
        "a521459451e8f1255cf54db6969ddc316dc5ec73a233408f1bb368ce48b6db1a"
    )
    assert _digest(PlanTool.exit_plan_mode) == (
        "0aecd8e581eb6349853b7ad10f8ac532b3379f2271fda54835d6ed16f985f187"
    )


def test_the_arguments_the_model_fills_in_are_the_ones_it_was_trained_on() -> None:
    enter = inspect.signature(PlanTool.enter_plan_mode).parameters
    exit_ = inspect.signature(PlanTool.exit_plan_mode).parameters

    assert [(n, p.default) for n, p in enter.items() if n != "self"] == [
        ("reason", ""),
        ("task", ""),
    ]
    assert [(n, p.default) for n, p in exit_.items() if n != "self"] == [
        ("plan", inspect.Parameter.empty),
        ("tasks", None),
    ]


async def test_a_plan_an_extension_rejects_through_the_tool_goes_back_to_the_agent() -> None:
    extension = _RejectingExtension("Cite two files.")
    tool, session = _tool()

    reply = await tool.exit_plan_mode("## Plan\n\nDo it.", [{"content": "step"}])

    assert reply == "Plan rejected: Cite two files."
    assert session.plan_store.latest == ""
    assert len(extension.asked) == 1
    assert extension.asked[0].plan == "## Plan\n\nDo it."


async def test_the_tool_answers_the_same_plan_once_nobody_objects() -> None:
    tool, session = _tool()

    reply = await tool.exit_plan_mode("## Plan\n\nDo it.")

    assert not reply.startswith("Plan rejected")
    assert session.plan_store.latest == "## Plan\n\nDo it."
