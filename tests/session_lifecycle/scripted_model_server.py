"""An OpenAI-compatible model on 127.0.0.1 that answers from a script and keeps every request."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests.session_lifecycle.model_script import ModelScript
from tests.session_lifecycle.schemas.model_turn import ModelTurn
from tests.session_lifecycle.schemas.recorded_message import RecordedMessage
from tests.session_lifecycle.schemas.recorded_request import RecordedRequest


class ScriptedModelServer:
    """The scripted model: start it, point igni at :attr:`url`, read :attr:`requests`."""

    # How long a held stream waits for its gate before giving up.
    HOLD_LIMIT_SECONDS = 60.0

    def __init__(self, script: ModelScript | None = None, model_id: str = "scripted/model") -> None:
        self.script = script or ModelScript()
        self.model_id = model_id
        self.requests: list[RecordedRequest] = []
        self._lock = threading.Condition()
        self._gates: dict[str, threading.Event] = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedHandler)
        self._server.daemon_threads = True
        self._server.owner = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> ScriptedModelServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        for gate in list(self._gates.values()):
            gate.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> ScriptedModelServer:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # ── Gates ────────────────────────────────────────────────────

    def gate(self, name: str) -> threading.Event:
        """The gate ``name``; a turn holding on it waits until it is released."""
        with self._lock:
            return self._gates.setdefault(name, threading.Event())

    def release(self, name: str) -> None:
        """Let every stream holding on ``name`` go on."""
        self.gate(name).set()

    # ── Reading back ─────────────────────────────────────────────

    def snapshot(self) -> list[RecordedRequest]:
        """Every request so far, as copies."""
        with self._lock:
            return [r.model_copy(deep=True) for r in self.requests]

    def agent_requests(self) -> list[RecordedRequest]:
        """The requests that carried tools: the agent's turns, not the naming call."""
        return [r for r in self.snapshot() if r.tool_names]

    def wait_for(
        self, predicate: Callable[[list[RecordedRequest]], bool], timeout: float = 20.0
    ) -> list[RecordedRequest]:
        """Block until ``predicate`` holds over the requests; raise on timeout."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while not predicate(self.requests):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"the scripted model never saw the expected requests; it saw "
                        f"{len(self.requests)}: {[r.rule for r in self.requests]}"
                    )
                self._lock.wait(min(remaining, 0.1))
            return [r.model_copy(deep=True) for r in self.requests]

    def streaming(self) -> list[RecordedRequest]:
        """The requests whose stream is still open."""
        return [r for r in self.snapshot() if r.outcome == "streaming"]

    # ── Called by the handler ────────────────────────────────────

    def record(self, body: dict[str, Any]) -> tuple[RecordedRequest, ModelTurn]:
        messages = [_message(m) for m in body.get("messages") or []]
        tools = [str((t.get("function") or {}).get("name", "")) for t in body.get("tools") or []]
        rule, step, turn = self.script.choose(messages, has_tools=bool(tools))
        with self._lock:
            request = RecordedRequest(
                index=len(self.requests),
                received_at=time.monotonic(),
                model=str(body.get("model", "")),
                stream=bool(body.get("stream")),
                messages=messages,
                tool_names=tools,
                body=json.dumps(body),
                rule=rule,
                step=step,
            )
            self.requests.append(request)
            self._lock.notify_all()
        return request, turn

    def finish(self, request: RecordedRequest, outcome: str) -> None:
        with self._lock:
            live = self.requests[request.index]
            live.outcome = outcome  # type: ignore[assignment]
            live.finished_at = time.monotonic()
            self._lock.notify_all()


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _message(raw: dict[str, Any]) -> RecordedMessage:
    calls = raw.get("tool_calls") or []
    return RecordedMessage(
        role=str(raw.get("role", "")),
        text=_text(raw.get("content")),
        tool_call_names=[str((c.get("function") or {}).get("name", "")) for c in calls],
        tool_call_ids=[str(c.get("id", "")) for c in calls],
        tool_call_id=raw.get("tool_call_id"),
    )


class _StreamClosedError(Exception):
    """igni closed the connection while the turn was streaming."""


class _ScriptedHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 — the stdlib's name
        owner: ScriptedModelServer = self.server.owner  # type: ignore[attr-defined]
        body = json.dumps({"object": "list", "data": [{"id": owner.model_id, "object": "model"}]})
        self._send_json(200, body.encode())

    def do_POST(self) -> None:  # noqa: N802 — the stdlib's name
        owner: ScriptedModelServer = self.server.owner  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.endswith("/chat/completions"):
            self._send_json(404, b'{"error": "not found"}')
            return
        request, turn = owner.record(body)
        if turn.delay_before:
            time.sleep(turn.delay_before)
        if turn.error_status is not None:
            message = {"error": {"message": "scripted failure", "type": "server_error"}}
            self._send_json(turn.error_status, json.dumps(message).encode())
            owner.finish(request, "errored")
            return
        try:
            if request.stream:
                self._stream(owner, turn, request.model or owner.model_id, request.index)
            else:
                self._send_json(200, json.dumps(_completion(turn, owner.model_id)).encode())
        except (_StreamClosedError, BrokenPipeError, ConnectionResetError):
            owner.finish(request, "aborted")
            self.close_connection = True
            return
        owner.finish(request, "completed")

    def _stream(
        self, owner: ScriptedModelServer, turn: ModelTurn, model: str, request_index: int
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        chunks = _chunks(turn, request_index)
        # Held after the first chunk that says something, not the bare role.
        hold_at = min(1, len(chunks) - 1)
        for i, delta in enumerate(chunks):
            self._event(_chunk(model, delta, None))
            if i == hold_at and turn.hold:
                self._hold(owner, turn.hold)
            elif turn.chunk_delay:
                self._sleep_alive(turn.chunk_delay)
        finish = "tool_calls" if turn.tool_calls else "stop"
        self._event(_chunk(model, {}, finish))
        usage = {
            "prompt_tokens": turn.prompt_tokens,
            "completion_tokens": turn.completion_tokens,
            "total_tokens": turn.prompt_tokens + turn.completion_tokens,
        }
        self._event({**_chunk(model, None, None), "choices": [], "usage": usage})
        self._write(b"data: [DONE]\n\n")

    def _hold(self, owner: ScriptedModelServer, name: str) -> None:
        gate = owner.gate(name)
        deadline = time.monotonic() + ScriptedModelServer.HOLD_LIMIT_SECONDS
        while not gate.wait(0.05):
            if time.monotonic() > deadline:
                raise _StreamClosedError(f"gate {name!r} was never released")
            self._write(b": held\n\n")

    def _sleep_alive(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            self._write(b": waiting\n\n")

    def _event(self, payload: dict[str, Any]) -> None:
        self._write(f"data: {json.dumps(payload)}\n\n".encode())

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise _StreamClosedError(str(exc)) from exc

    def _send_json(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the stdlib's name
        return


def _chunks(turn: ModelTurn, request_index: int) -> list[dict[str, Any]]:
    """The deltas of ``turn``: the role, reasoning, text, then each tool call."""
    size = max(1, turn.chunk_size)
    deltas: list[dict[str, Any]] = [{"role": "assistant", "content": ""}]
    for i in range(0, len(turn.reasoning), size):
        deltas.append({"reasoning_content": turn.reasoning[i : i + size]})
    for i in range(0, len(turn.text), size):
        deltas.append({"content": turn.text[i : i + size]})
    for index, call in enumerate(turn.tool_calls):
        call_id = call.call_id or f"call_{request_index}_{index}"
        arguments = json.dumps(call.arguments)
        deltas.append(
            {
                "tool_calls": [
                    {
                        "index": index,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": ""},
                    }
                ]
            }
        )
        for i in range(0, len(arguments), size):
            deltas.append(
                {
                    "tool_calls": [
                        {"index": index, "function": {"arguments": arguments[i : i + size]}}
                    ]
                }
            )
    return deltas


def _chunk(model: str, delta: dict[str, Any] | None, finish: str | None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-scripted",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _completion(turn: ModelTurn, model: str) -> dict[str, Any]:
    calls = [
        {
            "id": call.call_id or f"call_x_{i}",
            "type": "function",
            "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
        }
        for i, call in enumerate(turn.tool_calls)
    ]
    message: dict[str, Any] = {"role": "assistant", "content": turn.text or None}
    if calls:
        message["tool_calls"] = calls
    return {
        "id": "chatcmpl-scripted",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}
        ],
        "usage": {
            "prompt_tokens": turn.prompt_tokens,
            "completion_tokens": turn.completion_tokens,
            "total_tokens": turn.prompt_tokens + turn.completion_tokens,
        },
    }
