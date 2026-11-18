"""The LSP agent tool against a real server process, end to end.

The tool used to speak only half of JSON-RPC: every method went out
as a *request*, so ``textDocument/didOpen`` — a notification — came
back as ``-32601 Unknown request``, the document was never opened,
and the diagnostics that depend on an open document came back empty.

The server here behaves the way a real language server behaves in
that respect: a notification method sent as a request is refused
with ``-32601``, a notification sent as a notification opens the
document, and a diagnostics request answers with findings for the
documents the server has been told about and with nothing for the
others. That is the difference the fix makes, and these tests walk
the whole way: the tool, the manager, the client, the wire.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from igni_core.core.lsp.manager import LspServerManager
from igni_core.core.lsp.schemas import LspServerConfig
from igni_core.core.tools.lsp import LspTools

SERVER = r"""
import json, os, sys

inp, out = sys.stdin.buffer, sys.stdout.buffer
open_docs = {}
log_path = os.environ.get("FAKE_LSP_LOG")


def send(obj):
    body = json.dumps(obj).encode()
    out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    out.flush()


def read():
    n = None
    while True:
        line = inp.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        k, _, v = line.partition(b":")
        if k.lower() == b"content-length":
            n = int(v)
    return json.loads(inp.read(n))


while True:
    msg = read()
    if msg is None:
        break
    if log_path:
        with open(log_path, "a") as fh:
            fh.write(json.dumps(msg) + "\n")
    method = msg.get("method")
    if method == "exit":
        break
    if "id" not in msg:
        # A notification — sent without an id, answered by nothing.
        if method == "textDocument/didOpen":
            doc = msg["params"]["textDocument"]
            open_docs[doc["uri"]] = doc.get("text", "")
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
    elif method == "textDocument/didOpen":
        # What a real server answers when a notification method
        # arrives as a request.
        send({"jsonrpc": "2.0", "id": msg["id"], "error": {
            "code": -32601, "message": "Unknown request: textDocument/didOpen"}})
    elif method == "textDocument/diagnostic":
        uri = msg["params"]["textDocument"]["uri"]
        items = []
        if uri in open_docs:
            items = [{
                "range": {"start": {"line": 0, "character": 7}, "end": {"line": 0, "character": 9}},
                "message": "`os` imported but unused",
                "severity": 2,
                "source": "ruff",
            }]
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"items": items, "kind": "full"}})
"""

URI = "file:///tmp/lsp-notification/doc.py"
TEXT = "import os\n"


@pytest.fixture
def server_path(tmp_path: Path) -> Path:
    path = tmp_path / "fake_lsp_server.py"
    path.write_text(SERVER)
    return path


@pytest.fixture
async def manager(server_path: Path, tmp_path: Path):
    config = LspServerConfig(
        name="fake",
        command=sys.executable,
        args=[str(server_path)],
        env={"FAKE_LSP_LOG": str(tmp_path / "log.jsonl")},
    )
    mgr = LspServerManager({"fake": config}, tmp_path)
    yield mgr
    await mgr.shutdown_all()


def _tool(manager: LspServerManager) -> LspTools:
    return LspTools(manager)


def _received(tmp_path: Path) -> list[dict]:
    log = tmp_path / "log.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


async def _received_method(tmp_path: Path, method: str) -> dict:
    """The server logs a message when it reads it, and a notification
    gets no reply to wait for — so the log can lag the call. Wait for
    the message the test is about."""
    for _ in range(100):
        for msg in _received(tmp_path):
            if msg.get("method") == method:
                return msg
        await asyncio.sleep(0.05)
    raise AssertionError(f"the server never received {method}")


class TestTheDocumentOpens:
    @pytest.mark.asyncio
    async def test_a_didOpen_notification_is_answered_by_the_diagnostics_that_follow(
        self, manager, tmp_path
    ):
        tool = _tool(manager)

        opened = await tool.lsp_query(
            "fake",
            "textDocument/didOpen",
            json.dumps(
                {
                    "textDocument": {
                        "uri": URI,
                        "languageId": "python",
                        "version": 1,
                        "text": TEXT,
                    }
                }
            ),
        )
        assert json.loads(opened) == {"notification": "textDocument/didOpen", "sent": True}

        diagnostics = await tool.lsp_query(
            "fake",
            "textDocument/diagnostic",
            json.dumps({"textDocument": {"uri": URI}}),
        )
        parsed = json.loads(diagnostics)
        assert parsed["kind"] == "full"
        assert len(parsed["items"]) == 1
        assert parsed["items"][0]["message"] == "`os` imported but unused"

    @pytest.mark.asyncio
    async def test_the_didOpen_goes_out_without_an_id_and_the_diagnostics_with_one(
        self, manager, tmp_path
    ):
        """The wire, not the intent: a notification carries no id."""
        tool = _tool(manager)
        await tool.lsp_query(
            "fake",
            "textDocument/didOpen",
            json.dumps(
                {"textDocument": {"uri": URI, "languageId": "python", "version": 1, "text": TEXT}}
            ),
        )
        await tool.lsp_query(
            "fake",
            "textDocument/diagnostic",
            json.dumps({"textDocument": {"uri": URI}}),
        )
        did_open = await _received_method(tmp_path, "textDocument/didOpen")
        diagnostic = await _received_method(tmp_path, "textDocument/diagnostic")
        assert "id" not in did_open
        assert "id" in diagnostic

    @pytest.mark.asyncio
    async def test_the_didOpen_carries_the_text_the_file_holds(self, manager, tmp_path):
        tool = _tool(manager)
        await tool.lsp_query(
            "fake",
            "textDocument/didOpen",
            json.dumps(
                {"textDocument": {"uri": URI, "languageId": "python", "version": 1, "text": TEXT}}
            ),
        )
        did_open = await _received_method(tmp_path, "textDocument/didOpen")
        assert did_open["params"]["textDocument"]["text"] == TEXT
        assert did_open["params"]["textDocument"]["uri"] == URI


class TestTheRequestPathIsUnchanged:
    @pytest.mark.asyncio
    async def test_a_notification_method_sent_as_a_request_is_still_refused(self, manager):
        """What the tool used to do, and what a server still answers:
        ``-32601``. The refusal is the server's, and the fix does not
        paper over it — it stops asking the wrong way."""
        with pytest.raises(Exception) as exc_info:
            await manager.query(
                "fake",
                "textDocument/didOpen",
                {"textDocument": {"uri": URI, "languageId": "python", "version": 1, "text": TEXT}},
            )
        assert "-32601" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_diagnostics_for_a_document_that_was_never_opened_are_empty(self, manager):
        tool = _tool(manager)
        diagnostics = await tool.lsp_query(
            "fake",
            "textDocument/diagnostic",
            json.dumps({"textDocument": {"uri": "file:///tmp/never-opened.py"}}),
        )
        parsed = json.loads(diagnostics)
        assert parsed["items"] == []
