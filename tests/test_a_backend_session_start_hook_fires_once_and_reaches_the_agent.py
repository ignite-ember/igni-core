"""The backend's SessionStart hook fires once per session, says whether it
resumed, and its context reaches the agent.

``Session.fire_session_start_hook`` reported ``resumed: true`` for a session
``sessions.create`` had just made (it read ``session_named``) and dropped the
hook's ``systemMessage``, so in a window the hook could tell the agent
nothing (campaign R117).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from igni_core.core.hooks.schemas import HookResult
from igni_core.core.session.core import Session


class _Executor:
    def __init__(self, result: HookResult) -> None:
        self.result = result
        self.payloads: list[Any] = []

    async def execute(self, *, event: str, payload: Any) -> HookResult:
        self.payloads.append(payload)
        return self.result


def _session(stored_runs: list[object] | None, message: str = "") -> tuple[Session, _Executor]:
    async def get_session(**_: object) -> object | None:
        return None if stored_runs is None else SimpleNamespace(runs=stored_runs)

    session = Session.__new__(Session)
    session._session_start_fired = set()
    session._session_start_context = ""
    executor = _Executor(HookResult(should_continue=True, message=message))
    session.hook_executor = executor  # type: ignore[assignment]
    session.persistence = SimpleNamespace(db=SimpleNamespace(get_session=get_session))  # type: ignore[assignment]
    session.identity = SimpleNamespace(session_id="s-new")  # type: ignore[assignment]
    return session, executor


async def test_a_new_session_is_not_resumed_and_fires_once() -> None:
    session, executor = _session(None)

    await session.fire_session_start_hook()
    await session.fire_session_start_hook()

    assert [p.resumed for p in executor.payloads] == [False]


async def test_a_session_with_a_stored_run_is_resumed() -> None:
    session, executor = _session([object()])

    await session.fire_session_start_hook()

    assert [p.resumed for p in executor.payloads] == [True]


async def test_the_hook_s_context_is_kept_for_one_turn() -> None:
    session, _ = _session(None, message="The mascot is OTTO-7.")

    await session.fire_session_start_hook()

    assert session.take_session_start_context() == "The mascot is OTTO-7."
    assert session.take_session_start_context() == ""
