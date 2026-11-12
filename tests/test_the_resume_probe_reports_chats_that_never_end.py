"""``scripts/measure_resume.py``: a chat that never ends is reported as "timed out".

``Window.chats`` sends one task to several sessions and reports each
session's last event. A session that is silent past the deadline is
``"timed out"`` — the run goes on and the report is still written.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "measure_resume.py"


def _load():
    spec = importlib.util.spec_from_file_location("measure_resume_chats", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Socket:
    """A WebSocket that sends nothing back, or only what it is given."""

    def __init__(self, replies=()):
        self.sent = []
        self._replies = [json.dumps(r) for r in replies]

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        if self._replies:
            return self._replies.pop(0)
        await asyncio.sleep(3600)


async def test_a_silent_socket_gives_timed_out_for_every_session():
    mod = _load()
    window = mod.Window(_Socket())
    got = await window.chats("do it", ["a", "b"], timeout=0.2)
    assert got == {"a": "timed out", "b": "timed out"}


async def test_the_session_that_ended_keeps_its_event_and_the_other_times_out():
    mod = _load()
    socket = _Socket(
        [
            {"type": "text", "session_id": "a"},
            {"type": "stream_end", "session_id": "a"},
            {"type": "run_error", "session_id": "other"},
        ]
    )
    got = await mod.Window(socket).chats("do it", ["a", "b"], timeout=0.2)
    assert got == {"a": "stream_end", "b": "timed out"}
    assert [m["session_id"] for m in socket.sent] == ["a", "b"]
    assert all(m["text"] == "do it" for m in socket.sent)
