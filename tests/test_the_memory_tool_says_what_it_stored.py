"""``update_user_memory`` stores every memory it is given and replies with what it stored."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from agno.db.sqlite import AsyncSqliteDb, SqliteDb
from agno.models.openai.like import OpenAILike

from igni_core.core.config.settings import Settings
from igni_core.core.learn import TrainedLearningMachine

FACTS = [
    "Principal Software Engineer based in Amsterdam",
    "Solution Architect at S-PRO since 2025",
    "Works on GenAI systems and multi-agent orchestration",
    "Keeps his CV in the project's attachments folder",
]
CLAIM = "Memories saved — four entries covering identity, career, focus and CV."


class ExtractionServer:
    """An OpenAI-compatible endpoint that replays one scripted reply per request."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.requests = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server's name
                self.rfile.read(int(self.headers["Content-Length"]))
                message = replies[min(server.requests, len(replies) - 1)]
                server.requests += 1
                finish = "tool_calls" if message.get("tool_calls") else "stop"
                payload = json.dumps(
                    {
                        "id": "c",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "m",
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
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


def _calls(name: str, arguments: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, args in enumerate(arguments)
        ],
    }


@pytest.fixture
def serve() -> Iterator[Any]:
    servers: list[ExtractionServer] = []

    def start(replies: list[dict[str, Any]]) -> ExtractionServer:
        servers.append(ExtractionServer(replies))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def _store(url: str, db_file: Path, *, sync: bool = False) -> Any:
    db = SqliteDb(db_file=str(db_file)) if sync else AsyncSqliteDb(db_file=str(db_file))
    machine = TrainedLearningMachine(
        db=db,
        model=OpenAILike(id="m", base_url=url, api_key="k"),
        user_memory=Settings().learning.to_user_memory_input(),
    )
    return machine.user_memory_store


async def _update(store: Any, task: str) -> str:
    (tool,) = await store.aget_agent_tools(user_id="u", agent_id="igni")
    return await tool(task)


async def _stored(store: Any) -> list[str]:
    record = await store.aget(user_id="u")
    return [m["content"] for m in record.memories] if record else []


def test_four_memories_saved_in_one_reply_are_all_stored(serve, tmp_path):
    server = serve(
        [
            _calls("add_memory", [{"memory": f} for f in FACTS]),
            {"role": "assistant", "content": CLAIM},
        ]
    )
    store = _store(server.url, tmp_path / "state.db")

    asyncio.run(_update(store, "Remember who Dmytro is."))

    assert sorted(asyncio.run(_stored(store))) == sorted(FACTS)


def test_the_reply_lists_what_was_stored_not_what_the_extraction_model_claimed(serve, tmp_path):
    server = serve(
        [
            _calls("add_memory", [{"memory": f} for f in FACTS]),
            {"role": "assistant", "content": CLAIM},
        ]
    )
    store = _store(server.url, tmp_path / "state.db")

    reply = asyncio.run(_update(store, "Remember who Dmytro is."))

    stored = asyncio.run(_stored(store))
    assert reply.splitlines() == [f"Memory saved: {content}" for content in stored]
    assert CLAIM not in reply


def test_an_update_and_a_delete_are_reported_with_the_stored_text(serve, tmp_path):
    setup = serve(
        [
            _calls("add_memory", [{"memory": f} for f in FACTS[:2]]),
            {"role": "assistant", "content": "ok"},
        ]
    )
    store = _store(setup.url, tmp_path / "state.db")
    asyncio.run(_update(store, "Remember who Dmytro is."))
    record = asyncio.run(store.aget(user_id="u"))
    first, second = (m["id"] for m in record.memories)

    edit = serve(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "u1",
                        "type": "function",
                        "function": {
                            "name": "update_memory",
                            "arguments": json.dumps(
                                {"memory_id": first, "memory": "Based in Utrecht"}
                            ),
                        },
                    },
                    {
                        "id": "d1",
                        "type": "function",
                        "function": {
                            "name": "delete_memory",
                            "arguments": json.dumps({"memory_id": second}),
                        },
                    },
                ],
            },
            {"role": "assistant", "content": "Updated his location and removed the job."},
        ]
    )
    store = _store(edit.url, tmp_path / "state.db")

    reply = asyncio.run(_update(store, "He moved to Utrecht and left S-PRO."))

    assert reply.splitlines() == [
        "Memory updated: Based in Utrecht",
        f"Memory deleted: {FACTS[1]}",
    ]


def test_an_extraction_that_changes_nothing_says_so(serve, tmp_path):
    server = serve([{"role": "assistant", "content": "Saved everything you asked."}])
    store = _store(server.url, tmp_path / "state.db")

    reply = asyncio.run(_update(store, "Remember nothing new."))

    assert reply == "No updates needed"
    assert asyncio.run(_stored(store)) == []


def test_the_sync_tool_replies_with_what_was_stored(serve, tmp_path):
    server = serve(
        [
            _calls("add_memory", [{"memory": f} for f in FACTS]),
            {"role": "assistant", "content": CLAIM},
        ]
    )
    store = _store(server.url, tmp_path / "state.db", sync=True)
    (tool,) = store.get_agent_tools(user_id="u", agent_id="igni")

    reply = tool("Remember who Dmytro is.")

    stored = [m["content"] for m in store.get(user_id="u").memories]
    assert sorted(stored) == sorted(FACTS)
    assert reply.splitlines() == [f"Memory saved: {content}" for content in stored]
