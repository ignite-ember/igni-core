"""A message sent during a turn is read after that turn, never beside it.

Session 6fa3e09e: the person asked "Who did I have a dream speech"; the run
paused on a shell call, the person approved it, and while the resumed run
worked they wrote "No, search internet for this one". igni started a second
run beside the first. Its history had neither the question nor the work —
Agno leaves a run that is not finished out of the history — so "this one"
was read against an older, finished turn. Then both runs paused.

What happens instead, as ``docs/ARCHITECTURE.md`` and ``docs/CLOUD.md``
describe it:

* while a turn works — a new one, or one resumed after an approval — the
  message is queued for it: the turn takes it after its next tool call, or
  runs it next when it ends with it still queued;
* while a turn stands paused on an approval, the message ends that turn,
  as a cancel does — its approvals go — and the turn stays in the history
  with its waiting calls declined, so the message is read after it.

Every test runs the orchestrator the app builds over a real backend, with
the real agent, its tools, hooks and approvals and a real database; only
the language model is a script, so what it was shown is what it is asked.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.base import RunStatus

from igni_core.backend.activity import ActivityState
from igni_core.backend.schemas_pool import ExtensionPost
from igni_core.backend.server import BackendServer
from igni_core.core.approvals.declined import declined_note
from igni_core.core.config.models import ModelRegistry
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.core.hooks.queue.schemas import USER_NOTE_HEADER
from igni_core.core.session.persistence.run_history import RunHistory
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.envelope import Standing
from tests.host_model_helpers import HostRig

QUESTION = "Who did I have a dream speech"
FOLLOW_UP = "No, search internet for this one"
DECLINED = "Blocked: the person declined 'run_shell_command'."


class Shown:
    """One model call: what the model was shown, as ``role: text`` lines."""

    def __init__(self, messages: list[Message]) -> None:
        self.lines = [self._line(m) for m in messages if m.role != "system"]

    @staticmethod
    def _line(message: Message) -> str:
        calls = [c.get("function", {}) for c in message.tool_calls or []]
        called = "".join(f" calls {c.get('name')}({c.get('arguments')})" for c in calls)
        return f"{message.role}: {message.content}{called}"

    def position(self, text: str) -> int:
        """Where ``text`` first shows in the conversation, or -1."""
        return "\n".join(self.lines).find(text)

    def has(self, text: str) -> bool:
        return self.position(text) >= 0


class Brain:
    """The scripted model: what to answer, from the conversation so far.

    A user prompt naming ``deploy`` asks the shell to touch a file (an
    approval in the default mode); ``slow`` holds the first answer until
    released; a shell or read result is answered as :attr:`after_tool`
    says. Each call is recorded, and how many ran at once.
    """

    def __init__(self) -> None:
        self.shown: list[Shown] = []
        self.release = asyncio.Event()
        self.release.set()
        self.holding = asyncio.Event()
        #: After a shell result: ``read`` (held), ``deploy`` (another shell call) or ``answer``.
        self.after_shell = "answer"
        self.running = 0
        self.most_at_once = 0
        self.calls = 0

    def hold(self) -> None:
        self.release.clear()
        self.holding.clear()

    async def answer(self, messages: list[Message]) -> ModelResponse:
        if any("Title:" in str(m.content) for m in messages if m.role == "user"):
            return ModelResponse(role="assistant", content="Dream speech")
        self.calls += 1
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        try:
            self.shown.append(Shown(messages))
            return await self._decide(messages)
        finally:
            self.running -= 1

    async def _held(self) -> None:
        self.holding.set()
        await self.release.wait()

    async def _decide(self, messages: list[Message]) -> ModelResponse:
        last = messages[-1]
        if last.role == "user":
            text = str(last.content)
            if "slow" in text:
                await self._held()
                return _call("read-1", "list_processes")
            if "pause later" in text:
                await self._held()
                return _call(f"shell-{self.calls}", "run_shell_command", command="touch late.txt")
            if "think" in text:
                await self._held()
                return ModelResponse(role="assistant", content="Thought it over.")
            if "look first" in text:
                return _call("look-1", "list_processes")
            if "deploy" in text or QUESTION in text:
                return _call("shell-1", "run_shell_command", command="touch deployed.txt")
            return ModelResponse(role="assistant", content="On it.")
        if last.role == "tool" and last.tool_call_id == "look-1":
            return _call("shell-1", "run_shell_command", command="touch deployed.txt")
        if last.role == "tool" and str(last.content).startswith("Blocked:"):
            return ModelResponse(role="assistant", content="Not run.")
        if last.role == "tool" and last.tool_name == "run_shell_command":
            if self.after_shell == "deploy" and last.tool_call_id != "shell-again":
                return _call("shell-again", "run_shell_command", command="touch again.txt")
            if self.after_shell == "read":
                await self._held()
                return _call(f"read-{self.calls}", "list_processes")
            return ModelResponse(role="assistant", content="Deployed.")
        return ModelResponse(role="assistant", content="Done.")


def _call(call_id: str, name: str, **arguments: Any) -> ModelResponse:
    return ModelResponse(
        role="assistant",
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    )


class ScriptedModel(Model):
    """A model that answers from :class:`Brain`."""

    def __init__(self, brain: Brain) -> None:
        super().__init__(id="scripted", provider="test")
        self._brain = brain

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        yield await self._brain.answer(kwargs["messages"])

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return await self._brain.answer(kwargs["messages"])

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:  # pragma: no cover
        raise NotImplementedError

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


class Rig(HostRig):
    """The app's orchestrator over a real boot backend, its queue the backend's own."""

    def __init__(self, tmp_path: Path, resume: str | None = None) -> None:
        super().__init__(tmp_path)
        (self.project / "notes.txt").write_text("the notes\n")
        self.backend = BackendServer(
            SettingsLoader.load(project_dir=self.project),
            project_dir=self.project,
            resume_session_id=resume,
        )
        self.session = self.backend  # type: ignore[assignment]

    async def start(self, **_: Any) -> Rig:  # type: ignore[override]
        queue = self.backend.message_queue
        assert queue is not None
        self.backend.wire_queue_hook(queue)
        await super().start(queue=queue)
        return self

    def send(self, message: msg.Message) -> asyncio.Task[None]:
        """Sent from igni's window, handled in the background as the serve loop does."""
        message.mark_from("ws-1", Standing.WINDOW)
        assert self.orchestrator is not None
        return self.orchestrator.spawn_dispatch(message)

    def waiting(self) -> list[str]:
        return [
            r.requirement_id
            for m in self.backend.controllers.hitl.waiting_messages()
            for r in m.requirements
        ]

    def queued_echoes(self) -> list[str]:
        return [
            m.text for m in self.windows.sent if isinstance(m, msg.UserMessageReceived) and m.queued
        ]

    def stream_ends(self) -> list[str]:
        """The request ids every window was told are finished, in order."""
        return [m.id for m in self.windows.sent if isinstance(m, msg.StreamEnd)]

    def errors(self, request_id: str) -> list[str]:
        return [
            m.text for m in self.windows.sent if isinstance(m, msg.Error) and m.id == request_id
        ]

    async def stored_runs(self) -> list[Any]:
        found = await self.backend._session.db.get_runs(  # noqa: SLF001 — the stored history
            session_id=self.backend.session_id, deserialize=True
        )
        runs = found[0] if isinstance(found, tuple) else found
        return [r for r in runs or [] if r.parent_run_id is None]

    async def settle(self) -> None:
        """Let the tasks the messages started finish."""
        assert self.orchestrator is not None
        pending = self.orchestrator.in_flight.pending()
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), 30)


