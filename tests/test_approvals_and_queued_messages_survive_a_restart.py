"""An approval waiting on the person, and a message typed during a run,
survive the backend that held them.

Both used to live only in the backend's memory. Here each is written by
one set of objects and read back by a fresh set on the same database —
what a restart is — on SQLite and, when ``IGNI_TEST_DATABASE_URL`` is
set, on PostgreSQL (``tests/storage_backends.py``):

* a restored approval is shown again (``RunPaused``) and answering it
  resumes its run from Agno's copy, by ``run_id``, with the requirement
  confirmed;
* a restored sub-agent approval is shown again and can be answered;
* a queued message is delivered once — never lost to a restart, never
  delivered by two backends.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.backend.hitl_controller import HitlController
from igni_core.backend.host_backend import OrchestratorHostBackend
from igni_core.backend.pause_handler import PauseHandler
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.session_pool import SessionRuntime
from igni_core.core.approvals.journal import ApprovalJournal
from igni_core.core.db.database import Database
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.hooks.queue import QueueBridge
from igni_core.core.session.queued_messages import DurableMessageQueue, QueuedMessageStore
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.protocol import messages as msg
from tests.backend_server_factory import make_backend
from tests.storage_backends import Scopes

SESSION = "s1"


def _requirement(call_id: str, command: str = "make deploy") -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id=call_id,
            tool_name="run_shell_command",
            tool_args={"command": command},
            requires_confirmation=True,
        )
    )


def _store(db: Database) -> PendingRequirementsStore:
    """What a backend builds at start: the store, restored."""
    store = PendingRequirementsStore(ApprovalJournal(db), session_id=lambda: SESSION)
    store.restore(SESSION)
    return store


class FakeTeam:
    """Records the resume a resolved approval makes."""

    def __init__(self) -> None:
        self.resumed: dict[str, Any] = {}

    async def acontinue_run(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.resumed = kwargs
        return
        yield  # an async generator that yields nothing


class FakeHooks:
    async def execute(self, **_: Any) -> Any:
        return SimpleNamespace(message="", should_continue=True)


def _session(team: FakeTeam, coordinator: SubAgentHITLCoordinator) -> Any:
    return SimpleNamespace(
        session_id=SESSION,
        sub_agent_hitl=coordinator,
        main_team=team,
        permission_evaluator=None,
        hook_executor=FakeHooks(),
        set_permission_mode=lambda mode: mode,
    )


async def _drain(stream: AsyncIterator[Any]) -> AsyncIterator[msg.Message]:
    async for _ in stream:
        pass
    return
    yield


async def test_an_approval_is_asked_again_after_a_restart_and_resumes_its_run(
    database_url: DatabaseUrl,
):
    db = Database(database_url, scope=Scopes.fresh())
    before = _store(db)
    paused = PauseHandler(evaluator=None, store=before).handle(
        SimpleNamespace(run_id="run-1", active_requirements=[_requirement("call-1")])
    )
    (shown,) = paused.messages
    assert isinstance(shown, msg.RunPaused)
    requirement_id = shown.requirements[0].requirement_id

    # The backend restarts: a new store on the same database.
    after = _store(db)
    (again,) = after.waiting_messages()
    assert again.run_id == "run-1"
    assert [r.requirement_id for r in again.requirements] == [requirement_id]
    assert again.requirements[0].tool_args == {"command": "make deploy"}

    team = FakeTeam()
    controller = HitlController(
        _session(team, SubAgentHITLCoordinator()), after, stream_factory=_drain
    )
    replies = [
        m
        async for m in controller.resolve_batch(
            [msg.HITLDecision(requirement_id=requirement_id, action="confirm")]
        )
    ]
    assert not [m for m in replies if isinstance(m, msg.Error)], replies
    assert team.resumed["run_id"] == "run-1"
    assert team.resumed["session_id"] == SESSION
    (resumed,) = team.resumed["requirements"]
    assert resumed.tool_execution.tool_call_id == "call-1"
    assert resumed.confirmation is True

    assert _store(db).waiting_messages() == [], "an answered approval is not asked again"


async def test_what_the_policy_decided_is_kept_for_the_resume(database_url: DatabaseUrl):
    db = Database(database_url, scope=Scopes.fresh())
    decided = _requirement("call-2", "ls")
    decided.confirm()
    _store(db).stash_auto_resolved("run-2", [decided])

    after = _store(db)
    (kept,) = after.drain_auto_resolved("run-2")
    assert kept.tool_execution.tool_call_id == "call-2"
    assert kept.confirmation is True
    assert _store(db).drain_auto_resolved("run-2") == [], "drained once"


async def test_a_run_that_ended_leaves_nothing_waiting(database_url: DatabaseUrl):
    db = Database(database_url, scope=Scopes.fresh())
    PauseHandler(evaluator=None, store=_store(db)).handle(
        SimpleNamespace(run_id="run-3", active_requirements=[_requirement("call-3")])
    )
    _store(db).sweep_run("run-3")
    assert _store(db).waiting_messages() == []


async def test_another_producers_approvals_are_not_shown(database_url: DatabaseUrl):
    mine, theirs = Scopes.pair(database_url)
    PauseHandler(evaluator=None, store=_store(mine)).handle(
        SimpleNamespace(run_id="run-4", active_requirements=[_requirement("call-4")])
    )
    assert len(_store(mine).waiting_messages()) == 1
    assert _store(theirs).waiting_messages() == []


async def test_a_sub_agents_approval_is_asked_again_and_can_be_answered(
    database_url: DatabaseUrl,
):
    db = Database(database_url, scope=Scopes.fresh())
    journal = ApprovalJournal(db)
    before = SubAgentHITLCoordinator()
    before.attach_journal(journal, lambda: SESSION)
    requirement_id = await before.push_requirement(
        _requirement("call-5", "rm -rf build"), run_id="sub-run", agent_path=["architect"]
    )
    before.update_details(requirement_id, "Cleans the build directory.")

    after = SubAgentHITLCoordinator()
    after.attach_journal(ApprovalJournal(db), lambda: SESSION)
    assert after.restore(SESSION) == 1
    (again,) = after.waiting_requests()
    (request,) = again.requirements
    assert (request.requirement_id, request.agent_path) == (requirement_id, ["architect"])
    assert request.details == "Cleans the build directory."

    controller = HitlController(_session(FakeTeam(), after), _store(db), stream_factory=_drain)
    assert [r.requirement_id for m in controller.waiting_messages() for r in m.requirements] == [
        requirement_id
    ]
    assert after.resolve(requirement_id, "confirm") is True
    assert after.waiting_requests() == []

    fresh = SubAgentHITLCoordinator()
    fresh.attach_journal(ApprovalJournal(db), lambda: SESSION)
    assert fresh.restore(SESSION) == 0, "an answered approval is not asked again"


class SavedConversation:
    """Agno's copy of the session: the message ids its runs hold."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def record(self, run_output: Any) -> None:
        self.messages.extend(run_output.messages)

    async def get_session(self, **_: Any) -> Any:
        return SimpleNamespace(runs=[SimpleNamespace(messages=list(self.messages))])


