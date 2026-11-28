"""A window of igni, driven by a test: the app's own wire messages over the real WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import time
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from tests.session_lifecycle.client_view import ClientView
from tests.session_lifecycle.schemas.sent_message import SentMessage
from tests.session_lifecycle.schemas.wire_frame import WireFrame


class LifecycleClient:
    """One connection to the backend, bound to one session at a time."""

    RPC_TIMEOUT_SECONDS = 30.0
    _ids = itertools.count(1)

    def __init__(self, url: str, token: str, name: str = "window") -> None:
        self.url = url
        self.token = token
        self.name = name
        self.client_id = ""
        self.session_id = ""
        self.frames: list[WireFrame] = []
        self.sent: list[SentMessage] = []
        # Streams the connection dropped before their end; the app gave up on them.
        self.abandoned: set[str] = set()
        self.view = ClientView()
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._arrived = asyncio.Event()

    # ── Connection ───────────────────────────────────────────────

    async def connect(self) -> LifecycleClient:
        self._ws = await connect(
            self.url,
            subprotocols=["igni.v1", f"igni-token.{self.token}"],  # type: ignore[list-item]
            max_size=None,
            open_timeout=30,
        )
        self._reader = asyncio.create_task(self._read(self._ws))
        await self.wait_for(lambda f: f.type == "welcome", since=0)
        return self

    async def close(self) -> None:
        """Close the socket cleanly, as a window that is closed does."""
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        await self._stop_reader()

    async def drop(self) -> None:
        """Lose the connection without a goodbye, as sleep or a network change does."""
        ws, self._ws = self._ws, None
        if ws is not None:
            ws.transport.abort()
        await self._stop_reader()
        # The app fails every reply it still waits for when its socket goes.
        ended = {f.id for f in self.frames if f.type == "stream_end"}
        self.abandoned |= {m.request_id for m in self.sent if m.request_id not in ended}

    async def reconnect(self) -> None:
        """A new socket, bound again to the same session (the app's reconnect)."""
        await self.drop()
        await self.connect()
        if self.session_id:
            await self.attach(self.session_id)
            await self.hydrate()

    async def reload(self) -> list[Any]:
        """A reload: a new socket, a clean view, the session attached and its history read."""
        session = self.session_id
        await self.drop()
        self.view = ClientView()
        await self.connect()
        await self.attach(session)
        await self.hydrate()
        return await self.history()

    async def hydrate(self) -> None:
        """What the app reads on (re)connect: the sidebar's states and whether a run works."""
        await asyncio.sleep(0.3)
        states = await self.rpc("list_session_states")
        mine = [
            s
            for s in (states or {}).get("sessions") or []
            if s.get("session_id") == self.session_id
        ]
        self.view.sidebar_state = str(mine[0].get("state", "idle")) if mine else "idle"
        self.view.sidebar_waiting = int(mine[0].get("waiting_approvals") or 0) if mine else 0
        if self.view.pending:
            self.view.phase = "awaiting_approval"
        elif await self.processing():
            self.view.phase = "streaming"
        elif self.view.phase in ("starting", "streaming", "finalizing", "awaiting_approval"):
            self.view.phase = "done"

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def _stop_reader(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader

    async def _read(self, ws: ClientConnection) -> None:
        with contextlib.suppress(Exception):
            async for raw in ws:
                data = json.loads(raw)
                payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
                frame = WireFrame(
                    type=str(data.get("type", "")),
                    id=str(data.get("id") or ""),
                    session_id=str(data.get("session_id") or payload.get("session_id") or ""),
                    run_id=str(data.get("run_id") or ""),
                    parent_run_id=str(data.get("parent_run_id") or ""),
                    channel=str(data.get("channel") or ""),
                    received_at=time.monotonic(),
                    data=data,
                )
                if frame.type == "welcome":
                    self.client_id = str(data.get("client_id", ""))
                self.frames.append(frame)
                self.view.apply(frame)
                self._arrived.set()

    # ── Sending ──────────────────────────────────────────────────

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{self.name}-{next(self._ids)}"

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._ws is not None, f"{self.name} is not connected"
        if self.session_id and not payload.get("session_id"):
            payload = {**payload, "session_id": self.session_id}
        await self._ws.send(json.dumps(payload))

    async def rpc(self, method: str, args: dict[str, Any] | None = None) -> Any:
        """Call ``method`` and return its result; raise with its error."""
        req = self._next_id("rpc")
        mark = len(self.frames)
        await self._send({"type": "rpc_request", "id": req, "method": method, "args": args or {}})
        reply = await self.wait_for(
            lambda f: f.id == req and f.type != "stream_end",
            since=mark,
            timeout=self.RPC_TIMEOUT_SECONDS,
        )
        if reply.type == "rpc_response":
            if reply.data.get("error"):
                raise RuntimeError(f"{method}: {reply.data['error']}")
            return reply.data.get("result")
        return reply.data

    async def attach(self, session_id: str | None = None) -> str:
        """Bind this window to ``session_id`` (or the default session) and attach it."""
        if session_id and session_id != self.session_id:
            self.session_id = session_id
            self.view.rebind(session_id)
        args = {"session_id": session_id} if session_id else {}
        result = await self.rpc("attach_session", args)
        attached = str(result["session_id"])
        if attached != self.session_id:
            self.session_id = attached
            self.view.rebind(attached)
        return attached

    async def send(self, text: str, force: bool = False) -> str:
        """A ``user_message``: what the composer sends when nothing is streaming."""
        req = self._next_id("run")
        self.sent.append(
            SentMessage(
                text=text,
                kind="message",
                session_id=self.session_id,
                request_id=req,
                sent_at=time.monotonic(),
            )
        )
        self.view.sent()
        await self._send(
            {
                "type": "user_message",
                "id": req,
                "text": text,
                "file_contents": {},
                "client_id": self.client_id,
                "force": force,
            }
        )
        return req

    async def queue(self, text: str) -> None:
        """A ``queue_message``: what the composer sends while a reply streams."""
        self.sent.append(
            SentMessage(
                text=text, kind="queued", session_id=self.session_id, sent_at=time.monotonic()
            )
        )
        await self._send({"type": "queue_message", "text": text, "client_id": self.client_id})

    async def follow_up(self, text: str) -> str:
        """What the app does with a line typed now: queued while streaming, else sent."""
        if self.view.queues_messages:
            await self.queue(text)
            return ""
        return await self.send(text)

    async def cancel(self) -> None:
        """A bare ``cancel``."""
        self.view.cancelled()
        await self._send({"type": "cancel"})

    async def stop(self) -> None:
        """Escape / Stop, as ``useHitl.stop`` does: reject every open approval, then cancel."""
        pending = list(self.view.pending)
        if pending:
            await self._send(
                {
                    "type": "hitl_response_batch",
                    "id": self._next_id("hitl"),
                    "decisions": [
                        {"requirement_id": p.requirement_id, "action": "reject", "choice": ""}
                        for p in pending
                    ],
                }
            )
        await self.cancel()

    async def answer(
        self,
        action: str,
        choice: str = "",
        requirement_ids: list[str] | None = None,
        mode: str = "",
    ) -> str:
        """Answer the approvals on screen (or ``requirement_ids``) in one batch."""
        ids = requirement_ids or [p.requirement_id for p in self.view.pending]
        req = self._next_id("hitl")
        decisions = [{"requirement_id": rid, "action": action, "choice": choice} for rid in ids]
        if mode:
            for decision in decisions:
                decision["set_permission_mode"] = mode
        if action == "confirm" or ids:
            self.view.phase = "starting"
        self.view.pending = [p for p in self.view.pending if p.requirement_id not in ids]
        await self._send({"type": "hitl_response_batch", "id": req, "decisions": decisions})
        return req

    async def command(self, text: str) -> WireFrame:
        """A slash command; its ``command_result``."""
        req = self._next_id("cmd")
        mark = len(self.frames)
        await self._send({"type": "command", "id": req, "text": text})
        return await self.wait_for(
            lambda f: f.id == req and f.type != "stream_end", since=mark, timeout=30
        )

    async def switch_model(self, name: str) -> WireFrame:
        req = self._next_id("model")
        mark = len(self.frames)
        await self._send({"type": "model_switch", "id": req, "model_name": name})
        return await self.wait_for(lambda f: f.id == req, since=mark, timeout=30)

    # ── Reading ──────────────────────────────────────────────────

    async def history(self) -> list[Any]:
        result = await self.rpc("get_chat_history", {"session_id": self.session_id})
        return list(result or [])

    async def processing(self) -> bool:
        return bool(await self.rpc("get_processing"))

    async def wait_for(
        self,
        predicate: Callable[[WireFrame], bool],
        since: int | None = None,
        timeout: float = 20.0,
    ) -> WireFrame:
        """The first frame from index ``since`` on that matches ``predicate``."""
        start = len(self.frames) if since is None else since
        deadline = time.monotonic() + timeout
        while True:
            for frame in self.frames[start:]:
                if predicate(frame):
                    return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                kinds = [f.type for f in self.frames[start:]][-30:]
                raise TimeoutError(f"{self.name}: no matching frame; saw {kinds}")
            self._arrived.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._arrived.wait(), min(remaining, 0.1))

    def mine(self, since: int = 0) -> list[WireFrame]:
        """The frames of the bound session from ``since`` on."""
        return [f for f in self.frames[since:] if f.session_id == self.session_id]

    async def quiet(self, seconds: float = 0.6, limit: float = 20.0) -> None:
        """Wait until the bound session has sent nothing for ``seconds``."""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            mine = [
                f
                for f in self.frames
                if f.session_id == self.session_id and f.type != "push_notification"
            ]
            last = mine[-1].received_at if mine else 0.0
            if time.monotonic() - last >= seconds:
                return
            await asyncio.sleep(0.05)
