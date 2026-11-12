"""``RehydrateController`` — what each boot-time recovery step restores, and says.

Every step is best-effort and answers with a :class:`RehydrateOutcome`
instead of raising. Here each is driven over the stores a session holds
(a real ``PlanStore``, ``TodoStore`` and ``DurableMessageQueue``) with a
persistence layer that either holds rows or fails, and the outcome and the
restored state are both read back.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend.schemas_lifecycle import RehydrateOutcome
from igni_core.backend.server_rehydrate import RehydrateController
from igni_core.core.session.queued_messages import DurableMessageQueue, QueuedMessageStore
from igni_core.core.tools.orphan_rehydrator import (
    OrphanRehydrator,
    RehydrateBuild,
    RehydrateResult,
)
from igni_core.core.tools.plan import PlanDecision, PlanDecisionsBlob, PlanStore
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.todo import TodoItemWire, TodoStore


class _Persistence:
    def __init__(self, *, events=(), decisions=None, todos=(), fail: str = "") -> None:
        self._events = list(events)
        self._decisions = decisions or PlanDecisionsBlob()
        self._todos = list(todos)
        self._fail = fail

    async def load_event_log(self):
        if self._fail:
            raise RuntimeError(self._fail)
        return list(self._events)

    async def load_plan_decisions(self):
        if self._fail:
            raise RuntimeError(self._fail)
        return self._decisions

    async def load_todos(self):
        if self._fail:
            raise RuntimeError(self._fail)
        return list(self._todos)


def _session(persistence=None, **extra) -> SimpleNamespace:
    restored: list = []
    return SimpleNamespace(
        persistence=persistence,
        plan_store=PlanStore(),
        todo_store=TodoStore(),
        restore_event_log=restored.extend,
        restored=restored,
        project_dir=None,
        **extra,
    )


def _controller(session) -> RehydrateController:
    return RehydrateController(session, ProcessSupervisor())  # type: ignore[arg-type]


def _outcome(step: str, ok: bool = True, reason: str | None = None) -> RehydrateOutcome:
    return RehydrateOutcome(ok=ok, step=step, reason=reason)  # type: ignore[arg-type]


# ── event log ─────────────────────────────────────────────────


async def test_the_event_log_is_put_back_on_the_session() -> None:
    session = _session(_Persistence(events=["e1", "e2"]))

    outcome = await _controller(session).event_log()

    assert outcome == RehydrateOutcome(ok=True, step="event_log")
    assert session.restored == ["e1", "e2"]


async def test_an_empty_event_log_restores_nothing() -> None:
    session = _session(_Persistence())

    outcome = await _controller(session).event_log()

    assert (outcome.ok, outcome.reason) == (True, "empty on disk")
    assert session.restored == []


async def test_without_persistence_there_is_nothing_to_load() -> None:
    controller = _controller(_session(None))

    for step in ("event_log", "plan_decisions", "todos"):
        outcome = await getattr(controller, step)()
        assert (outcome.ok, outcome.step, outcome.reason) == (True, step, "no persistence")


async def test_a_store_that_cannot_be_read_is_reported_not_raised() -> None:
    session = _session(_Persistence(fail="disk gone"))
    controller = _controller(session)

    for step in ("event_log", "plan_decisions", "todos"):
        outcome = await getattr(controller, step)()
        assert (outcome.ok, outcome.step, outcome.reason) == (False, step, "disk gone")
    assert session.restored == []
    assert session.plan_store.decisions == {}
    assert session.todo_store.items == []


# ── plan decisions and todos ──────────────────────────────────


async def test_plan_decisions_come_back_keyed_by_run() -> None:
    blob = PlanDecisionsBlob(decisions={"run-1": PlanDecision.APPROVED})
    session = _session(_Persistence(decisions=blob))

    outcome = await _controller(session).plan_decisions()

    assert outcome == RehydrateOutcome(ok=True, step="plan_decisions")
    assert session.plan_store.decisions == {"run-1": PlanDecision.APPROVED}


async def test_the_persisted_todos_replace_the_plan_seed() -> None:
    session = _session(_Persistence(todos=[TodoItemWire(content="Ship", status="in_progress")]))
    session.todo_store.set([TodoItemWire(content="Seeded", status="pending")])

    outcome = await _controller(session).todos()

    assert outcome == RehydrateOutcome(ok=True, step="todos")
    assert [(t.content, t.status) for t in session.todo_store.items] == [("Ship", "in_progress")]


async def test_with_no_snapshot_the_plan_seed_stays() -> None:
    session = _session(_Persistence())
    session.todo_store.set([TodoItemWire(content="Seeded", status="pending")])

    outcome = await _controller(session).todos()

    assert (outcome.ok, outcome.reason) == (True, "no snapshot")
    assert [t.content for t in session.todo_store.items] == ["Seeded"]


# ── plan store ────────────────────────────────────────────────


async def test_a_plan_already_held_is_not_read_from_history() -> None:
    session = _session()
    session.plan_store.set_plan("the plan", run_id="r1")

    outcome = await _controller(session).plan_store()

    assert (outcome.ok, outcome.reason) == (True, "already populated")
    assert session.plan_store.latest == "the plan"


# ── queued messages ───────────────────────────────────────────


class _Conversation:
    """Agno's copy of the session: the message ids a conversation holds."""

    def __init__(self, ids: list[str]) -> None:
        self._ids = ids

    def get_session(self, session_id, session_type):  # noqa: ARG002
        run = SimpleNamespace(messages=[SimpleNamespace(id=i) for i in self._ids])
        return SimpleNamespace(runs=[run])


