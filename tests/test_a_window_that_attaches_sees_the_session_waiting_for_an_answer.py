"""A window that attaches to a session sees what that session waits for.

A run that paused on an approval leaves the person with a question, and
the window that showed it can lose it: a reload, a new tab, a session
picked from the sidebar. Then the run stays paused with nobody able to
answer it (campaign R647).

So attaching to a session shows its waiting approvals again — the
``run_paused`` carrying the requirements, which is the card the person
answers. The attach's own id rides on every message it brings, and a
``stream_end`` of that id ends them.

What we pin:

* the approval the attached session waits on comes back as a
  ``run_paused`` carrying that requirement, tagged with the attach's
  request id;
* the card carries the tool and its arguments — what the person answers;
* a session with nothing waiting shows no card;
* an approval that was answered is not shown again;
* another session's approval is not shown to the window that attached;
* a sub-agent's approval is shown too.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.pool_replier import PoolReplier
from igni_core.backend.pool_rpcs import AttachSessionRpc, PoolRpcRouter
from igni_core.backend.schemas_pause import PendingRequirement
from igni_core.backend.schemas_rpc import AttachSessionResult
from igni_core.backend.session_pool import SessionRuntime
from igni_core.core.hitl.models import PendingEntry
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class _Transport:
    """What the window would have received."""

    def __init__(self) -> None:
        self.sent: list[msg.Message] = []

    async def send(self, message: msg.Message) -> None:
        self.sent.append(message)

    def receive(self) -> Any:
        raise AssertionError("not read here")


class _Hitl:
    """The session's HITL controller, over the real store and coordinator."""

    def __init__(self, store: PendingRequirementsStore, coordinator: SubAgentHITLCoordinator):
        self._store = store
        self._coordinator = coordinator

    def waiting_messages(self) -> list[msg.RunPaused]:
        return [*self._store.waiting_messages(), *self._coordinator.waiting_requests()]

    async def resume_interrupted(self):
        for proto in ():
            yield proto


def _requirement(tool_name: str = "run_shell_command") -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id="call-1",
            tool_name=tool_name,
            tool_args={"command": "echo hello"},
            requires_confirmation=True,
        )
    )


def _request(requirement_id: str = "req-1") -> msg.HITLRequest:
    return msg.HITLRequest(
        requirement_id=requirement_id,
        tool_name="run_shell_command",
        friendly_name="Shell",
        tool_args={"command": "echo hello"},
        details="A shell command wants to run.",
    )


def _store_with_one_waiting(requirement_id: str = "req-1") -> PendingRequirementsStore:
    """A store holding one approval waiting on the person."""
    store = PendingRequirementsStore()
    store.register(
        requirement_id,
        PendingRequirement(req=_requirement(), run_id="run-1", request=_request(requirement_id)),
    )
    return store


def _runtime(
    session_id: str,
    store: PendingRequirementsStore,
    coordinator: SubAgentHITLCoordinator,
) -> tuple[SessionRuntime, _Transport]:
    """A runtime over a session, and the transport that session's windows read.

    The session's transport is its own: the approval a window is shown
    goes to the session (every window showing it), not to the one
    connection that asked to attach.
    """
    transport = _Transport()
    backend = SimpleNamespace(
        session_id=session_id,
        controllers=SimpleNamespace(hitl=_Hitl(store, coordinator)),
    )
    runtime = SessionRuntime(
        backend=backend,  # type: ignore[arg-type]
        rpc_table={},
        queue=[],
        transport=transport,  # type: ignore[arg-type]
    )
    return runtime, transport


class _Pool:
    """The pool, with one runtime per session."""

    def __init__(self, runtimes: dict[str, SessionRuntime]) -> None:
        self._runtimes = runtimes
        self.asked: list[str] = []

    async def get_or_create(self, session_id: str) -> SessionRuntime:
        self.asked.append(session_id)
        return self._runtimes[session_id]


class _Sessions:
    """The session router, as the attach handler sees it."""

    def __init__(self, pool: _Pool, session_id: str = "s1") -> None:
        self.pool = pool
        self.session_id = session_id

    async def attach(self, session_id: str, project_dir: str | None) -> Any:
        return AttachSessionResult(session_id=self.session_id, project_dir="/project")


