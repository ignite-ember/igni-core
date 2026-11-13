"""The LSP client against a real server process: launch, handshake, requests,
timeouts, the reader's survival of bad frames, and shutdown.

The server is a tiny Python script speaking Content-Length framed JSON-RPC on
stdio. Its behaviour is chosen by the ``FAKE_LSP_MODE`` environment variable
so each test gets exactly the server it needs.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from igni_core.core.lsp.client import LspClient, LspClientError
from igni_core.core.lsp.schemas import LspServerConfig

SERVER = r"""
import json, os, sys

mode = os.environ.get("FAKE_LSP_MODE", "normal")
log = os.environ.get("FAKE_LSP_LOG")
inp, out = sys.stdin.buffer, sys.stdout.buffer


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


def note(msg):
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(msg) + "\n")


if mode == "die":
    sys.exit(0)
if mode == "leaky":
    # A child that keeps the output pipe open after the server itself exits.
    import subprocess
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
if mode == "stubborn":
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    msg = read()
    if msg is None:
        break
    note(msg)
    method = msg.get("method")
    if method == "exit" and mode != "stubborn":
        break
    if "id" not in msg:
        continue
    if method == "initialize":
        if mode == "init-error":
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32002, "message": "nope"}})
            continue
        if mode == "init-hang":
            continue
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}})
    elif method == "shutdown":
        if mode == "stubborn":
            continue
        send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
    elif method == "echo":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": msg["params"]})
    elif method == "garbage-then-echo":
        for junk in (b"\xff\xfe", b"not json", b"[1, 2]", b'{"jsonrpc": "2.0"}'):
            out.write(b"Content-Length: %d\r\n\r\n" % len(junk) + junk)
        out.flush()
        send({"jsonrpc": "2.0", "id": msg["id"], "result": "still here"})
    elif method == "noise-then-echo":
        send({"jsonrpc": "2.0", "method": "window/logMessage", "params": {}})
        send({"jsonrpc": "2.0", "id": 900, "method": "workspace/configuration", "params": {}})
        send({"jsonrpc": "2.0", "id": 777, "result": "nobody asked"})
        send({"jsonrpc": "2.0", "id": msg["id"], "result": "after noise"})
    elif method == "fail":
        send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no such method"}})
    elif method == "swap":
        second = read()
        send({"jsonrpc": "2.0", "id": second["id"], "result": "second"})
        send({"jsonrpc": "2.0", "id": msg["id"], "result": "first"})
    elif method == "silent":
        pass