async def test_a_queued_message_is_delivered_once_across_a_restart(database_url: DatabaseUrl):
    db = Database(database_url, scope=Scopes.fresh())
    saved = SavedConversation()

    def queue(session: str = SESSION) -> DurableMessageQueue:
        return DurableMessageQueue(QueuedMessageStore(db), session_id=lambda: session)

    first = queue()
    for text in ("also run the tests", "and update the changelog", "then open a pull request"):
        first.append(text)
    assert list(queue()) == list(first), "each one is a row"
    assert list(queue("another-session")) == []

    # A run takes the first after its next tool call and records it in
    # the conversation; the session is saved, and the run settles.
    bridge = QueueBridge(queue=first)
    await bridge.injector(name="read_file", func=lambda: "contents", args={})
    run_output = SimpleNamespace(messages=[])
    bridge.persister(run_output=run_output)
    saved.record(run_output)
    await first.settle(saved)

    # The next run takes one, and the backend dies before the session is
    # saved. Another one waits.
    first.append("and tag the release")
    second_run = QueueBridge(queue=first)
    await second_run.injector(name="read_file", func=lambda: "contents", args={})
    lost = [m.text for m in second_run.buffer.items]
    assert lost == ["and tag the release"]

    # Restart. Nothing waiting was taken twice, and what the dead run
    # took — never recorded — is offered again, once.
    after = queue()
    assert list(after) == []
    assert await after.recover(saved) == 1
    assert list(after) == ["and tag the release"]
    assert await queue().recover(saved) == 0, "recovered once"

    # And what was recorded before the crash is never offered again.
    recorded = [m.content for m in saved.messages]
    assert recorded == [
        "also run the tests",
        "and update the changelog",
        "then open a pull request",
    ]


