"""The invariants every step of every scenario is held to."""

from __future__ import annotations

import asyncio
import time

from tests.session_lifecycle.lifecycle_client import LifecycleClient
from tests.session_lifecycle.lifecycle_rig import SECRET_CANARY, LifecycleRig
from tests.session_lifecycle.schemas.expected_turn import ExpectedTurn, TurnFate
from tests.session_lifecycle.schemas.recorded_request import RecordedRequest
from tests.session_lifecycle.schemas.violation import Violation

# Run frames: what a working run sends. None may follow a cancel.
PROMPT_TOKENS = 100

RUN_FRAMES = frozenset(
    {
        "run_started",
        "content_delta",
        "tool_started",
        "tool_completed",
        "model_completed",
        "run_paused",
    }
)


class InvariantChecker:
    """Records every invariant one scenario breaks, step by step."""

    # How long after Escape/Stop the session is watched before it is judged.
    CANCEL_WATCH_SECONDS = 4.0

    def __init__(self, rig: LifecycleRig, marker: str) -> None:
        self.rig = rig
        self.marker = marker
        self.violations: list[Violation] = []
        self.expected: list[ExpectedTurn] = []
        # Texts of requests that are a sub-agent's, not the conversation's.
        self.side_markers: list[str] = []
        self._seen: set[tuple[str, str, str]] = set()
        self._history_floor = 0.0
        # Requests before this index belong to earlier scenarios on the same rig.
        self._first_request = len(rig.model.requests)

    # ── What the scenario tells the checker ──────────────────────

    def expect(self, text: str, kind: str = "message") -> None:
        """``text`` went into the conversation; the model must see it from now on."""
        self.expected.append(
            ExpectedTurn(text=text, kind=kind, sent_at=time.monotonic())  # type: ignore[arg-type]
        )

    def mark(self, fate: TurnFate) -> None:
        """What became of the latest turn's run."""
        if self.expected:
            self.expected[-1].fate = fate

    def cleared(self) -> None:
        """``/clear``: the conversation starts over; earlier turns are no longer owed."""
        self.expected = []
        self._history_floor = time.monotonic()

    def fail(self, invariant: str, step: str, detail: str) -> None:
        key = (invariant, step, detail)
        if key in self._seen:
            return
        self._seen.add(key)
        self.violations.append(Violation(invariant=invariant, step=step, detail=detail))

    # ── What the model was sent ──────────────────────────────────

    def conversation_requests(self) -> list[RecordedRequest]:
        """The agent's requests in this scenario's conversation, not its sub-agents'."""
        return [
            r
            for r in self.rig.model.agent_requests()
            if r.mentions(self.marker)
            and not any(side in r.messages[-1].text for side in self.side_markers if r.messages)
            and not self._is_side(r)
        ]

    def _is_side(self, request: RecordedRequest) -> bool:
        users = request.user_texts()
        return bool(users) and any(side in users[0] for side in self.side_markers)

    def check_requests(self, step: str) -> None:
        """``single-run``, ``history`` and ``no-secret`` over every request so far."""
        for request in self.rig.model.snapshot()[self._first_request :]:
            if SECRET_CANARY in request.body:
                self.fail(
                    "no-secret", step, f"request #{request.index} carried a secret file's contents"
                )
        requests = self.conversation_requests()
        self._check_overlap(step, requests)
        for request in requests:
            if request.received_at >= self._history_floor:
                self._check_history(step, request)

    def _check_overlap(self, step: str, requests: list[RecordedRequest]) -> None:
        now = time.monotonic()
        for i, a in enumerate(requests):
            a_end = a.finished_at if a.finished_at is not None else now
            for b in requests[i + 1 :]:
                if b.received_at < a_end - 0.01:
                    self.fail(
                        "single-run",
                        step,
                        f"two model streams of one conversation at once: #{a.index} "
                        f"({_label(a)}) and #{b.index} ({_label(b)})",
                    )

    def _check_history(self, step: str, request: RecordedRequest) -> None:
        if not self.expected or not request.messages:
            return
        users = [m for m in request.messages if m.role == "user"]
        latest = users[-1].text if users else ""
        current = [i for i, turn in enumerate(self.expected) if turn.text in latest]
        if current:
            upto = max(current)
        else:
            anywhere = [i for i, turn in enumerate(self.expected) if request.mentions(turn.text)]
            if not anywhere:
                return
            upto = max(anywhere)
        positions: list[int] = []
        for turn in self.expected[: upto + 1]:
            in_user = [i for i, m in enumerate(users) if turn.text in m.text]
            if len(in_user) > 1:
                self.fail(
                    "history",
                    step,
                    f"request #{request.index} shows the {turn.fate} turn {turn.text!r} "
                    f"{len(in_user)} times",
                )
            if in_user:
                positions.append(in_user[0])
            elif not request.mentions(turn.text):
                self.fail(
                    "history",
                    step,
                    f"request #{request.index} ({_label(request)}) lost the {turn.fate} "
                    f"turn {turn.text!r}",
                )
        if positions != sorted(positions):
            self.fail("history", step, f"request #{request.index} shows the turns out of order")

    # ── What the window and the backend say ──────────────────────

    async def check_settled(self, step: str, client: LifecycleClient) -> None:
        """``stop-affordance``: the window agrees with the backend, once nothing moves."""
        await client.quiet()
        state, waiting = await self.backend_state(client)
        on_disk = sorted(self.rig.db.waiting_approvals(client.session_id))
        on_screen = sorted(p.requirement_id for p in client.view.pending)
        active = state != "idle"
        if client.view.stop_armed != active:
            self.fail(
                "stop-affordance",
                step,
                f"the window's Stop is {'armed' if client.view.stop_armed else 'off'} "
                f"(phase {client.view.phase}) while the backend says {state}",
            )
        if on_screen != on_disk:
            self.fail(
                "stop-affordance",
                step,
                f"the window shows approvals {on_screen}, the backend waits on {on_disk}",
            )
        if waiting != len(on_disk):
            self.fail(
                "stop-affordance",
                step,
                f"the sidebar counts {waiting} waiting approvals, the backend holds {len(on_disk)}",
            )
        if client.view.sidebar_state != state:
            self.fail(
                "stop-affordance",
                step,
                f"the last session_states_changed said {client.view.sidebar_state}, "
                f"list_session_states says {state}",
            )

    async def backend_state(self, client: LifecycleClient) -> tuple[str, int]:
        """The session's state and waiting approvals, as ``list_session_states`` answers."""
        states = await client.rpc("list_session_states")
        for entry in (states or {}).get("sessions") or []:
            if entry.get("session_id") == client.session_id:
                return str(entry.get("state", "idle")), int(entry.get("waiting_approvals") or 0)
        return "idle", 0

    async def check_cancelled(
        self, step: str, client: LifecycleClient, cancelled_at: float, frame_mark: int
    ) -> None:
        """``cancel-clears`` and ``quiet-after-cancel``, a moment after Escape/Stop."""
        await asyncio.sleep(max(0.0, cancelled_at + self.CANCEL_WATCH_SECONDS - time.monotonic()))
        await client.quiet(0.5)
        processing = await client.processing()
        state, waiting = await self.backend_state(client)
        on_disk = self.rig.db.journal_rows(client.session_id)
        open_streams = [r for r in self.conversation_requests() if r.outcome == "streaming"]
        if processing:
            self.fail("cancel-clears", step, "get_processing still says a run is working")
        if state != "idle":
            self.fail("cancel-clears", step, f"the session is {state} after the cancel, not idle")
        if waiting or on_disk or client.view.pending:
            self.fail(
                "cancel-clears",
                step,
                f"approvals still wait after the cancel: sidebar {waiting}, disk {on_disk}, "
                f"window {[p.requirement_id for p in client.view.pending]}",
            )
        if open_streams:
            self.fail(
                "cancel-clears",
                step,
                f"model streams still open after the cancel: {[r.index for r in open_streams]}",
            )
        late_calls = [r for r in self.conversation_requests() if r.received_at > cancelled_at + 0.3]
        if late_calls:
            self.fail(
                "quiet-after-cancel",
                step,
                f"{len(late_calls)} model call(s) after the cancel: "
                f"{[(r.index, _label(r)) for r in late_calls]}",
            )
        late_frames = [
            f.type
            for f in client.frames[frame_mark:]
            if f.session_id == client.session_id
            and f.type in RUN_FRAMES
            and f.received_at > cancelled_at + 0.5
        ]
        if late_frames:
            self.fail(
                "quiet-after-cancel",
                step,
                f"run frames after the cancel: {sorted(set(late_frames))}",
            )

    def check_stream_ends(self, step: str, client: LifecycleClient) -> None:
        """``stream-ends``: every streamed request the window made has its ``stream_end``."""
        ended = {f.id for f in client.frames if f.type == "stream_end"}
        for sent in client.sent:
            if (
                sent.kind == "message"
                and sent.request_id
                and sent.request_id not in ended
                and sent.request_id not in client.abandoned
            ):
                self.fail("stream-ends", step, f"{sent.text!r} never got its stream_end")

    def check_zombies(self, step: str, client: LifecycleClient) -> None:
        """``single-run``: no frame of an older top-level run after a newer one started."""
        started: list[str] = []
        for frame in client.frames:
            if frame.session_id != client.session_id or not frame.run_id or not frame.top_level:
                continue
            if frame.type == "run_started" and frame.run_id not in started:
                started.append(frame.run_id)
                continue
            if frame.run_id in started and started[-1] != frame.run_id:
                self.fail(
                    "single-run",
                    step,
                    f"run {frame.run_id[:8]} sent {frame.type} after run {started[-1][:8]} started",
                )

    def check_persisted(self, step: str, client: LifecycleClient) -> None:
        """``persisted``: the disk agrees with the window once the session is idle."""
        rows = self.rig.db.received(client.session_id)
        pending = [r.text for r in rows if r.status == "pending" and not r.interrupted_reason]
        if pending:
            self.fail("persisted", step, f"messages still pending on disk while idle: {pending}")
        journal = self.rig.db.journal_rows(client.session_id)
        if journal:
            self.fail(
                "persisted", step, f"the approvals journal still holds rows while idle: {journal}"
            )
        for turn in self.expected:
            if turn.kind != "message":
                continue
            count = sum(1 for r in rows if turn.text in r.text)
            if count > 1:
                self.fail("persisted", step, f"{turn.text!r} is recorded {count} times")
        saved = {run.run_id for run in self.rig.db.runs(client.session_id)}
        told = {
            f.run_id
            for f in client.frames
            if f.type == "run_completed" and f.top_level and f.session_id == client.session_id
        }
        missing = sorted(r[:8] for r in told - saved)
        if missing:
            self.fail("persisted", step, f"runs the window saw complete are not saved: {missing}")

    def check_tokens(self, step: str, client: LifecycleClient) -> None:
        """``tokens-once``: every model call is counted once — per run, and in the session."""
        mine = [f for f in client.frames if f.session_id == client.session_id and f.top_level]
        completions: dict[str, int] = {}
        calls: dict[str, int] = {}
        reported: dict[str, int] = {}
        for frame in mine:
            if frame.type == "run_completed":
                completions[frame.run_id] = completions.get(frame.run_id, 0) + 1
                reported[frame.run_id] = int(frame.data.get("input_tokens") or 0)
            elif frame.type == "model_completed":
                calls[frame.run_id] = calls.get(frame.run_id, 0) + int(
                    frame.data.get("input_tokens") or 0
                )
        for run_id, count in completions.items():
            if count > 1:
                self.fail("tokens-once", step, f"run {run_id[:8]} completed {count} times")
            if reported[run_id] != calls.get(run_id, 0):
                self.fail(
                    "tokens-once",
                    step,
                    f"run {run_id[:8]} reports {reported[run_id]} input tokens; its model calls "
                    f"reported {calls.get(run_id, 0)}",
                )
        total = self.rig.db.session_input_tokens(client.session_id)
        expected = sum(
            PROMPT_TOKENS for r in self.conversation_requests() if r.outcome == "completed"
        )
        if total is not None and total != expected:
            self.fail(
                "tokens-once" if total > expected else "tokens-lost",
                step,
                f"the session's saved total is {total} input tokens; its completed model calls "
                f"used {expected}",
            )


def _label(request: RecordedRequest) -> str:
    return f"{request.rule or 'unscripted'} step {request.step}"