"""


@pytest.fixture
def server_path(tmp_path: Path) -> Path:
    path = tmp_path / "fake_lsp_server.py"
    path.write_text(SERVER)
    return path


def _client(server_path: Path, tmp_path: Path, mode: str = "normal", **config) -> LspClient:
    env = {"FAKE_LSP_MODE": mode, "FAKE_LSP_LOG": str(tmp_path / "log.jsonl")}
    cfg = LspServerConfig(
        name="fake",
        command=sys.executable,
        args=[str(server_path)],
        env=env,
        **config,
    )
    return LspClient(cfg, project_dir=tmp_path)


def _received(tmp_path: Path) -> list[dict]:
    log = tmp_path / "log.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


async def _received_at_least(tmp_path: Path, count: int) -> list[dict]:
    """The server logs after it reads, so give it a moment to catch up."""
    for _ in range(100):
        msgs = _received(tmp_path)
        if len(msgs) >= count:
            return msgs
        await asyncio.sleep(0.05)
    return _received(tmp_path)


async def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestStart:
    @pytest.mark.asyncio
    async def test_handshake_sends_initialize_then_initialized_with_the_root(
        self, server_path, tmp_path
    ):
        client = _client(server_path, tmp_path, initialization_options={"strict": True})
        await client.start()
        try:
            msgs = await _received_at_least(tmp_path, 2)
            assert [m["method"] for m in msgs] == ["initialize", "initialized"]
            init, initialized = msgs
            assert init["id"] == 1
            assert "id" not in initialized
            assert initialized["params"] == {}
            params = init["params"]
            assert params["processId"] == os.getpid()
            assert params["rootUri"] == f"file://{tmp_path.resolve()}"
            assert params["initializationOptions"] == {"strict": True}
            assert params["capabilities"] == {"workspace": {}, "textDocument": {}}
            assert "process_id" not in params
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    async def test_a_configured_root_uri_replaces_the_project_dir(self, server_path, tmp_path):
        client = _client(server_path, tmp_path, root_uri="file:///elsewhere")
        await client.start()
        try:
            assert (await _received_at_least(tmp_path, 1))[0]["params"][
                "rootUri"
            ] == "file:///elsewhere"
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    async def test_the_server_runs_in_the_project_dir_with_the_config_env(
        self, server_path, tmp_path
    ):
        cwd_script = tmp_path / "cwd_server.py"
        cwd_script.write_text(
            "import os, sys\n"
            "open(os.environ['WHERE'], 'w').write(os.getcwd() + '|' + os.environ['PATH'][:1]"
            " + '|' + os.environ['MINE'])\n"
        )
        cfg = LspServerConfig(
            name="cwd",
            command=sys.executable,
            args=[str(cwd_script)],
            env={"WHERE": str(tmp_path / "where.txt"), "MINE": "yes"},
        )
        client = LspClient(cfg, project_dir=tmp_path)
        with pytest.raises(LspClientError):
            await asyncio.wait_for(client.start(), timeout=10)
        cwd, path_head, mine = (tmp_path / "where.txt").read_text().split("|")
        assert Path(cwd).resolve() == tmp_path.resolve()
        assert path_head == os.environ["PATH"][:1]
        assert mine == "yes"

    @pytest.mark.asyncio
    async def test_a_missing_command_is_a_client_error_naming_it(self, tmp_path):
        client = LspClient(LspServerConfig(name="x", command="/no/such/language-server"), tmp_path)
        with pytest.raises(LspClientError, match="failed to launch '/no/such/language-server'"):
            await client.start()
        assert client._proc is None

    @pytest.mark.asyncio
    async def test_a_second_start_does_not_spawn_or_handshake_again(self, server_path, tmp_path):
        client = _client(server_path, tmp_path)
        await client.start()
        try:
            pid = client._proc.pid
            await client.start()
            assert client._proc.pid == pid
            assert [m["method"] for m in await _received_at_least(tmp_path, 2)] == [
                "initialize",
                "initialized",
            ]
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    async def test_a_server_error_on_initialize_leaves_no_process(self, server_path, tmp_path):
        client = _client(server_path, tmp_path, mode="init-error")
        with pytest.raises(LspClientError, match="LSP error -32002: nope"):
            await client.start()
        assert client._proc is None
        assert client._initialized is False
        assert client._pending == {}

    @pytest.mark.asyncio
    async def test_a_server_that_exits_during_initialize_fails_the_start_at_once(
        self, server_path, tmp_path
    ):
        client = _client(server_path, tmp_path, mode="die")
        with pytest.raises(LspClientError):
            await asyncio.wait_for(client.start(), timeout=10)
        assert client._proc is None

    @pytest.mark.asyncio
    async def test_a_cancelled_start_kills_the_process_it_spawned(self, server_path, tmp_path):
        client = _client(server_path, tmp_path, mode="init-hang")
        task = asyncio.create_task(client.start())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if client._proc is not None and client._pending:
                break
        pid = client._proc.pid
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client._proc is None
        await asyncio.sleep(0.1)
        assert not await _alive(pid)


class TestRequest:
    @pytest.mark.asyncio
    async def test_the_result_comes_back_verbatim_with_unicode_intact(self, server_path, tmp_path):
        client = _client(server_path, tmp_path)
        await client.start()
        try:
            params = {"text": "héllo — 日本語 ☃", "n": [1, 2, {"a": None}]}
            assert await client.request("echo", params) == params
            sent = [m for m in await _received_at_least(tmp_path, 3) if m["method"] == "echo"][0]
            assert sent["params"] == params
            assert sent["jsonrpc"] == "2.0"
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    async def test_ids_count_up_from_the_handshake(self, server_path, tmp_path):
        client = _client(server_path, tmp_path)
        await client.start()
        try:
            await client.request("echo", {})
            await client.request("echo", {})
            ids = [m["id"] for m in await _received_at_least(tmp_path, 4) if "id" in m]
            assert ids == [1, 2, 3]
            assert client._pending == {}
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    async def test_a_server_error_names_its_code_and_message(self, server_path, tmp_path):
        client = _client(server_path, tmp_path)
        await client.start()
        try:
            with pytest.raises(LspClientError) as err:
                await client.request("fail", {})
            assert str(err.value) == "LSP error -32601: no such method"
            assert client._pending == {}
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    async def test_a_silent_server_times_out_and_the_wait_is_forgotten(self, server_path, tmp_path):
        client = _client(server_path, tmp_path)
        await client.start()
        try:
            with pytest.raises(LspClientError) as err:
                await client.request("silent", {}, timeout=0.2)
            assert str(err.value) == "fake: silent timed out after 0.2s"
            assert client._pending == {}
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    async def test_concurrent_requests_get_their_own_answers_out_of_order(
        self, server_path, tmp_path
    ):
        client = _client(server_path, tmp_path)
        await client.start()
        try:
            first = asyncio.create_task(client.request("swap", {}))
            await asyncio.sleep(0.05)
            second = asyncio.create_task(client.request("echo", ["x"]))
            assert await first == "first"
            assert await second == "second"
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    async def test_params_that_are_not_an_object_array_or_null_are_refused_cleanly(
        self, server_path, tmp_path
    ):
        client = _client(server_path, tmp_path)
        await client.start()
        try:
            with pytest.raises(LspClientError) as err:
                await client.request("echo", "just a string")
            assert str(err.value) == "fake: echo params must be an object, an array or null"
            with pytest.raises(LspClientError, match="params must be an object"):
                await client.notify("echo", 5)
            assert client._pending == {}
            assert await client.request("echo", ["ok"]) == ["ok"]
            ids = [m["id"] for m in await _received_at_least(tmp_path, 3) if "id" in m]
            assert ids == [1, 2]
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    async def test_request_and_notify_refuse_when_not_running(self, tmp_path):
        client = LspClient(LspServerConfig(name="idle", command="x"), tmp_path)
        with pytest.raises(LspClientError, match="idle: not running"):
            await client.request("echo", {})
        with pytest.raises(LspClientError, match="idle: not running"):
            await client.notify("echo", {})
        assert client._pending == {}

    @pytest.mark.asyncio
    async def test_a_notification_carries_no_id_and_waits_for_nothing(self, server_path, tmp_path):
        client = _client(server_path, tmp_path)
        await client.start()
        try:
            await client.notify("workspace/didChangeConfiguration", {"settings": {}})
            await client.request("echo", [1])
            sent = [
                m
                for m in await _received_at_least(tmp_path, 4)
                if m["method"] == "workspace/didChangeConfiguration"
            ]
            assert sent == [
                {
                    "jsonrpc": "2.0",
                    "method": "workspace/didChangeConfiguration",
                    "params": {"settings": {}},
                }
            ]
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    async def test_a_dead_server_is_reported_as_gone_or_not_running(self, server_path, tmp_path):
        client = _client(server_path, tmp_path)
        await client.start()
        client._proc.kill()
        await client._proc.wait()
        with pytest.raises(LspClientError, match="fake: server gone"):
            for _ in range(50):
                await client.notify("echo", {"pad": "x" * 100000})
        await client.shutdown()


class TestReader:
    @pytest.mark.asyncio
    async def test_frames_that_cannot_be_used_are_dropped_and_later_answers_still_arrive(
        self, server_path, tmp_path
    ):
        client = _client(server_path, tmp_path)
        await client.start()
        try:
            assert await client.request("garbage-then-echo", {}, timeout=3) == "still here"
            assert await client.request("echo", ["next"], timeout=3) == ["next"]
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    async def test_notifications_server_requests_and_stray_answers_are_ignored(
        self, server_path, tmp_path
    ):
        client = _client(server_path, tmp_path)
        await client.start()
        try:
            assert await client.request("noise-then-echo", {}, timeout=3) == "after noise"
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    async def test_the_end_of_the_stream_ends_the_reader_and_fails_every_waiter(self, tmp_path):
        client = LspClient(LspServerConfig(name="x", command="x"), tmp_path)
        reader = asyncio.StreamReader()
        reader.feed_eof()

        class _Proc:
            stdout = reader

        client._proc = _Proc()
        fut = asyncio.get_running_loop().create_future()
        client._pending[3] = fut
        await asyncio.wait_for(client._reader_loop(), timeout=2)
        with pytest.raises(LspClientError, match="LSP server closed its output"):
            await fut
        assert client._pending == {}

    @pytest.mark.asyncio
    async def test_a_truncated_body_fails_every_waiter(self, tmp_path):
        client = LspClient(LspServerConfig(name="x", command="x"), tmp_path)
        reader = asyncio.StreamReader()
        reader.feed_data(b"Content-Length: 50\r\n\r\n{")
        reader.feed_eof()

        class _Proc:
            stdout = reader

        client._proc = _Proc()
        fut = asyncio.get_running_loop().create_future()
        client._pending[3] = fut
        await asyncio.wait_for(client._reader_loop(), timeout=2)
        with pytest.raises(LspClientError, match="reader crashed"):
            await fut
        assert client._pending == {}


class TestShutdown:
    @pytest.mark.asyncio
    async def test_a_polite_server_gets_shutdown_then_exit_and_leaves(self, server_path, tmp_path):
        client = _client(server_path, tmp_path)
        await client.start()
        pid = client._proc.pid
        await client.shutdown()
        methods = [m["method"] for m in _received(tmp_path)]
        assert methods == ["initialize", "initialized", "shutdown", "exit"]
        shutdown = [m for m in _received(tmp_path) if m["method"] == "shutdown"][0]
        assert "params" in shutdown and shutdown["params"] is None
        assert client._proc is None
        assert client._reader_task is None
        assert client._initialized is False
        assert not await _alive(pid)

    @pytest.mark.asyncio
    async def test_a_server_that_ignores_shutdown_is_terminated(self, server_path, tmp_path):
        client = _client(server_path, tmp_path, mode="stubborn")
        await client.start()
        pid = client._proc.pid
        await asyncio.wait_for(client.shutdown(), timeout=15)
        assert client._proc is None
        await asyncio.sleep(0.2)
        assert not await _alive(pid)

    @pytest.mark.asyncio
    async def test_shutdown_completes_when_the_output_pipe_outlives_the_server(
        self, server_path, tmp_path
    ):
        client = _client(server_path, tmp_path, mode="leaky")
        await client.start()
        await asyncio.wait_for(client.shutdown(), timeout=10)
        assert client._proc is None
        assert client._reader_task is None
        assert client._initialized is False

    @pytest.mark.asyncio
    async def test_shutting_down_a_client_never_started_does_nothing(self, tmp_path):
        client = LspClient(LspServerConfig(name="x", command="x"), tmp_path)
        await client.shutdown()
        await client.shutdown()
        assert client._proc is None

    @pytest.mark.asyncio
    async def test_shutdown_fails_the_requests_still_waiting(self, server_path, tmp_path):
        client = _client(server_path, tmp_path)
        await client.start()
        waiting = asyncio.create_task(client.request("silent", {}, timeout=30))
        await asyncio.sleep(0.1)
        await client.shutdown()
        with pytest.raises(LspClientError, match="LSP server (shut down|closed its output)"):
            await waiting

    @pytest.mark.asyncio
    async def test_a_client_can_start_again_after_shutdown(self, server_path, tmp_path):
        client = _client(server_path, tmp_path)
        await client.start()
        await client.shutdown()
        await client.start()
        try:
            assert await client.request("echo", ["again"]) == ["again"]
        finally:
            await client.shutdown()
