"""``SessionLearningManager``: what it extracts, injects and falls back to.

The prune of the injected block is pinned in
``test_learning_context_is_pruned.py``; this file pins the rest of the
manager's contract with a fake learning machine (the machine is the
dependency, the manager is the subject):

* ``extract`` sends the turn to ``aprocess`` as a detached task, with the
  ids read when it was called, and never raises when the machine does;
* with no machine ``extract`` and ``inject`` do nothing;
* ``inject`` fills in a missing model and db, and a failing context build
  leaves the prompt alone;
* ``effective_machine`` prefers the team's machine and falls back;
* the boot reason is warned about only when there is no machine.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.session import learning_ops
from igni_core.core.session.learning_ops import SessionLearningManager


class _Machine:
    def __init__(self, ctx: str = "ctx", fail: Exception | None = None) -> None:
        self.model: Any = None
        self.db: Any = None
        self.ctx = ctx
        self.fail = fail
        self.processed: list[dict[str, Any]] = []

    async def abuild_context(self, user_id: str, session_id: str) -> str:
        if self.fail:
            raise self.fail
        return self.ctx

    async def aprocess(self, messages, user_id, session_id) -> None:
        if self.fail:
            raise self.fail
        self.processed.append(
            {
                "messages": [(m.role, m.content) for m in messages],
                "user": user_id,
                "sid": session_id,
            }
        )


def _manager(team=None, learning=None, ids=None, db=None, reason="") -> SessionLearningManager:
    ids = ids if ids is not None else {"u": "u1", "s": "s1"}
    return SessionLearningManager(
        settings=SimpleNamespace(),
        db=db,
        user_id_ref=lambda: ids["u"],
        session_id_ref=lambda: ids["s"],
        main_team_ref=lambda: team,
        learning=learning,
        boot_reason=reason,
    )


@pytest.fixture
def tasks(monkeypatch) -> list[asyncio.Task]:
    started: list[asyncio.Task] = []
    real = learning_ops.background.start

    def capture(work, **kw):
        t = real(work, **kw)
        started.append(t)
        return t

    monkeypatch.setattr(learning_ops.background, "start", capture)
    return started


async def test_extract_sends_the_user_and_assistant_messages(tasks):
    machine = _Machine()
    mgr = _manager(learning=machine)

    await mgr.extract("hello", "hi there")
    await asyncio.gather(*tasks)

    assert machine.processed == [
        {"messages": [("user", "hello"), ("assistant", "hi there")], "user": "u1", "sid": "s1"}
    ]


async def test_extract_without_an_assistant_reply_sends_only_the_user_message(tasks):
    machine = _Machine()
    await _manager(learning=machine).extract("hello", "")
    await asyncio.gather(*tasks)

    assert machine.processed[0]["messages"] == [("user", "hello")]


async def test_extract_uses_the_ids_of_the_moment_it_was_called(tasks):
    machine = _Machine()
    ids = {"u": "u1", "s": "s1"}
    await _manager(learning=machine, ids=ids).extract("a", "b")
    ids.update(u="u2", s="s2")
    await asyncio.gather(*tasks)

    assert machine.processed[0]["user"] == "u1"
    assert machine.processed[0]["sid"] == "s1"


async def test_extract_runs_detached_from_the_caller(tasks):
    machine = _Machine()
    await _manager(learning=machine).extract("a", "b")

    assert machine.processed == [], "the caller waited for the learning run"
    await asyncio.gather(*tasks)
    assert len(machine.processed) == 1


async def test_a_failing_extraction_is_a_warning_not_an_error(tasks, caplog):
    machine = _Machine(fail=RuntimeError("db down"))
    with caplog.at_level(logging.WARNING, logger=learning_ops.logger.name):
        await _manager(learning=machine).extract("a", "b")
        await asyncio.gather(*tasks)

    assert any("Learning extraction failed: db down" in r.getMessage() for r in caplog.records)


async def test_extract_without_a_machine_schedules_nothing(tasks):
    await _manager(learning=None).extract("a", "b")
    assert tasks == []


async def test_inject_without_a_machine_leaves_the_prompt_alone():
    team = SimpleNamespace(instructions=["base"])
    await _manager(team=team, learning=None).inject()
    assert team.instructions == ["base"]


async def test_inject_fills_in_a_missing_model_and_db(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(
        learning_ops,
        "ModelRegistry",
        lambda settings: SimpleNamespace(get_model=lambda: sentinel),
    )
    db = object()
    machine = _Machine()
    team = SimpleNamespace(instructions=["base"])

    await _manager(team=team, learning=machine, db=db).inject()

    assert machine.model is sentinel
    assert machine.db is db


async def test_inject_keeps_a_model_and_db_the_machine_already_has(monkeypatch):
    monkeypatch.setattr(
        learning_ops,
        "ModelRegistry",
        lambda settings: SimpleNamespace(get_model=lambda: pytest.fail("asked for a model")),
    )
    own_model, own_db = object(), object()
    machine = _Machine()
    machine.model, machine.db = own_model, own_db

    await _manager(team=SimpleNamespace(instructions=["b"]), learning=machine, db=object()).inject()

    assert machine.model is own_model
    assert machine.db is own_db


async def test_inject_puts_the_context_under_the_owned_heading():
    team = SimpleNamespace(instructions=["base"])
    machine = _Machine(ctx="likes tabs")
    machine.model = machine.db = object()

    await _manager(team=team, learning=machine).inject()

    assert team.instructions == ["base", "## What I Know About You\n\nlikes tabs"]


async def test_a_failing_context_build_leaves_the_prompt_untouched(caplog):
    team = SimpleNamespace(instructions=["base"])
    machine = _Machine(fail=RuntimeError("blip"))
    machine.model = machine.db = object()

    with caplog.at_level(logging.DEBUG, logger=learning_ops.logger.name):
        await _manager(team=team, learning=machine).inject()

    assert team.instructions == ["base"]
    assert any("Learning context build failed: blip" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("ctx", "instructions"),
    [("", ["base"]), ("ctx", []), ("ctx", None)],
    ids=["empty-context", "empty-instructions", "no-instructions"],
)
async def test_inject_adds_nothing_without_context_or_instructions(ctx, instructions):
    team = SimpleNamespace(instructions=instructions)
    machine = _Machine(ctx=ctx)
    machine.model = machine.db = object()
    before = None if instructions is None else list(instructions)

    await _manager(team=team, learning=machine).inject()

    assert team.instructions == before


def test_the_team_machine_wins_over_the_session_machine():
    team = SimpleNamespace(learning_machine="team-machine")
    mgr = _manager(team=team, learning="session-machine")
    assert mgr.effective_machine == "team-machine"
    assert mgr.machine == "session-machine"


@pytest.mark.parametrize("team", [SimpleNamespace(), SimpleNamespace(learning_machine=None)])
def test_a_team_without_a_machine_falls_back_to_the_session_machine(team):
    assert _manager(team=team, learning="session-machine").effective_machine == "session-machine"


def test_no_machine_anywhere_is_none_not_an_error():
    mgr = _manager(team=SimpleNamespace(), learning=None)
    assert mgr.machine is None
    assert mgr.effective_machine is None


def test_the_team_is_read_each_time_so_a_rebuilt_team_is_seen():
    current = {"team": SimpleNamespace(learning_machine="old")}
    mgr = SessionLearningManager(
        settings=SimpleNamespace(),
        db=None,
        user_id_ref=lambda: "u",
        session_id_ref=lambda: "s",
        main_team_ref=lambda: current["team"],
    )
    assert mgr.effective_machine == "old"
    current["team"] = SimpleNamespace(learning_machine="new")
    assert mgr.effective_machine == "new"


def test_the_boot_reason_is_warned_once_when_there_is_no_machine(caplog):
    with caplog.at_level(logging.WARNING, logger=learning_ops.logger.name):
        _manager(learning=None, reason="no api key")
    assert [r.getMessage() for r in caplog.records] == ["LearningMachine unavailable: no api key"]


@pytest.mark.parametrize(
    ("learning", "reason"), [(object(), "ignored"), (None, "")], ids=["machine", "no-reason"]
)
def test_there_is_no_boot_warning_with_a_machine_or_without_a_reason(caplog, learning, reason):
    with caplog.at_level(logging.WARNING, logger=learning_ops.logger.name):
        _manager(learning=learning, reason=reason)
    assert caplog.records == []