def _taken_queue(tmp_path, *texts: str):
    queue = DurableMessageQueue(QueuedMessageStore(tmp_path / "q.db"), session_id=lambda: "s")
    for text in texts:
        queue.append(text)
    return queue, [queue.take() for _ in texts]


async def test_a_message_a_run_took_and_never_recorded_is_offered_again(tmp_path) -> None:
    queue, (recorded, lost) = _taken_queue(tmp_path, "recorded", "lost")
    session = _session(db=_Conversation([recorded.id]))

    outcome = await _controller(session).queued_messages(queue)

    assert (outcome.ok, outcome.step, outcome.reason) == (
        True,
        "queued_messages",
        "1 offered again",
    )
    assert list(queue) == ["lost"]


async def test_a_message_the_conversation_holds_is_dropped_not_offered(tmp_path) -> None:
    queue, (recorded,) = _taken_queue(tmp_path, "recorded")
    session = _session(db=_Conversation([recorded.id]))

    outcome = await _controller(session).queued_messages(queue)

    assert (outcome.ok, outcome.reason) == (True, "")
    assert list(queue) == []


async def test_without_a_queue_there_is_nothing_to_recover() -> None:
    outcome = await _controller(_session()).queued_messages(None)

    assert (outcome.ok, outcome.reason) == (True, "no queue")


# ── orphan processes ──────────────────────────────────────────


async def test_without_a_project_dir_no_orphans_are_looked_for() -> None:
    outcome = await _controller(_session()).orphan_processes()

    assert (outcome.ok, outcome.step, outcome.reason) == (
        True,
        "orphan_processes",
        "no project_dir",
    )


async def test_a_clean_project_has_no_orphans_to_adopt(tmp_path) -> None:
    session = _session()
    session.project_dir = tmp_path

    outcome = await _controller(session).orphan_processes()

    assert outcome.ok is True and outcome.step == "orphan_processes"


async def test_a_store_that_cannot_be_built_is_named(tmp_path, monkeypatch) -> None:
    session = _session()
    session.project_dir = tmp_path
    monkeypatch.setattr(
        "igni_core.backend.server_rehydrate.build_rehydrator",
        lambda supervisor, project_dir: RehydrateBuild(
            result=RehydrateResult(ok=False, surfaced=0, pruned=0, reason="store_init: no disk")
        ),
    )

    outcome = await _controller(session).orphan_processes()

    assert (outcome.ok, outcome.reason) == (False, "store_init: no disk")


async def test_a_store_that_fails_without_a_reason_is_called_unknown(tmp_path, monkeypatch) -> None:
    session = _session()
    session.project_dir = tmp_path
    monkeypatch.setattr(
        "igni_core.backend.server_rehydrate.build_rehydrator",
        lambda supervisor, project_dir: RehydrateBuild(
            result=RehydrateResult(ok=False, surfaced=0, pruned=0, reason="")
        ),
    )

    outcome = await _controller(session).orphan_processes()

    assert (outcome.ok, outcome.reason) == (False, "unknown")


async def test_a_rehydrator_that_raises_is_reported_not_raised(tmp_path, monkeypatch) -> None:
    class Boom(OrphanRehydrator):
        async def run(self):
            raise RuntimeError("db locked")

    session = _session()
    session.project_dir = tmp_path
    monkeypatch.setattr(
        "igni_core.backend.server_rehydrate.build_rehydrator",
        lambda supervisor, project_dir: RehydrateBuild(rehydrator=Boom(ProcessSupervisor(), None)),  # type: ignore[arg-type],
    )

    outcome = await _controller(session).orphan_processes()

    assert (outcome.ok, outcome.reason) == (False, "db locked")


@pytest.mark.parametrize("project", [None])
async def test_the_log_store_is_pointed_at_the_project_even_when_there_is_none(project) -> None:
    supervisor = ProcessSupervisor()
    seen: list = []
    supervisor.configure_log_store = seen.append  # type: ignore[method-assign]
    session = _session()
    session.project_dir = project

    await RehydrateController(session, supervisor).orphan_processes()  # type: ignore[arg-type]

    assert seen == [project]
