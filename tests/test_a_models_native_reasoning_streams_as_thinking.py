"""A reasoning model's native reasoning reaches the app as thinking while it streams."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from agno.agent import Agent
from agno.run import agent as agent_events
from agno.run import team as team_events

from igni_core.backend.inline_think_splitter import InlineThinkSplitter
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.models import ModelRegistry
from igni_core.core.config.settings import Settings
from igni_core.protocol import messages as msg
from igni_core.protocol.serializer import serialize_event

THINKING = ["The user asks ", "whether 1001 is prime. ", "1001 = 7 * 11 * 13."]
ANSWER = "Composite."


class StreamingServer:
    """An OpenAI-compatible endpoint streaming the given deltas, then a stop."""

    def __init__(self, deltas: list[dict[str, str]]) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server's name
                self.rfile.read(int(self.headers["Content-Length"]))
                chunks = [{"delta": {"role": "assistant", **d}} for d in deltas]
                chunks.append({"delta": {}, "finish_reason": "stop"})
                body = "".join(
                    "data: "
                    + json.dumps(
                        {
                            "id": "c",
                            "object": "chat.completion.chunk",
                            "created": 0,
                            "model": "m",
                            "choices": [{"index": 0, "finish_reason": None, **c}],
                        }
                    )
                    + "\n\n"
                    for c in chunks
                )
                payload = (body + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def serve() -> Iterator[Any]:
    servers: list[StreamingServer] = []

    def start(deltas: list[dict[str, str]]) -> StreamingServer:
        servers.append(StreamingServer(deltas))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def _streamed(url: str) -> list[msg.ContentDelta]:
    """The content deltas the backend sends for one streamed run, in order."""
    settings = Settings()
    settings.models.registry = {
        "m": ModelRegistryEntry(provider="openai_like", model_id="m", url=url, api_key="k")
    }
    agent = Agent(model=ModelRegistry(settings).get_model("m"), retries=0, telemetry=False)
    splitter = InlineThinkSplitter()

    async def go() -> list[msg.ContentDelta]:
        out: list[msg.ContentDelta] = []
        async for event in agent.arun("Is 1001 prime?", stream=True):
            message = serialize_event(event)
            if message is None:
                continue
            out.extend(
                m
                for m in splitter.split(message, getattr(event, "run_id", None))
                if isinstance(m, msg.ContentDelta)
            )
        return out

    return asyncio.run(go())


def test_native_reasoning_deltas_arrive_as_thinking_before_the_answer(serve):
    server = serve([{"reasoning_content": part} for part in THINKING] + [{"content": ANSWER}])

    deltas = _streamed(server.url)

    assert [(d.text, d.is_thinking) for d in deltas] == [
        *((part, True) for part in THINKING),
        (ANSWER, False),
    ]


def test_inline_think_tags_still_arrive_as_thinking(serve):
    """MiniMax-M2.7's shape, which showed its thinking all along."""
    server = serve([{"content": "<think>pondering</think>"}, {"content": ANSWER}])

    deltas = _streamed(server.url)

    assert ("pondering", True) in [(d.text, d.is_thinking) for d in deltas]
    assert deltas[-1].text == ANSWER
    assert not deltas[-1].is_thinking


@pytest.mark.parametrize("events", [agent_events, team_events], ids=["agent", "team"])
def test_a_content_event_carrying_only_reasoning_is_a_thinking_delta(events):
    message = serialize_event(events.RunContentEvent(reasoning_content="<think>weighing</think>"))
    assert message == msg.ContentDelta(text="weighing", is_thinking=True)


def test_a_content_event_with_neither_says_nothing():
    assert serialize_event(agent_events.RunContentEvent(content="", reasoning_content="")) is None