@pytest.fixture
def brain(monkeypatch) -> Brain:
    brain = Brain()
    monkeypatch.setattr(ModelRegistry, "get_model", lambda self, name=None: ScriptedModel(brain))
    monkeypatch.setattr(ModelRegistry, "get_context_window", lambda self, name=None: 200_000)
    return brain


@pytest.fixture
async def rig(tmp_path: Path, brain: Brain):
    rig = await Rig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.settle()
        await rig.stop()


async def _until(condition: Callable[[], bool], timeout: float = 10) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("the condition never held")
        await asyncio.sleep(0.01)


async def _paused_on_the_question(rig: Rig) -> str:
    await rig.send(msg.UserMessage(id="m-1", text=QUESTION))
    (requirement_id,) = rig.waiting()
    return requirement_id


def _read_after_the_question(brain: Brain, text: str) -> list[Shown]:
    """The calls that were shown ``text``; each must show the question before it."""
    saw = [s for s in brain.shown if s.has(text)]
    assert saw, f"the model was never shown {text!r}"
    for shown in saw:
        assert 0 <= shown.position(QUESTION) < shown.position(text), shown.lines
    return saw


# ── While a turn works ───────────────────────────────────────────


async def test_a_follow_up_while_the_resumed_turn_works_joins_that_turn(
    rig: Rig, brain: Brain
) -> None:
    """Session 6fa3e09e, step by step."""
    requirement_id = await _paused_on_the_question(rig)
    brain.after_shell = "read"
    brain.hold()
    resumed = rig.send(msg.HITLResponse(id="h-1", requirement_id=requirement_id, action="confirm"))
    await asyncio.wait_for(brain.holding.wait(), 10)
    calls = brain.calls

    follow_up = rig.send(msg.UserMessage(id="m-2", text=FOLLOW_UP))
    await asyncio.wait_for(follow_up, 10)

    assert brain.calls == calls, "no run starts beside the working turn"
    assert rig.queued_echoes() == [FOLLOW_UP], "every window draws it queued"
    brain.release.set()
    await asyncio.wait_for(resumed, 30)
    await rig.settle()

    assert (rig.project / "deployed.txt").exists(), "the approved call ran"
    assert brain.most_at_once == 1
    (saw,) = _read_after_the_question(brain, FOLLOW_UP)
    assert saw.has(USER_NOTE_HEADER), "taken in after the turn's next tool call"
    runs = await rig.stored_runs()
    assert len(runs) == 1 and runs[0].status == RunStatus.completed, "one turn, finished"
    assert rig.backend.message_queue is not None and len(rig.backend.message_queue) == 0


