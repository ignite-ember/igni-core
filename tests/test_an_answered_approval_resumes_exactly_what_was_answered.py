"""``HitlController.resolve_batch`` resumes the run with the requirements that were answered.

The answer to an approval dialog reaches Agno as one ``acontinue_run``
with the requirements as the person decided them. These tests drive the
real controller, store, coordinator and Agno requirements; only the team
(which records its resume and streams a little) and the hook executor
are stand-ins. Pinned: what an empty, unknown, repeated or cross-run
answer does, which requirements the resume carries, what a sticky choice
saves, how a sub-agent's answer is kept off the main team, what the Stop
hook says afterwards, and that a run's ``continuing`` mark never outlives
a continuation that ended.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunCompletedEvent, RunContentEvent
from agno.run.requirement import RunRequirement

from igni_core.backend.hitl_controller import HitlController
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.schemas_pause import PendingRequirement
from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.core.config.permission_eval import PermissionEvaluator
from igni_core.core.db.database import Database
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.extension_gate import ExtensionHookVerdict, extension_hook_gate
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.session.schemas import PermissionModeSwitch
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.protocol import messages as msg
from tests.storage_backends import Scopes

SESSION = "s-1"


class Team:
    """Records every resume; its stream is what a resumed run would say."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.continued: list[dict[str, Any]] = []
        self.db: Any = None
        self._fail = fail_with

    async def acontinue_run(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.continued.append(kwargs)
        yield RunContentEvent(content="resumed", run_id=kwargs["run_id"])
        if self._fail is not None:
            raise self._fail
        yield RunCompletedEvent(run_id=kwargs["run_id"])


class Hooks:
    def __init__(self, message: str = "", should_continue: bool = True) -> None:
        self.executed: list[dict[str, Any]] = []
        self._reply = SimpleNamespace(message=message, should_continue=should_continue)

    async def execute(self, **kwargs: Any) -> Any:
        self.executed.append(kwargs)
        return self._reply


def _session(
    project: Path,
    team: Team | None = None,
    hooks: Hooks | None = None,
    permission_evaluator: PermissionEvaluator | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=SESSION,
        project_dir=project,
        sub_agent_hitl=SubAgentHITLCoordinator(),
        main_team=team or Team(),
        permission_evaluator=permission_evaluator,
        broadcast_bus=SimpleNamespace(drain_post_run=lambda run_id: None),
        hook_executor=hooks or Hooks(),
        mode=SimpleNamespace(set_permission_mode=lambda mode: PermissionModeSwitch(text=mode)),
    )


def _requirement(tool: str = "run_shell_command", **args: Any) -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_name=tool, tool_args=args or {"command": "make"}, requires_confirmation=True
        )
    )


def _pend(store: PendingRequirementsStore, req_id: str, run_id: str, **args: Any) -> RunRequirement:
    req = _requirement(**args)
    store.register(
        req_id,
        PendingRequirement(req=req, run_id=run_id, request=msg.HITLRequest(requirement_id=req_id)),
    )
    return req


async def _collect(stream: AsyncIterator[msg.Message]) -> list[msg.Message]:
    return [m async for m in stream]


def _confirm(req_id: str, choice: str = "") -> msg.HITLDecision:
    return msg.HITLDecision(requirement_id=req_id, action="confirm", choice=choice)


def _controller(
    tmp_path: Path, store: PendingRequirementsStore | None = None, **kwargs: Any
) -> tuple[HitlController, SimpleNamespace, PendingRequirementsStore]:
    session = _session(tmp_path, **kwargs)
    store = store or PendingRequirementsStore()
    return HitlController(session, store), session, store  # type: ignore[arg-type]


# ── What an answer carries ────────────────────────────────────────


async def test_no_decisions_yield_nothing_and_resume_nothing(tmp_path: Path):
    controller, session, _ = _controller(tmp_path)

    assert await _collect(controller.resolve_batch([])) == []
    assert session.main_team.continued == []
    assert session.hook_executor.executed == []