async def test_a_message_recorded_before_the_crash_is_not_offered_again(
    database_url: DatabaseUrl,
):
    db = Database(database_url, scope=Scopes.fresh())
    saved = SavedConversation()
    first = DurableMessageQueue(QueuedMessageStore(db), session_id=lambda: SESSION)
    first.append("run the migrations")
    bridge = QueueBridge(queue=first)
    await bridge.injector(name="read_file", func=lambda: "contents", args={})
    run_output = SimpleNamespace(messages=[])
    bridge.persister(run_output=run_output)
    saved.record(run_output)
    # The session was saved; the backend died before the run settled.
    after = DurableMessageQueue(QueuedMessageStore(db), session_id=lambda: SESSION)
    assert await after.recover(saved) == 0
    assert list(after) == []


def test_another_producers_queue_is_not_read(database_url: DatabaseUrl):
    mine, theirs = Scopes.pair(database_url)
    DurableMessageQueue(QueuedMessageStore(mine), session_id=lambda: SESSION).append("mine")
    assert list(DurableMessageQueue(QueuedMessageStore(theirs), session_id=lambda: SESSION)) == []


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[msg.Message] = []

    async def send(self, message: msg.Message) -> None:
        self.sent.append(message)


async def test_a_window_attaching_is_shown_what_waits(database_url: DatabaseUrl):
    """The dialog is shown again where the person will see it: to the
    window that attaches to the session, after a reload or a restart."""
    db = Database(database_url, scope=Scopes.fresh())
    PauseHandler(evaluator=None, store=_store(db)).handle(
        SimpleNamespace(run_id="run-6", active_requirements=[_requirement("call-6")])
    )
    restarted = HitlController(
        _session(FakeTeam(), SubAgentHITLCoordinator()), _store(db), stream_factory=_drain
    )
    transport = RecordingTransport()
    runtime = SessionRuntime(
        backend=SimpleNamespace(controllers=SimpleNamespace(hitl=restarted)),  # type: ignore[arg-type]
        rpc_table={},
        queue=[],
        transport=transport,
    )
    await runtime.replay_waiting_approvals()
    (shown,) = transport.sent
    assert isinstance(shown, msg.RunPaused)
    assert shown.run_id == "run-6"
    assert shown.requirements[0].tool_args == {"command": "make deploy"}


async def test_the_host_model_sees_a_real_backends_waiting_approval(
    database_url: DatabaseUrl, tmp_path: Path
):
    """The host's ``waiting_approvals`` reads the approvals of a real
    :class:`BackendServer`, reached through its controllers."""
    db = Database(database_url, scope=Scopes.fresh())
    store = _store(db)
    PauseHandler(evaluator=None, store=store).handle(
        SimpleNamespace(run_id="run-7", active_requirements=[_requirement("call-7")])
    )
    session = MagicMock()
    session.session_id = SESSION
    session.sub_agent_hitl = SubAgentHITLCoordinator()
    backend = make_backend(session, settings=MagicMock(), hitl_store=store)
    runtime = SessionRuntime(
        backend=backend, rpc_table={}, queue=[], transport=RecordingTransport()
    )
    host = OrchestratorHostBackend(
        orchestrator=SimpleNamespace(pool=SimpleNamespace(find=lambda _: runtime)),  # type: ignore[arg-type]
        transport=MagicMock(),
        project_dir=tmp_path,
        client_state=None,
    )

    (waiting,) = host.waiting_approvals(SESSION)

    assert isinstance(waiting, msg.RunPaused)
    assert waiting.run_id == "run-7"
    assert waiting.requirements[0].tool_args == {"command": "make deploy"}