async def test_a_follow_up_while_a_new_turn_works_joins_that_turn(rig: Rig, brain: Brain) -> None:
    brain.hold()
    first = rig.send(msg.UserMessage(id="m-1", text=f"{QUESTION}, slow"))
    await asyncio.wait_for(brain.holding.wait(), 10)

    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-2", text=FOLLOW_UP)), 10)
    assert brain.calls == 1, "no run starts beside the working turn"
    brain.release.set()
    await asyncio.wait_for(first, 30)
    await rig.settle()

    (saw,) = _read_after_the_question(brain, FOLLOW_UP)
    assert saw.has(USER_NOTE_HEADER)
    runs = await rig.stored_runs()
    assert len(runs) == 1
    users = [m.content for m in runs[0].messages if m.role == "user"]
    assert users[-1] == FOLLOW_UP, "the conversation records it as the person's message"


async def test_a_follow_up_a_working_turn_never_took_runs_next_with_that_turn_read_first(
    rig: Rig, brain: Brain
) -> None:
    brain.hold()
    first = rig.send(msg.UserMessage(id="m-1", text=f"{QUESTION}, think"))
    await asyncio.wait_for(brain.holding.wait(), 10)

    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-2", text=FOLLOW_UP)), 10)
    assert brain.calls == 1
    brain.release.set()
    await asyncio.wait_for(first, 30)
    await rig.settle()

    (saw,) = _read_after_the_question(brain, FOLLOW_UP)
    assert saw.position("Thought it over.") < saw.position(FOLLOW_UP), "after the turn's answer"
    assert not saw.has(USER_NOTE_HEADER), "its own turn, not a note on a tool result"
    runs = await rig.stored_runs()
    assert [r.status for r in runs] == [RunStatus.completed, RunStatus.completed]


