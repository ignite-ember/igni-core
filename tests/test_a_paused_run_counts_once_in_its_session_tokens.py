"""A run's tokens are added to its session's totals once, however often it paused."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from agno.agent import Agent
from agno.db.sqlite import AsyncSqliteDb
from agno.metrics import RunMetrics
from agno.models.openai.like import OpenAILike
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession
from agno.team import Team
from agno.team import _session as team_session
from agno.tools import tool

from tests.session_factory import build_session

PROMPT_TOKENS = 100
"""Prompt tokens every reply reports."""
COMPLETION_TOKENS = 10
"""Completion tokens every reply reports."""


class ScriptedServer:
    """An OpenAI-compatible endpoint: ``calls`` tool calls, one per reply, then an answer."""

    def __init__(self, calls: int) -> None:
        self.requests = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server's name
                self.rfile.read(int(self.headers["Content-Length"]))
                n = server.requests
                server.requests += 1
                if n < calls:
                    message: dict[str, Any] = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call_{n}",
                                "type": "function",
                                "function": {"name": "ping", "arguments": "{}"},
                            }
                        ],
                    }
                    finish = "tool_calls"
                else:
                    message, finish = {"role": "assistant", "content": "done"}, "stop"
                payload = json.dumps(
                    {
                        "id": "c",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "m",
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                        "usage": {
                            "prompt_tokens": PROMPT_TOKENS,
                            "completion_tokens": COMPLETION_TOKENS,
                            "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
                        },
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


@tool(requires_confirmation=True)
def ping() -> str:
    """Answer pong."""
    return "pong"


@pytest.fixture(scope="module")
def composed(tmp_path_factory: pytest.TempPathFactory) -> None:
    """A real session's composition, which is where igni corrects Agno's count."""
    build_session(tmp_path_factory.mktemp("session"))


@pytest.fixture
def serve() -> Iterator[Any]:
    servers: list[ScriptedServer] = []

    def start(calls: int) -> ScriptedServer:
        servers.append(ScriptedServer(calls))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


async def _run_confirming_every_pause(url: str, db_file: Path, *, turns: int) -> tuple[Any, Any]:
    agent = Agent(
        model=OpenAILike(id="m", base_url=url, api_key="k"),
        tools=[ping],
        db=AsyncSqliteDb(db_file=str(db_file)),
        session_id="s",
        telemetry=False,
    )
    for _ in range(turns):
        run = await agent.arun("go")
        while run.is_paused:
            for requirement in run.requirements or []:
                if not requirement.is_resolved():
                    requirement.confirm()
            run = await agent.acontinue_run(
                run_response=run, requirements=run.requirements, session_id="s"
            )
    session = await agent.aget_session("s")
    return session, run


@pytest.mark.usefixtures("composed")
def test_a_run_that_paused_five_times_is_counted_once(serve, tmp_path):
    server = serve(calls=5)

    session, run = asyncio.run(_run_confirming_every_pause(server.url, tmp_path / "s.db", turns=1))

    totals = session.session_data["session_metrics"]
    assert run.metrics.input_tokens == 6 * PROMPT_TOKENS
    assert totals["input_tokens"] == run.metrics.input_tokens
    assert totals["output_tokens"] == run.metrics.output_tokens


@pytest.mark.usefixtures("composed")
def test_the_session_totals_are_the_sum_of_its_runs(serve, tmp_path):
    server = serve(calls=3)

    session, _ = asyncio.run(_run_confirming_every_pause(server.url, tmp_path / "s.db", turns=2))

    runs = session.runs
    assert len(runs) == 2
    totals = session.session_data["session_metrics"]
    assert totals["input_tokens"] == sum(r.metrics.input_tokens for r in runs)
    assert totals["total_tokens"] == sum(r.metrics.total_tokens for r in runs)


@pytest.mark.usefixtures("composed")
def test_a_run_left_paused_adds_nothing_yet(serve, tmp_path):
    server = serve(calls=1)

    async def pause_only() -> Any:
        agent = Agent(
            model=OpenAILike(id="m", base_url=server.url, api_key="k"),
            tools=[ping],
            db=AsyncSqliteDb(db_file=str(tmp_path / "s.db")),
            session_id="s",
            telemetry=False,
        )
        run = await agent.arun("go")
        assert run.is_paused
        return await agent.aget_session("s")

    session = asyncio.run(pause_only())

    totals = (session.session_data or {}).get("session_metrics") or {}
    assert totals.get("input_tokens", 0) == 0


@pytest.mark.usefixtures("composed")
@pytest.mark.parametrize(
    ("status", "counted"),
    [(RunStatus.paused, 0), (RunStatus.running, 0), (RunStatus.completed, 7)],
)
def test_a_team_run_is_counted_only_once_it_ends(status, counted):
    team = Team(members=[], telemetry=False)
    session = TeamSession(session_id="s", session_data={})
    run = TeamRunOutput(run_id="r", status=status, metrics=RunMetrics(input_tokens=7))

    team_session.update_session_metrics(team, session, run)

    totals = session.session_data.get("session_metrics") or {}
    assert totals.get("input_tokens", 0) == counted