async def test_an_answered_batch_resumes_its_run_with_each_requirement_as_decided(tmp_path: Path):
    controller, session, store = _controller(tmp_path)
    ok = _pend(store, "r1", "run-1", command="make")
    no = _pend(store, "r2", "run-1", command="rm -rf /")

    out = await _collect(
        controller.resolve_batch(
            [_confirm("r1"), msg.HITLDecision(requirement_id="r2", action="reject")]
        )
    )

    (call,) = session.main_team.continued
    assert (call["run_id"], call["session_id"]) == ("run-1", SESSION)
    assert call["requirements"] == [ok, no]
    assert ok.confirmation is True and no.confirmation is False
    assert (call["stream"], call["stream_events"]) == (True, True)
    assert [m.type for m in out] == ["content_delta", "run_completed"]
    assert store.pending_ids() == []


async def test_what_the_policy_decided_for_the_run_joins_the_answered_ones(tmp_path: Path):
    controller, session, store = _controller(tmp_path)
    decided = _requirement(command="ls")
    store.stash_auto_resolved("run-1", [decided])
    other = _requirement(command="pwd")
    store.stash_auto_resolved("run-2", [other])
    asked = _pend(store, "r1", "run-1")

    await _collect(controller.resolve_batch([_confirm("r1")]))

    (call,) = session.main_team.continued
    assert call["requirements"] == [asked, decided]
    assert store.drain_auto_resolved("run-2") == [other]


async def test_an_unknown_requirement_is_an_error_and_nothing_resumes(tmp_path: Path):
    controller, session, _ = _controller(tmp_path)

    out = await _collect(controller.resolve_batch([_confirm("ghost")]))

    assert [m.text for m in out] == ["Unknown requirement: ghost"]
    assert session.main_team.continued == []
    assert session.hook_executor.executed == []


async def test_a_requirement_answered_twice_is_unknown_the_second_time(tmp_path: Path):
    controller, session, store = _controller(tmp_path)
    _pend(store, "r1", "run-1")

    await _collect(controller.resolve_batch([_confirm("r1")]))
    again = await _collect(controller.resolve_batch([_confirm("r1")]))

    assert [m.text for m in again] == ["Unknown requirement: r1"]
    assert len(session.main_team.continued) == 1


async def test_an_answer_from_another_run_is_refused_and_can_still_be_answered(tmp_path: Path):
    controller, session, store = _controller(tmp_path)
    first = _pend(store, "r1", "run-a")
    stray = _pend(store, "r2", "run-b")

    out = await _collect(controller.resolve_batch([_confirm("r1"), _confirm("r2")]))

    errors = [m.text for m in out if isinstance(m, msg.Error)]
    assert errors == [
        "Cross-run HITL batch rejected: requirement r2 belongs to run run-b but batch is for run run-a"
    ]
    (call,) = session.main_team.continued
    assert (call["run_id"], call["requirements"]) == ("run-a", [first])
    assert store.pending_ids() == ["r2"]
    assert stray.confirmation is None

    await _collect(controller.resolve_batch([_confirm("r2")]))

    assert [c["run_id"] for c in session.main_team.continued] == ["run-a", "run-b"]


async def test_a_requirement_whose_answer_cannot_be_applied_is_an_error_and_not_resumed(
    tmp_path: Path,
):
    controller, session, store = _controller(tmp_path)
    broken = _pend(store, "r1", "run-1")
    broken.confirm = lambda: (_ for _ in ()).throw(RuntimeError("already decided"))  # type: ignore[method-assign]

    out = await _collect(controller.resolve_batch([_confirm("r1")]))

    assert [m.text for m in out] == ["Failed to confirm requirement r1: already decided"]
    assert session.main_team.continued == []


async def test_resolve_single_answers_once_by_default_and_saves_no_rule(tmp_path: Path):
    controller, session, store = _controller(tmp_path)
    req = _pend(store, "r1", "run-1")

    out = await _collect(controller.resolve_single("r1", "confirm"))

    assert req.confirmation is True
    assert [c["run_id"] for c in session.main_team.continued] == ["run-1"]
    assert out[-1].type == "run_completed"
    assert not (tmp_path / CONFIG_DIR / "settings.local.json").exists()