async def test_two_quick_follow_ups_are_read_once_each_in_the_order_sent(
    rig: Rig, brain: Brain
) -> None:
    brain.hold()
    first = rig.send(msg.UserMessage(id="m-1", text=f"{QUESTION}, slow"))
    await asyncio.wait_for(brain.holding.wait(), 10)

    one = rig.send(msg.UserMessage(id="m-2", text="first follow-up"))
    two = rig.send(msg.UserMessage(id="m-3", text="second follow-up"))
    await asyncio.wait_for(asyncio.gather(one, two), 10)
    assert brain.calls == 1
    assert rig.queued_echoes() == ["first follow-up", "second follow-up"]
    brain.release.set()
    await asyncio.wait_for(first, 30)
    await rig.settle()

    last = brain.shown[-1]
    assert 0 <= last.position(QUESTION) < last.position("first follow-up")
    assert last.position("first follow-up") < last.position("second follow-up")
    runs = await rig.stored_runs()
    users = [m.content for r in runs for m in r.messages or [] if m.role == "user"]
    assert users.count("first follow-up") == 1 and users.count("second follow-up") == 1
    assert len(runs) == 1, "both taken into the turn; no turn of their own"


async def test_an_approval_answered_after_a_queued_follow_up_resumes_the_turn_that_takes_it(
    rig: Rig, brain: Brain
) -> None:
    brain.hold()
    first = rig.send(msg.UserMessage(id="m-1", text=f"{QUESTION}, pause later"))
    await asyncio.wait_for(brain.holding.wait(), 10)
    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-2", text=FOLLOW_UP)), 10)
    brain.after_shell = "read"
    brain.release.set()
    await asyncio.wait_for(first, 30)

    (requirement_id,) = rig.waiting()
    assert brain.calls == 1, "the queued message waits for the paused turn"
    assert rig.backend.activity().state is ActivityState.WAITING, "shown as waiting"
    listed = await rig.backend.list_sessions()
    assert [s.has_paused_run for s in listed.sessions] == [True]
    assert list(rig.backend.message_queue or []) == [FOLLOW_UP], "still queued, visibly"

    resumed = rig.send(msg.HITLResponse(id="h-1", requirement_id=requirement_id, action="confirm"))
    await asyncio.wait_for(brain.holding.wait(), 10)
    brain.release.set()
    await asyncio.wait_for(resumed, 30)
    await rig.settle()

    assert (rig.project / "late.txt").exists(), "the approved call ran"
    saw = _read_after_the_question(brain, FOLLOW_UP)[0]
    assert 0 <= saw.position("touch late.txt") < saw.position(USER_NOTE_HEADER)
    assert saw.position(USER_NOTE_HEADER) < saw.position(FOLLOW_UP), "on the approved call's result"
    assert brain.most_at_once == 1
    assert rig.backend.activity().state is ActivityState.IDLE


# ── While a turn stands paused ───────────────────────────────────


async def test_a_follow_up_while_a_turn_waits_on_an_approval_is_read_after_that_turn(
    rig: Rig, brain: Brain
) -> None:
    await _paused_on_the_question(rig)

    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-2", text=FOLLOW_UP)), 30)
    await rig.settle()

    assert rig.waiting() == [], "the new message ends the pause"
    assert not (rig.project / "deployed.txt").exists(), "its call never runs"
    saw = _read_after_the_question(brain, FOLLOW_UP)[0]
    assert 0 <= saw.position(DECLINED) < saw.position(FOLLOW_UP), saw.lines
    runs = await rig.stored_runs()
    assert [r.status for r in runs] == [RunStatus.completed, RunStatus.completed]
    paused = runs[0]
    (declined,) = [m for m in paused.messages if m.role == "tool"]
    assert declined.content == DECLINED and declined.tool_call_id == "shell-1"
    assert declined.tool_call_error is True
    (tool,) = paused.tools
    assert tool.confirmed is False and tool.requires_confirmation is False
    assert [r.confirmation for r in paused.requirements] == [False]


