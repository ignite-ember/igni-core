"""The ``PlanSubmit`` gate and the transactions that ask it.

Extensions answer first, then igni's configured hooks, on what the
extensions left. Either may refuse with a reason. These drive the gate
with a real extension gate, a real hook executor running shell
commands, and the coordinator over a session-shaped fixture.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.extension_gate import ExtensionHookGate, ExtensionHookVerdict
from igni_core.core.hooks.extension_values import PlanSubmitValue
from igni_core.core.hooks.schemas import HookDefinition
from igni_core.core.session.schemas import PermissionModeSwitch
from igni_core.core.tools.plan import (
    PlanModeEnteredEvent,
    PlanSubmitDecision,
    PlanSubmitGate,
    PlanTransactionCoordinator,
)
from tests.session_coordinators import plan_session, wire_coordinators


class _Extensions:
    """Whoever answers for the extensions; records what it was asked."""

    def __init__(self, answer: Callable[[PlanSubmitValue], ExtensionHookVerdict[Any]]) -> None:
        self.answer = answer
        self.asked: list[PlanSubmitValue] = []

    def hooks(self, event: HookEvent) -> bool:
        return True

    async def ask(self, value: PlanSubmitValue) -> ExtensionHookVerdict[Any]:
        self.asked.append(value)
        return self.answer(value)

    def tell(self, event, payload) -> None:
        return None

    def tool_called(self, tool_name, *, answered, detail) -> None:
        return None


def _gate(answer=None) -> tuple[PlanSubmitGate, _Extensions | None]:
    extensions = ExtensionHookGate()
    answerer = _Extensions(answer) if answer else None
    extensions.install(answerer)
    return PlanSubmitGate(extensions), answerer


def _rewrite(plan: str) -> Callable[[PlanSubmitValue], ExtensionHookVerdict[Any]]:
    return lambda v: ExtensionHookVerdict(allowed=True, value=v.model_copy(update={"plan": plan}))


def _refuse(reason: str) -> Callable[[PlanSubmitValue], ExtensionHookVerdict[Any]]:
    return lambda v: ExtensionHookVerdict(allowed=False, value=v, reason=reason)


def _command_hook(tmp_path, stdout: str, *, log: bool = False) -> HookExecutor:
    """A PlanSubmit command hook that prints ``stdout``; with ``log`` it
    also saves the payload it was sent."""
    sink = tmp_path / "payload.json"
    command = f"cat > {sink}; " if log else ""
    command += f"echo '{stdout}'"
    return HookExecutor(
        {"PlanSubmit": [HookDefinition(type="command", command=command, timeout=5000)]}
    )


async def _ask(gate: PlanSubmitGate, hooks: HookExecutor, plan="Do it.", attempt=0):
    return await gate.ask(
        session_id="s-1",
        plan=plan,
        tasks=[{"content": "A", "activeForm": ""}],
        attempt=attempt,
        hooks=hooks,
    )


async def test_with_nobody_hooking_the_plan_goes_ahead_unchanged():
    gate, _ = _gate()

    decision = await _ask(gate, HookExecutor({}))

    assert decision == PlanSubmitDecision(allowed=True, plan="Do it.")


async def test_the_extensions_are_handed_the_session_plan_tasks_and_attempt():
    gate, answerer = _gate(_rewrite("Do it."))

    await _ask(gate, HookExecutor({}), attempt=2)

    (asked,) = answerer.asked
    assert (asked.session_id, asked.plan, asked.attempt) == ("s-1", "Do it.", 2)
    assert asked.tasks == [{"content": "A", "activeForm": ""}]


async def test_an_extension_refusal_carries_its_reason_and_igni_hooks_do_not_run(tmp_path):
    gate, _ = _gate(_refuse("Cite two files."))
    hooks = _command_hook(tmp_path, '{"continue": true}', log=True)

    decision = await _ask(gate, hooks)

    assert decision.allowed is False
    assert decision.reason == "Cite two files."
    assert not (tmp_path / "payload.json").exists()


async def test_an_extension_refusal_without_a_reason_is_a_refusal_with_none():
    gate, _ = _gate(_refuse(""))

    decision = await _ask(gate, HookExecutor({}))

    assert (decision.allowed, decision.reason) == (False, "")


async def test_the_extensions_plan_is_stripped():
    gate, _ = _gate(_rewrite("  Checked plan.\n\n"))

    decision = await _ask(gate, HookExecutor({}))

    assert decision.plan == "Checked plan."


async def test_a_blank_extension_plan_keeps_the_agents_plan():
    for blank in ("", "   \n"):
        gate, _ = _gate(_rewrite(blank))

        decision = await _ask(gate, HookExecutor({}), plan="Mine.")

        assert decision == PlanSubmitDecision(allowed=True, plan="Mine.")


async def test_the_hook_is_sent_the_changed_plan_and_the_attempt(tmp_path):
    gate, _ = _gate(_rewrite("Changed."))
    hooks = _command_hook(tmp_path, '{"continue": true}', log=True)

    decision = await _ask(gate, hooks, attempt=3)

    sent = json.loads((tmp_path / "payload.json").read_text())
    assert (sent["session_id"], sent["plan"], sent["attempt"]) == ("s-1", "Changed.", 3)
    assert decision == PlanSubmitDecision(allowed=True, plan="Changed.")


async def test_a_hook_refusal_carries_its_message(tmp_path):
    gate, _ = _gate()
    hooks = _command_hook(tmp_path, '{"continue": false, "systemMessage": "Too short."}')

    decision = await _ask(gate, hooks)

    assert (decision.allowed, decision.reason) == (False, "Too short.")


async def test_a_hook_refusal_without_a_message_says_a_hook_rejected_it(tmp_path):
    gate, _ = _gate()
    hooks = _command_hook(tmp_path, '{"continue": false}')

    decision = await _ask(gate, hooks)

    assert (decision.allowed, decision.reason) == (False, "a PlanSubmit hook rejected it")


# ── the coordinator over the gate ─────────────────────────────────


def _session(hooks: HookExecutor | None = None):
    session = plan_session(hook_executor=hooks or HookExecutor({}))
    wire_coordinators(session, set_permission_mode=lambda mode: PermissionModeSwitch(text=mode))
    return session


def _events(session) -> list[Any]:
    seen: list[Any] = []
    session.broadcast_bus.register(seen.append)
    return seen


async def test_a_rejected_plan_stores_nothing_and_drops_its_task_errors():
    gate, _ = _gate(_refuse("No."))
    session = _session()
    seen = _events(session)

    result = await PlanTransactionCoordinator(session, gate).submit(
        "Do it.", [{"content": "A"}, "junk"]
    )
    session.broadcast_bus.drain_post_run(run_id=None)

    assert (result.ok, result.reason, result.reply_text) == (
        False,
        "rejected",
        "Plan rejected: No.",
    )
    assert result.error == "Plan rejected: No."
    assert result.validation_errors == []
    assert session.plan_store.latest == ""
    assert session.todo_store.items == []
    assert seen == []


async def test_a_rejection_with_no_reason_still_says_rejected():
    gate, _ = _gate(_refuse(""))
    session = _session()

    result = await PlanTransactionCoordinator(session, gate).submit("Do it.", None)

    assert result.reply_text == "Plan rejected: "


async def test_an_empty_plan_is_refused_without_asking_the_gate():
    gate, answerer = _gate(_rewrite("x"))
    session = _session()
    coordinator = PlanTransactionCoordinator(session, gate)

    for plan in (None, "", "  \n "):
        result = await coordinator.submit(plan, [{"content": "A"}])  # type: ignore[arg-type]
        assert (result.ok, result.reason) == (False, "empty_plan")
        assert result.reply_text == (
            "Error: plan is empty. Pass a markdown-formatted plan describing what you intend to do."
        )

    assert answerer.asked == []
    assert session.plan_store.rejected == 0


async def test_the_plan_is_stripped_before_the_gate_and_before_storage():
    gate, answerer = _gate(lambda v: ExtensionHookVerdict(allowed=True, value=v))
    session = _session()

    result = await PlanTransactionCoordinator(session, gate).submit("  Plan é ✓ \n", None)

    assert answerer.asked[0].plan == "Plan é ✓"
    assert session.plan_store.latest == "Plan é ✓"
    assert result.reply_text.startswith("Plan submitted. Stop here")


async def test_a_plan_without_tasks_names_no_task_count_and_leaves_the_checklist():
    gate, _ = _gate()
    session = _session()
    session.todo_store.set([])

    result = await PlanTransactionCoordinator(session, gate).submit("Do it.", None)

    assert result.task_count == 0
    assert "structured task" not in result.reply_text
    assert "validation errors" not in result.reply_text
    assert session.todo_store.items == []


async def test_the_rows_the_gate_is_asked_about_are_the_validated_ones():
    gate, answerer = _gate(lambda v: ExtensionHookVerdict(allowed=True, value=v))
    session = _session()

    result = await PlanTransactionCoordinator(session, gate).submit(
        "Do it.", [{"content": " A ", "activeForm": "Doing A", "status": "done"}, {"content": ""}]
    )

    assert answerer.asked[0].tasks == [{"content": "A", "activeForm": "Doing A"}]
    assert result.validation_errors == ["todos[1]: empty content"]
    assert result.reply_text.endswith("Tasks validation errors (ignored): todos[1]: empty content")


async def test_the_submitted_event_carries_the_plan_the_gate_left():
    gate, _ = _gate(_rewrite("Changed."))
    session = _session()
    seen = _events(session)

    await PlanTransactionCoordinator(session, gate).submit("Mine.", [{"content": "A"}])
    assert seen == []  # queued until the run ends
    session.broadcast_bus.drain_post_run(run_id=None)

    (event,) = seen
    assert event.plan == "Changed."
    assert [t.content for t in event.tasks] == ["A"]


# ── entering ──────────────────────────────────────────────────────


async def test_a_refused_entry_resets_nothing_and_broadcasts_nothing():
    gate, _ = _gate()
    session = plan_session()
    wire_coordinators(
        session,
        set_permission_mode=lambda mode: PermissionModeSwitch(refused=True, text="Not allowed."),
    )
    session.plan_store.record_rejection()
    seen = _events(session)

    result = await PlanTransactionCoordinator(session, gate).enter("why", "task")

    assert (result.ok, result.reason, result.error) == (False, "evaluator_refused", "Not allowed.")
    assert result.reply_text == "Not allowed."
    assert session.plan_store.rejected == 1
    assert seen == []


async def test_entering_broadcasts_the_stripped_reason_and_reports_the_flip():
    gate, _ = _gate()
    session = _session()
    seen = _events(session)

    result = await PlanTransactionCoordinator(session, gate).enter("  refactor  ", "")

    (event,) = seen
    assert isinstance(event, PlanModeEnteredEvent)
    assert (event.mode, event.source, event.reason) == ("plan", "agent", "refactor")
    assert result.reply_text.startswith(
        "Entered plan mode (refactor). File edits and mutating shell commands are now blocked."
    )


async def test_a_whitespace_only_task_does_not_spawn_the_researcher():
    gate, _ = _gate()
    session = _session()
    coordinator = PlanTransactionCoordinator(session, gate)
    calls: list[str] = []

    async def run(task: str) -> str:
        calls.append(task)
        return "report"

    coordinator._researcher.run = run  # type: ignore[method-assign]

    blank = await coordinator.enter("", "   ")
    real = await coordinator.enter("", "  look  ")

    assert calls == ["look"]
    assert blank.researcher_report == ""
    assert real.researcher_report == "report"


async def test_the_reply_without_a_report_tells_the_agent_how_to_gather_context():
    gate, _ = _gate()
    session = _session()

    result = await PlanTransactionCoordinator(session, gate).enter("", "")

    assert result.reply_text == (
        "Entered plan mode. File edits and mutating shell commands are now blocked. "
        "Gather context (your read-only tools, and "
        'run_shell_command "cat <path>"), '
        "then call exit_plan_mode(plan, tasks=[...]) with a concrete proposal. "
        "Tip: pass the user's request as `task=` next time and this tool will "
        "spawn the plan_researcher sub-agent for you automatically."
    )
    assert "file_read" not in result.reply_text


async def test_the_reply_with_a_report_appends_it_after_the_re_research_hint():
    gate, _ = _gate()
    session = _session()
    coordinator = PlanTransactionCoordinator(session, gate)

    async def run(task: str) -> str:
        return "REPORT BODY"

    coordinator._researcher.run = run  # type: ignore[method-assign]
    result = await coordinator.enter("", "look")

    assert result.mode_flipped is False
    assert result.reply_text == (
        "Entered plan mode. File edits and mutating shell commands are now blocked."
        "\n\nplan_researcher sub-agent report follows. "
        "Review it, refine if needed, then call "
        "exit_plan_mode(plan, tasks=[...]) with the final plan. "
        "If the report is thin, call enter_plan_mode again with "
        "a sharper task description for another research pass.\n\n"
        "---\n\n"
        "REPORT BODY"
    )


async def test_each_rejected_submission_counts_as_an_attempt_the_gate_is_told():
    gate, answerer = _gate(_refuse("No."))
    session = _session()
    coordinator = PlanTransactionCoordinator(session, gate)

    await coordinator.submit("Do it.", None)
    await coordinator.submit("Do it.", None)

    assert session.plan_store.rejected == 2
    assert [v.attempt for v in answerer.asked] == [0, 1]


async def test_the_plan_stored_is_the_one_the_gate_left_not_the_one_written():
    gate, _ = _gate(_rewrite("Changed."))
    session = _session()

    await PlanTransactionCoordinator(session, gate).submit("Mine.", None)

    assert session.plan_store.latest == "Changed."


async def test_a_successful_entry_starts_the_attempts_again():
    gate, _ = _gate()
    session = _session()
    session.plan_store.record_rejection()

    await PlanTransactionCoordinator(session, gate).enter("", "")

    assert session.plan_store.rejected == 0
