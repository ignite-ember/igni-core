"""The session RPCs of one backend: the list, the title, the switch, the auto-name.

``SessionsController`` is driven with a real :class:`SessionIdentity` and a
persistence that answers as the real one does (typed rows, a
:class:`PersistResult`), so what a window is sent is what is asserted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from igni_core.backend.server_sessions import SessionsController
from igni_core.core.session.identity import SessionIdentity
from igni_core.core.session.schemas import PersistResult, SessionListRow
from igni_core.protocol import messages as msg


class Persistence:
    def __init__(self) -> None:
        self.rows: list[SessionListRow] = []
        self.limits: list[Any] = []
        self.name = ""
        self.title_result = PersistResult(ok=True)
        self.titled: list[str] = []
        self.generated: str | None = "Fix the login"
        self.raises: Exception | None = None
        self.busy_flags: list[bool] = []

    async def list_sessions(self, limit: int | None = None) -> list[SessionListRow]:
        self.limits.append(limit)
        return self.rows

    async def get_name(self) -> str:
        return self.name

    async def title(self, name: str) -> PersistResult:
        self.titled.append(name)
        return self.title_result

    async def auto_name(self, model: Any, *, model_busy: bool = False) -> str | None:
        self.busy_flags.append(model_busy)
        if self.raises is not None:
            raise self.raises
        return self.generated


class Rig:
    def __init__(self) -> None:
        self.persistence = Persistence()
        self.identity = SessionIdentity("s-1", False, "me", lambda: None, lambda: None)
        self.rebound: list[str] = []
        self.session = SimpleNamespace(
            persistence=self.persistence,
            identity=self.identity,
            main_team=SimpleNamespace(model="the-model"),
            rebind_identity=self._rebind,
        )
        self.controller = SessionsController(self.session, self._history)  # type: ignore[arg-type]

    async def _rebind(self, session_id: str) -> None:
        self.rebound.append(session_id)

    async def _history(self, session_id: str) -> list[dict]:
        return []


async def test_the_list_is_every_session_as_wire_entries() -> None:
    rig = Rig()
    rig.persistence.rows = [
        SessionListRow(session_id="a", name="First", created_at=1, updated_at=5, run_count=2),
        SessionListRow(session_id="b", summary="about b", agent_name="igni"),
    ]

    listed = await rig.controller.list_sessions()

    assert rig.persistence.limits == [None], "uncapped: the window virtualises the list"
    assert all(isinstance(e, msg.SessionListEntry) for e in listed.sessions)
    assert [e.session_id for e in listed.sessions] == ["a", "b"]
    first, second = listed.sessions
    assert (first.name, first.created_at, first.updated_at, first.run_count) == ("First", 1, 5, 2)
    assert (second.summary, second.agent_name, second.name) == ("about b", "igni", "")


async def test_a_title_that_took_marks_the_session_named() -> None:
    rig = Rig()

    result = await rig.controller.title("Release notes")

    assert result.ok
    assert rig.persistence.titled == ["Release notes"]
    assert rig.identity.session_named is True


async def test_a_title_that_was_refused_leaves_the_session_to_be_auto_named() -> None:
    rig = Rig()
    rig.persistence.title_result = PersistResult(ok=False, error="disk full")

    result = await rig.controller.title("Release notes")

    assert not result.ok and result.error == "disk full"
    assert rig.identity.session_named is False


async def test_switching_says_which_session_it_switched_to() -> None:
    rig = Rig()
    rig.persistence.name = "Release notes"

    named = await rig.controller.switch_session("s-2")
    rig.persistence.name = ""
    unnamed = await rig.controller.switch_session("s-3")

    assert rig.rebound == ["s-2", "s-3"]
    assert named.text == "Switched to session: Release notes"
    assert unnamed.text == "Switched to session: s-3", "no name: the id"


async def test_an_auto_name_that_fails_is_reported_not_raised() -> None:
    rig = Rig()
    rig.persistence.raises = RuntimeError("model unreachable")

    result = await rig.controller.maybe_auto_name_session()

    assert (result.ok, result.name, result.reason) == (False, "", "error")


async def test_an_empty_name_is_no_name() -> None:
    rig = Rig()
    rig.persistence.generated = ""

    result = await rig.controller.maybe_auto_name_session()

    assert (result.ok, result.name, result.reason) == (False, "", "no_name_produced")


async def test_a_generated_name_is_reported_and_a_busy_model_is_passed_on() -> None:
    rig = Rig()

    result = await rig.controller.maybe_auto_name_session(model_busy=True)

    assert (result.ok, result.name, result.reason) == (True, "Fix the login", "generated")
    assert rig.persistence.busy_flags == [True]


async def test_a_named_session_is_left_alone() -> None:
    rig = Rig()
    rig.persistence.name = "Mine"

    result = await rig.controller.maybe_auto_name_session()

    assert (result.ok, result.name, result.reason) == (False, "Mine", "already_named")
    assert rig.persistence.busy_flags == [], "the model was not asked"