async def test_answering_the_approval_after_the_follow_up_runs_nothing(
    rig: Rig, brain: Brain
) -> None:
    requirement_id = await _paused_on_the_question(rig)
    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-2", text=FOLLOW_UP)), 30)
    calls = brain.calls

    answer = msg.HITLResponse(id="h-1", requirement_id=requirement_id, action="confirm")
    await asyncio.wait_for(rig.send(answer), 30)
    await rig.settle()

    assert rig.errors("h-1") == [f"Unknown requirement: {requirement_id}"]
    assert not (rig.project / "deployed.txt").exists()
    assert brain.calls == calls, "nothing continues the turn the person moved on from"
    assert rig.backend.activity().state is ActivityState.IDLE
    listed = await rig.backend.list_sessions()
    assert [s.has_paused_run for s in listed.sessions] == [False]


async def test_a_follow_up_to_a_turn_a_restart_left_paused_is_read_after_that_turn(
    tmp_path: Path, brain: Brain
) -> None:
    before = await Rig(tmp_path).start()
    try:
        await _paused_on_the_question(before)
        session_id = before.backend.session_id
    finally:
        await before.settle()
        await before.stop()

    after = await Rig(tmp_path, resume=session_id).start()
    try:
        assert len(after.waiting()) == 1, "the approval is asked again"
        await asyncio.wait_for(after.send(msg.UserMessage(id="m-2", text=FOLLOW_UP)), 30)
        await after.settle()

        assert after.waiting() == []
        saw = _read_after_the_question(brain, FOLLOW_UP)[0]
        assert 0 <= saw.position(DECLINED) < saw.position(FOLLOW_UP)
        assert not (after.project / "deployed.txt").exists()
    finally:
        await after.settle()
        await after.stop()


async def test_a_paused_turn_shows_as_waiting_until_a_follow_up_ends_it(
    rig: Rig, brain: Brain
) -> None:
    await _paused_on_the_question(rig)
    assert rig.backend.activity().state is ActivityState.WAITING
    listed = await rig.backend.list_sessions()
    assert [s.has_paused_run for s in listed.sessions] == [True]

    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-2", text=FOLLOW_UP)), 30)
    await rig.settle()

    assert rig.backend.activity().state is ActivityState.IDLE
    listed = await rig.backend.list_sessions()
    assert [s.has_paused_run for s in listed.sessions] == [False]
    assert not rig.queued_echoes(), "a message to a paused turn is not queued: it is the turn"