async def _attach(
    runtimes: dict[str, SessionRuntime], session_id: str = "s1"
) -> tuple[list[msg.Message], list[msg.Message]]:
    """Attach to ``session_id``.

    Returns what the connection that attached was answered, and what the
    session was sent — the approvals it shows again, and the end of
    them.
    """
    connection = _Transport()
    router = PoolRpcRouter(PoolReplier(connection))  # type: ignore[arg-type]
    sessions = _Sessions(_Pool(runtimes))
    router.mount(AttachSessionRpc(sessions))  # type: ignore[arg-type]
    request = msg.RPCRequest(
        id="req-attach",
        method=RpcMethod.ATTACH_SESSION.value,
        args={"session_id": session_id},
    )
    await router.handle(request)
    shown = runtimes[session_id].transport  # type: ignore[attr-defined]
    return connection.sent, shown.sent  # type: ignore[attr-defined]


async def test_the_approval_the_session_waits_on_is_shown_again() -> None:
    """The window that attaches is shown the question it lost."""
    runtime, _ = _runtime("s1", _store_with_one_waiting(), SubAgentHITLCoordinator())

    _, shown = await _attach({"s1": runtime})

    paused = [m for m in shown if isinstance(m, msg.RunPaused)]
    assert len(paused) == 1
    assert [r.requirement_id for r in paused[0].requirements] == ["req-1"]
    assert paused[0].run_id == "run-1"


async def test_the_card_carries_what_the_person_is_asked_to_allow() -> None:
    """The tool and its arguments: the card the person answers."""
    runtime, _ = _runtime("s1", _store_with_one_waiting(), SubAgentHITLCoordinator())

    _, shown = await _attach({"s1": runtime})

    paused = next(m for m in shown if isinstance(m, msg.RunPaused))
    request = paused.requirements[0]
    assert request.tool_name == "run_shell_command"
    assert request.tool_args == {"command": "echo hello"}
    assert request.details == "A shell command wants to run."


async def test_everything_the_attach_brought_carries_the_attachs_id() -> None:
    """The window can tell what its attach brought back."""
    runtime, _ = _runtime("s1", _store_with_one_waiting(), SubAgentHITLCoordinator())

    _, shown = await _attach({"s1": runtime})

    assert [m.id for m in shown] == ["req-attach", "req-attach"]
    assert isinstance(shown[-1], msg.StreamEnd)
    assert shown[-1].session_id == "s1"


async def test_a_session_with_nothing_waiting_shows_no_card() -> None:
    """Nothing is waiting: there is no card to show."""
    runtime, _ = _runtime("s1", PendingRequirementsStore(), SubAgentHITLCoordinator())

    answered, shown = await _attach({"s1": runtime})

    assert [type(m) for m in answered] == [msg.RPCResponse]
    assert [type(m) for m in shown] == [msg.StreamEnd]


async def test_an_answered_approval_is_not_shown_again() -> None:
    """The person answered it: there is nothing left to answer."""
    store = _store_with_one_waiting()
    store.pop("req-1")
    runtime, _ = _runtime("s1", store, SubAgentHITLCoordinator())

    _, shown = await _attach({"s1": runtime})

    assert not [m for m in shown if isinstance(m, msg.RunPaused)]


async def test_another_sessions_approval_is_not_shown() -> None:
    """A window is shown the session it attached to, not another one."""
    mine, _ = _runtime("s1", _store_with_one_waiting(), SubAgentHITLCoordinator())
    other, other_transport = _runtime(
        "s2", _store_with_one_waiting("req-2"), SubAgentHITLCoordinator()
    )

    _, shown = await _attach({"s1": mine, "s2": other})

    paused = [m for m in shown if isinstance(m, msg.RunPaused)]
    assert len(paused) == 1
    assert [r.requirement_id for r in paused[0].requirements] == ["req-1"]
    assert not [m for m in other_transport.sent if isinstance(m, msg.RunPaused)]


async def test_a_sub_agents_approval_is_shown_too() -> None:
    """A sub-agent's question is a question too."""
    coordinator = SubAgentHITLCoordinator()
    coordinator.pending.add(
        "req-sub",
        PendingEntry(requirement=_requirement("write_file"), run_id="run-sub"),
    )
    runtime, _ = _runtime("s1", PendingRequirementsStore(), coordinator)

    _, shown = await _attach({"s1": runtime})

    paused = [m for m in shown if isinstance(m, msg.RunPaused)]
    assert len(paused) == 1
    assert paused[0].run_id == "run-sub"
    assert [r.tool_name for r in paused[0].requirements] == ["write_file"]
