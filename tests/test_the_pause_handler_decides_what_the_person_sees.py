"""``PauseHandler.handle`` decides, per paused tool call, whether the person is asked.

Policy DENY rejects the call with a note, ALLOW confirms it, and only what the
policy left undecided is registered in the store and shown, in one ``RunPaused``.
Anything that goes wrong deciding — no evaluator, an evaluator that raises, a
requirement that refuses to confirm or reject — falls to asking the person,
never to allowing.

Real handler, real store; the Agno requirements are small fakes that record
which of ``confirm`` / ``reject`` was called.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from igni_core.backend.pause_handler import PauseHandler
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.core.config.permission_eval.schemas import PermissionDecision, PermissionOutcome
from igni_core.protocol import messages as msg


class FakeRequirement:
    def __init__(self, name: str | None, args: Any = None, fail: str = "") -> None:
        self.tool_execution = (
            None if name is None else SimpleNamespace(tool_name=name, tool_args=args or {})
        )
        self.calls: list[tuple[str, str | None]] = []
        self._fail = fail

    def confirm(self) -> None:
        if self._fail == "confirm":
            raise RuntimeError("cannot confirm")
        self.calls.append(("confirm", None))

    def reject(self, note: str | None = None) -> None:
        if self._fail == "reject":
            raise RuntimeError("cannot reject")
        self.calls.append(("reject", note))


class FakeEvaluator:
    """Answers by tool name; a name mapped to an exception raises it."""

    def __init__(self, verdicts: dict[str, Any]) -> None:
        self._verdicts = verdicts

    def evaluate_outcome(self, name: str, args: dict[str, Any]) -> PermissionOutcome:
        verdict = self._verdicts.get(
            name, PermissionOutcome(decision=PermissionDecision.DEFER, reason="", source="defer")
        )
        if isinstance(verdict, Exception):
            raise verdict
        return verdict


def _deny(reason: str) -> PermissionOutcome:
    return PermissionOutcome(decision=PermissionDecision.DENY, reason=reason, source="deny")


def _allow() -> PermissionOutcome:
    return PermissionOutcome(decision=PermissionDecision.ALLOW, reason="", source="allow")


def _event(reqs: Any, run_id: Any = "run-1") -> SimpleNamespace:
    return SimpleNamespace(run_id=run_id, active_requirements=reqs)


def _handle(evaluator: Any, reqs: Any, run_id: Any = "run-1"):
    store = PendingRequirementsStore()
    result = PauseHandler(evaluator, store).handle(_event(reqs, run_id))
    return result, store


def test_a_denied_call_is_rejected_with_the_reason_and_the_person_is_not_asked():
    req = FakeRequirement("edit_file")
    result, store = _handle(FakeEvaluator({"edit_file": _deny("plan mode")}), [req])

    assert req.calls == [("reject", "Blocked: plan mode")]
    assert result.messages == []
    assert result.auto_resolved == [req]
    assert store.pending_ids() == []


def test_a_denial_without_a_reason_still_rejects():
    req = FakeRequirement("edit_file")
    result, _ = _handle(FakeEvaluator({"edit_file": _deny("")}), [req])

    assert req.calls == [("reject", "Blocked: ")]
    assert result.auto_resolved == [req]


def test_an_allowed_call_is_confirmed_and_the_person_is_not_asked():
    req = FakeRequirement("read_file")
    result, store = _handle(FakeEvaluator({"read_file": _allow()}), [req])

    assert req.calls == [("confirm", None)]
    assert result.messages == []
    assert result.auto_resolved == [req]
    assert store.pending_ids() == []


def test_an_undecided_call_is_registered_and_shown_in_one_paused_message():
    first = FakeRequirement("run_shell_command", {"command": "make deploy"})
    second = FakeRequirement("write_file", {"path": "a.txt"})
    result, store = _handle(FakeEvaluator({}), [first, second], run_id="run-9")

    (paused,) = result.messages
    assert isinstance(paused, msg.RunPaused)
    assert paused.run_id == "run-9"
    assert [r.tool_name for r in paused.requirements] == ["run_shell_command", "write_file"]
    assert paused.requirements[0].tool_args == {"command": "make deploy"}
    assert all(len(r.requirement_id) == 8 for r in paused.requirements)
    assert sorted(store.pending_ids()) == sorted(r.requirement_id for r in paused.requirements)
    assert result.auto_resolved == []
    assert result.run_id == "run-9"
    assert first.calls == [] and second.calls == []


def test_a_mixed_pause_decides_some_and_asks_for_the_rest():
    denied = FakeRequirement("edit_file")
    asked = FakeRequirement("run_shell_command")
    result, store = _handle(FakeEvaluator({"edit_file": _deny("no")}), [denied, asked])

    (paused,) = result.messages
    assert [r.tool_name for r in paused.requirements] == ["run_shell_command"]
    assert result.auto_resolved == [denied]
    assert len(store.pending_ids()) == 1


def test_without_an_evaluator_every_call_goes_to_the_person():
    req = FakeRequirement("edit_file")
    result, store = _handle(None, [req])

    assert len(result.messages) == 1
    assert req.calls == []
    assert result.auto_resolved == []
    assert len(store.pending_ids()) == 1


def test_an_evaluator_that_raises_sends_the_call_to_the_person_not_through():
    req = FakeRequirement("edit_file")
    result, store = _handle(FakeEvaluator({"edit_file": RuntimeError("boom")}), [req])

    assert len(result.messages) == 1
    assert req.calls == []
    assert result.auto_resolved == []
    assert len(store.pending_ids()) == 1


def test_a_requirement_that_cannot_be_confirmed_is_shown_instead():
    req = FakeRequirement("read_file", fail="confirm")
    result, store = _handle(FakeEvaluator({"read_file": _allow()}), [req])

    assert result.auto_resolved == []
    (paused,) = result.messages
    assert paused.requirements[0].tool_name == "read_file"
    assert len(store.pending_ids()) == 1


def test_a_requirement_that_cannot_be_rejected_is_shown_instead():
    req = FakeRequirement("edit_file", fail="reject")
    result, store = _handle(FakeEvaluator({"edit_file": _deny("no")}), [req])

    assert result.auto_resolved == []
    assert len(result.messages) == 1
    assert len(store.pending_ids()) == 1


def test_an_event_without_requirements_decides_and_shows_nothing():
    for reqs in (None, []):
        result, store = _handle(FakeEvaluator({}), reqs)
        assert result.messages == []
        assert result.auto_resolved == []
        assert store.pending_ids() == []

    store = PendingRequirementsStore()
    result = PauseHandler(None, store).handle(SimpleNamespace())
    assert result.messages == [] and result.run_id is None


def test_an_event_without_a_run_id_is_shown_with_an_empty_one():
    result, _ = _handle(None, [FakeRequirement("edit_file")], run_id=None)

    assert result.run_id is None
    assert result.messages[0].run_id == ""


def test_a_requirement_without_a_tool_execution_is_shown_with_an_empty_name_and_args():
    result, store = _handle(None, [FakeRequirement(None)])

    (request,) = result.messages[0].requirements
    assert request.tool_name == ""
    assert request.tool_args == {}
    assert len(store.pending_ids()) == 1


def test_a_long_unicode_tool_name_reaches_the_person_unchanged():
    name = "outil_é世界_" + "x" * 300
    result, _ = _handle(None, [FakeRequirement(name)])

    assert result.messages[0].requirements[0].tool_name == name