async def test_a_sticky_choice_saves_a_rule_and_holds_the_session_to_it(tmp_path: Path):
    evaluator = PermissionEvaluator()
    controller, _, store = _controller(tmp_path, permission_evaluator=evaluator)
    _pend(store, "r1", "run-1", command="make all")

    await _collect(controller.resolve_batch([_confirm("r1", "always")]))

    saved = json.loads((tmp_path / CONFIG_DIR / "settings.local.json").read_text())
    assert saved["permissions"]["allow"] == ["Bash(make all)"]
    assert [r.tool for r in evaluator.allow] == ["Bash"]


# ── A sub-agent's answer ──────────────────────────────────────────


async def test_a_sub_agent_s_answer_goes_to_the_sub_agent_and_not_the_main_team(tmp_path: Path):
    controller, session, store = _controller(tmp_path)
    coordinator = session.sub_agent_hitl
    sub_req = _requirement("save_file", file_name="a.py")
    asking = asyncio.create_task(coordinator.push_requirement(sub_req, run_id="sub-1"))
    for _ in range(100):
        (pending,) = coordinator.list_new_pending() or [None]
        if pending is not None:
            break
        await asyncio.sleep(0.01)
    assert pending is not None
    # A main-team entry under the same id is dropped, not resumed.
    store.register(pending.requirement_id, PendingRequirement(req=object(), run_id="run-1"))

    out = await _collect(controller.resolve_batch([_confirm(pending.requirement_id)]))
    await asyncio.wait_for(asking, 2)

    assert out == []
    assert session.main_team.continued == []
    assert session.hook_executor.executed == []
    assert store.pending_ids() == []
    assert sub_req.confirmation is True


# ── After the continuation ────────────────────────────────────────


class Gate:
    def __init__(self, refusal: str | None) -> None:
        self._refusal = refusal

    def hooks(self, event: HookEvent) -> bool:
        return event is HookEvent.STOP

    async def ask(self, value: Any) -> ExtensionHookVerdict[Any]:
        if self._refusal is None:
            return ExtensionHookVerdict(allowed=True, value=value)
        return ExtensionHookVerdict(allowed=False, value=value, reason=self._refusal, by="acme")

    def tell(self, *_: Any) -> None:
        return None

    def tool_called(self, *_: Any, **__: Any) -> None:
        return None


@pytest.fixture
def gate() -> Any:
    yield extension_hook_gate
    extension_hook_gate.install(None)


async def test_an_extension_that_refuses_the_stop_says_why_and_the_configured_hooks_stay_quiet(
    tmp_path: Path, gate: Any
):
    hooks = Hooks(message="configured hook says stop", should_continue=False)
    controller, _, store = _controller(tmp_path, hooks=hooks)
    _pend(store, "r1", "run-1")
    gate.install(Gate("keep going: the tests are red"))

    out = await _collect(controller.resolve_batch([_confirm("r1")]))

    infos = [m.text for m in out if isinstance(m, msg.Info)]
    assert infos == ["keep going: the tests are red"]
    assert hooks.executed == []


async def test_a_configured_hook_s_message_is_shown_only_when_it_says_stop(tmp_path: Path):
    for continues, shown in ((False, ["wrap up"]), (True, [])):
        hooks = Hooks(message="wrap up", should_continue=continues)
        controller, _, store = _controller(tmp_path, hooks=hooks)
        _pend(store, "r1", "run-1")

        out = await _collect(controller.resolve_batch([_confirm("r1")]))

        assert [m.text for m in out if isinstance(m, msg.Info)] == shown
        (call,) = hooks.executed
        assert call["event"] == HookEvent.STOP.value
        assert call["payload"].session_id == SESSION


# ── The mark a continuation leaves ────────────────────────────────


def _journalled(database_url: DatabaseUrl) -> tuple[Database, PendingRequirementsStore]:
    db = Database(database_url, scope=Scopes.fresh())
    return db, PendingRequirementsStore(ApprovalJournal(db), session_id=lambda: SESSION)


def _after_a_restart(db: Database) -> list[str]:
    store = PendingRequirementsStore(ApprovalJournal(db), session_id=lambda: SESSION)
    store.restore(SESSION)
    return store.take_interrupted_runs()