async def test_a_post_that_starts_a_turn_waits_for_the_resumed_turn_to_end(
    rig: Rig, brain: Brain
) -> None:
    """A turn started by other means than a person's message — an extension's
    ``chat.post`` — waits for the resumed turn too: one run at a time."""
    requirement_id = await _paused_on_the_question(rig)
    brain.after_shell = "read"
    brain.hold()
    resumed = rig.send(msg.HITLResponse(id="h-1", requirement_id=requirement_id, action="confirm"))
    await asyncio.wait_for(brain.holding.wait(), 10)
    calls = brain.calls

    assert rig.orchestrator is not None
    await rig.orchestrator.post_from_extension(
        ExtensionPost(
            session_id=rig.backend.session_id,
            extension_id="acme.reviewer",
            text="a note from the reviewer",
            queued=False,
        )
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert brain.calls == calls, "no run starts beside the resumed turn"
    brain.release.set()
    await asyncio.wait_for(resumed, 30)
    await rig.settle()

    assert brain.most_at_once == 1
    _read_after_the_question(brain, "a note from the reviewer")
    runs = await rig.stored_runs()
    assert [r.status for r in runs] == [RunStatus.completed, RunStatus.completed]


@pytest.mark.parametrize("resumed", [False, True], ids=["first-pause", "paused-again"])
async def test_a_follow_up_while_the_paused_turn_still_closes_ends_that_pause(
    rig: Rig, brain: Brain, resumed: bool
) -> None:
    """The pause has been streamed; the run only closes (its event log). A
    message then is one to a paused turn, not one to queue behind it."""
    runs = rig.backend.controllers.runs
    closing = asyncio.Event()
    let_go = asyncio.Event()
    keep_event_log = runs._keep_event_log  # noqa: SLF001 — the closing step, held open

    async def held_open(seq_before: int) -> None:
        if rig.waiting():
            closing.set()
            await let_go.wait()
        await keep_event_log(seq_before)

    if resumed:
        requirement_id = await _paused_on_the_question(rig)
        brain.after_shell = "deploy"
        runs._keep_event_log = held_open  # type: ignore[method-assign]  # noqa: SLF001
        turn = rig.send(msg.HITLResponse(id="h-1", requirement_id=requirement_id, action="confirm"))
    else:
        runs._keep_event_log = held_open  # type: ignore[method-assign]  # noqa: SLF001
        turn = rig.send(msg.UserMessage(id="m-1", text=QUESTION))
    await asyncio.wait_for(closing.wait(), 10)

    assert not rig.backend.turn_in_progress
    follow_up = rig.send(msg.UserMessage(id="m-2", text=FOLLOW_UP))
    for _ in range(20):
        await asyncio.sleep(0)
    let_go.set()
    await asyncio.wait_for(asyncio.gather(turn, follow_up), 30)
    await rig.settle()

    assert rig.queued_echoes() == [], "not queued behind a pause"
    assert rig.waiting() == []
    saw = _read_after_the_question(brain, FOLLOW_UP)[-1]
    assert 0 <= saw.position(DECLINED) < saw.position(FOLLOW_UP)


async def test_only_the_calls_left_waiting_are_declined(rig: Rig, brain: Brain) -> None:
    await rig.send(msg.UserMessage(id="m-1", text=f"{QUESTION}, look first"))
    assert len(rig.waiting()) == 1

    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-2", text=FOLLOW_UP)), 30)
    await rig.settle()

    paused = (await rig.stored_runs())[0]
    results = [(m.tool_call_id, m.content) for m in paused.messages if m.role == "tool"]
    assert [call for call, _ in results] == ["look-1", "shell-1"], "one result per call"
    assert results[1][1] == DECLINED and results[0][1] != DECLINED, (
        "the call that ran keeps its result"
    )
    saw = _read_after_the_question(brain, FOLLOW_UP)[0]
    assert saw.lines.count(f"tool: {DECLINED}") == 1


async def test_a_run_that_is_not_paused_is_never_closed(rig: Rig, brain: Brain) -> None:
    await rig.send(msg.UserMessage(id="m-1", text="hello"))
    (finished,) = await rig.stored_runs()
    history = RunHistory(rig.backend._session.db)  # noqa: SLF001 — the stored history

    closed = await history.close_declined(rig.backend.session_id, finished.run_id, declined_note)

    assert closed is False
    (again,) = await rig.stored_runs()
    assert again.status == RunStatus.completed
    assert [m.role for m in again.messages] == [m.role for m in finished.messages]


@pytest.mark.parametrize("resumed", [False, True], ids=["after-a-turn", "after-a-resumed-turn"])
async def test_once_a_turn_has_ended_the_next_message_starts_its_own(
    rig: Rig, brain: Brain, resumed: bool
) -> None:
    if resumed:
        requirement_id = await _paused_on_the_question(rig)
        await rig.send(msg.HITLResponse(id="h-1", requirement_id=requirement_id, action="confirm"))
    else:
        await rig.send(msg.UserMessage(id="m-1", text="hello"))
    assert not rig.backend.turn_in_progress
    calls = brain.calls

    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-2", text=FOLLOW_UP)), 30)

    assert rig.queued_echoes() == [], "nothing works: it is a turn of its own"
    assert brain.calls == calls + 1


async def test_every_request_queued_for_a_working_turn_gets_its_stream_end_at_once(
    rig: Rig, brain: Brain
) -> None:
    """A window waits for a request's ``stream_end`` without a timeout: a queued
    message's request ends as it is queued, the working turn's when it ends."""
    brain.hold()
    first = rig.send(msg.UserMessage(id="m-1", text=f"{QUESTION}, slow"))
    await asyncio.wait_for(brain.holding.wait(), 10)

    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-2", text="first follow-up")), 10)
    await asyncio.wait_for(rig.send(msg.UserMessage(id="m-3", text="second follow-up")), 10)

    assert rig.stream_ends() == ["m-2", "m-3"], "each queued request ended, the turn's still open"
    brain.release.set()
    await asyncio.wait_for(first, 30)
    await rig.settle()
    assert rig.stream_ends() == ["m-2", "m-3", "m-1"], "every request ended exactly once"