async def test_a_run_that_was_continuing_is_marked_while_it_runs_and_cleared_when_it_ends(
    database_url: DatabaseUrl, tmp_path: Path
):
    db, store = _journalled(database_url)
    controller, _, _ = _controller(tmp_path, store)
    _pend(store, "r1", "run-1")
    marked_midway: list[str] = []

    async def watching(stream: AsyncIterator[Any]) -> AsyncIterator[msg.Message]:
        async for _ in stream:
            marked_midway.extend(_after_a_restart(db))
            return
        yield

    controller._stream_factory = watching  # type: ignore[assignment]
    await _collect(controller.resolve_batch([_confirm("r1")]))

    assert marked_midway == ["run-1"]
    assert _after_a_restart(db) == []


@pytest.mark.parametrize("how", ["fails", "cancelled"])
async def test_a_continuation_that_fails_or_is_cancelled_still_clears_its_mark(
    database_url: DatabaseUrl, tmp_path: Path, how: str
):
    db, store = _journalled(database_url)
    failure = RuntimeError("provider hung up") if how == "fails" else asyncio.CancelledError()
    controller, _, _ = _controller(tmp_path, store, team=Team(fail_with=failure))
    _pend(store, "r1", "run-1")

    async def straight(stream: AsyncIterator[Any]) -> AsyncIterator[msg.Message]:
        async for _ in stream:
            pass
        yield msg.Info(text="unreachable")

    controller._stream_factory = straight  # type: ignore[assignment]
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await _collect(controller.resolve_batch([_confirm("r1")]))

    assert _after_a_restart(db) == []


# ── Streams and sweeping ──────────────────────────────────────────


async def test_each_stream_gets_its_own_multiplexer_and_its_own_relay(tmp_path: Path):
    controller, session, _ = _controller(tmp_path)

    async def team(name: str) -> AsyncIterator[Any]:
        yield RunContentEvent(content=name, run_id=name)
        await asyncio.sleep(0.05)
        yield RunCompletedEvent(run_id=name)

    both = await asyncio.gather(
        _collect(controller.stream_with_subagent(team("a"))),
        _collect(controller.stream_with_subagent(team("b"))),
    )

    assert [[m.type for m in out] for out in both] == [["content_delta", "run_completed"]] * 2
    assert [out[0].text for out in both] == ["a", "b"]
    assert not session.sub_agent_hitl.has_relay


async def test_sweeping_a_finished_run_drops_what_it_left_pending_and_decided(tmp_path: Path):
    controller, _, store = _controller(tmp_path)
    _pend(store, "r1", "run-1")
    _pend(store, "r2", "run-2")
    store.stash_auto_resolved("run-1", [_requirement()])

    controller.sweep_run("run-1")

    assert store.pending_ids() == ["r2"]
    assert store.drain_auto_resolved("run-1") == []


async def test_a_pause_is_handled_into_what_the_window_is_shown(tmp_path: Path):
    controller, _, store = _controller(tmp_path)
    req = _requirement(command="make")

    result = controller.handle_pause(SimpleNamespace(run_id="run-1", active_requirements=[req]))  # type: ignore[arg-type]

    (paused,) = result.messages
    assert isinstance(paused, msg.RunPaused) and paused.run_id == "run-1"
    assert paused.requirements[0].requirement_id in store.pending_ids()


async def test_no_approval_waiting_is_none_shown(tmp_path: Path):
    controller, _, store = _controller(tmp_path)
    assert controller.waiting_messages() == []
    _pend(store, "r1", "run-1")
    (waiting,) = controller.waiting_messages()
    assert (waiting.run_id, [r.requirement_id for r in waiting.requirements]) == ("run-1", ["r1"])


async def test_an_interrupted_run_is_not_reported_when_agno_holds_no_stored_run(
    database_url: DatabaseUrl, tmp_path: Path
):
    db, store = _journalled(database_url)
    store.begin_continuing("run-1")
    restarted = PendingRequirementsStore(ApprovalJournal(db), session_id=lambda: SESSION)
    restarted.restore(SESSION)
    controller, _, _ = _controller(tmp_path, restarted)  # the team has no db

    assert await _collect(controller.resume_interrupted()) == []
    assert _after_a_restart(db) == []
