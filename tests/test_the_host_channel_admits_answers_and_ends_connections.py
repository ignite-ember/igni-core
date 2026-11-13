"""The loopback socket an extension's process reaches the host model on.

A real :class:`HostChannel` over the rig's backend, spoken to over a real
WebSocket by a plain client (no SDK): who is admitted, what each frame is
answered with, how the secrets are replaced, revoked and forgotten, and
what happens to a connection that is cut off, too big or too slow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

from igni_core.extensions.host import channel as channel_module
from igni_core.extensions.host.channel import (
    ENV_HOST_SECRET,
    ENV_HOST_URL,
    MAX_FRAME,
    PATH,
    HostChannel,
    _SocketSink,
)
from igni_core.extensions.host.processes import PROCESS_GROUP_HEADER, ExtensionProcesses
from igni_core.extensions.host.wire import SignalMessage
from tests.host_model_helpers import HostRig

EXT = "acme.reviewer"


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    rig.install(EXT, host=["sessions", "chat.read"])
    try:
        yield rig
    finally:
        await rig.stop()


def channel_of(rig: HostRig) -> HostChannel:
    assert rig.channel is not None
    return rig.channel


async def granted(rig: HostRig, secret: str = "") -> dict[str, str]:
    return await channel_of(rig).grant(EXT, secret or secrets.token_urlsafe(16))


def bearer(env: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {env[ENV_HOST_SECRET]}"}


async def status_of(
    url: str,
    *,
    path: str | None = None,
    host: str | None = None,
    headers: dict[str, str] | None = None,
) -> int:
    """The HTTP status the listener answers a handshake with, over a raw socket."""
    _, _, hostport = url.partition("://")
    hostname, _, port = hostport.partition("/")[0].partition(":")
    sent = {
        "Host": host if host is not None else f"{hostname}:{port}",
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version": "13",
        **(headers or {}),
    }
    reader, writer = await asyncio.open_connection(hostname, int(port))
    try:
        lines = [f"GET {path if path is not None else PATH} HTTP/1.1"]
        lines += [f"{name}: {value}" for name, value in sent.items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        first = await asyncio.wait_for(reader.readline(), 5)
        return int(first.split()[1])
    finally:
        writer.close()


async def until(done: Callable[[], object]) -> None:
    """Wait, for at most five seconds, for something the channel does after the reply."""
    for _ in range(250):
        if done():
            return
        await asyncio.sleep(0.02)


async def open_socket(env: dict[str, str]) -> ClientConnection:
    socket = await connect(env[ENV_HOST_URL], additional_headers=bearer(env))
    hello = json.loads(await socket.recv())
    assert hello["op"] == "hello"
    return socket


async def exchange(socket: ClientConnection, frame: Any) -> dict[str, Any]:
    await socket.send(frame if isinstance(frame, str) else json.dumps(frame))
    return json.loads(await asyncio.wait_for(socket.recv(), 5))


# ── Starting, granting and stopping ───────────────────────────────


async def test_the_url_is_the_loopback_address_on_an_os_port_and_needs_a_start(
    rig: HostRig,
) -> None:
    channel = HostChannel(rig.model, hooks=rig.hooks, processes=ExtensionProcesses())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="await start"):
        _ = channel.url

    await channel.start()
    try:
        assert channel.url.startswith("ws://127.0.0.1:")
        assert channel.url.endswith(PATH)
        assert int(channel.url.split(":")[2].split("/")[0]) > 0
    finally:
        await channel.stop()


async def test_starting_twice_or_at_once_listens_once(
    rig: HostRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    listened: list[object] = []

    async def counted(*args: Any, **kwargs: Any) -> Any:
        listened.append(args)
        return await serve(*args, **kwargs)

    monkeypatch.setattr(channel_module, "serve", counted)
    channel = channel_of(rig)

    await asyncio.gather(channel.start(), channel.start(), channel.start())
    first = channel.url
    await channel.start()

    assert len(listened) == 1
    assert channel.url == first


async def test_a_grant_starts_the_channel_and_returns_the_process_environment(
    rig: HostRig,
) -> None:
    env = await granted(rig, "s3cret")

    assert env == {ENV_HOST_URL: channel_of(rig).url, ENV_HOST_SECRET: "s3cret"}


async def test_a_second_grant_replaces_the_first_secret(rig: HostRig) -> None:
    old = await granted(rig, "first-secret")
    new = await granted(rig, "second-secret")

    with pytest.raises(InvalidStatus) as refused:
        await connect(old[ENV_HOST_URL], additional_headers=bearer(old))
    assert refused.value.response.status_code == 401
    socket = await open_socket(new)
    await socket.close()


async def test_stop_forgets_every_secret_and_the_port(rig: HostRig) -> None:
    env = await granted(rig, "s3cret")
    channel = channel_of(rig)

    await channel.stop()

    with pytest.raises(RuntimeError):
        _ = channel.url
    with pytest.raises(OSError):
        await connect(env[ENV_HOST_URL], additional_headers=bearer(env))
    again = await channel.start()
    assert again is None
    with pytest.raises(InvalidStatus) as refused:
        await connect(channel.url, additional_headers=bearer(env))
    assert refused.value.response.status_code == 401, "the old secret is not remembered"


async def test_stopping_a_channel_that_never_started_is_quiet(rig: HostRig) -> None:
    await HostChannel(rig.model, hooks=rig.hooks, processes=ExtensionProcesses()).stop()  # type: ignore[arg-type]


# ── Admission ─────────────────────────────────────────────────────


async def test_a_path_other_than_the_endpoint_is_not_found(rig: HostRig) -> None:
    env = await granted(rig)

    assert await status_of(env[ENV_HOST_URL], path="/v1/other", headers=bearer(env)) == 404
    assert await status_of(env[ENV_HOST_URL], path="/", headers=bearer(env)) == 404


async def test_a_query_string_on_the_endpoint_is_still_the_endpoint(rig: HostRig) -> None:
    env = await granted(rig)

    socket = await connect(f"{env[ENV_HOST_URL]}?v=1", additional_headers=bearer(env))
    assert json.loads(await socket.recv())["op"] == "hello"
    await socket.close()


async def test_any_origin_is_forbidden_even_with_the_right_secret(rig: HostRig) -> None:
    env = await granted(rig)

    status = await status_of(
        env[ENV_HOST_URL], headers={**bearer(env), "Origin": "http://127.0.0.1"}
    )

    assert status == 403


async def test_a_host_that_is_not_the_loopback_address_and_port_is_misdirected(
    rig: HostRig,
) -> None:
    env = await granted(rig)
    port = env[ENV_HOST_URL].split(":")[2].split("/")[0]

    for host in ("evil.example", f"evil.example:{port}", "127.0.0.1", f"127.0.0.1:{int(port) + 1}"):
        status = await status_of(env[ENV_HOST_URL], host=host, headers=bearer(env))
        assert status == 421, host


async def test_the_bracketed_form_of_the_loopback_address_is_not_the_host(
    rig: HostRig,
) -> None:
    env = await granted(rig)
    port = env[ENV_HOST_URL].split(":")[2].split("/")[0]

    assert await status_of(env[ENV_HOST_URL], host=f"[::1]:{port}", headers=bearer(env)) == 421


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Bearer ", "Bearer wrong", "Basic s3cret", "s3cret", "Token s3cret"],
)
async def test_a_missing_empty_wrong_or_non_bearer_secret_is_unauthorized(
    rig: HostRig, authorization: str | None
) -> None:
    env = await granted(rig, "s3cret")
    headers = {} if authorization is None else {"Authorization": authorization}

    assert await status_of(env[ENV_HOST_URL], headers=headers) == 401


async def test_the_bearer_scheme_is_read_in_any_case(rig: HostRig) -> None:
    env = await granted(rig, "s3cret")

    socket = await connect(env[ENV_HOST_URL], additional_headers={"Authorization": "BEARER s3cret"})
    assert json.loads(await socket.recv())["extension_id"] == EXT
    await socket.close()


async def test_the_secret_alone_says_which_extension_is_connecting(rig: HostRig) -> None:
    rig.install("acme.other", host=["sessions"])
    channel = channel_of(rig)
    mine = await channel.grant(EXT, "secret-one")
    theirs = await channel.grant("acme.other", "secret-two")

    first = await connect(mine[ENV_HOST_URL], additional_headers=bearer(mine))
    second = await connect(theirs[ENV_HOST_URL], additional_headers=bearer(theirs))
    try:
        assert json.loads(await first.recv())["extension_id"] == EXT
        hello = json.loads(await second.recv())
        assert hello["extension_id"] == "acme.other"
        assert "chat.read" not in hello["granted"]
    finally:
        await first.close()
        await second.close()


# ── What a connection is told and asked ───────────────────────────


async def test_a_connection_is_told_hello_with_what_the_extension_holds(rig: HostRig) -> None:
    env = await granted(rig)
    socket = await connect(env[ENV_HOST_URL], additional_headers=bearer(env))
    try:
        hello = json.loads(await socket.recv())
    finally:
        await socket.close()

    assert hello["op"] == "hello" and hello["extension_id"] == EXT
    assert hello["schema_version"] == rig.model.services.host_schema.version  # type: ignore[union-attr]
    assert {"sessions", "chat.read"} <= set(hello["granted"])


async def test_a_request_is_answered_as_a_process_with_its_id(rig: HostRig) -> None:
    socket = await open_socket(await granted(rig))
    try:
        reply = await exchange(socket, {"id": "q-1", "op": "get", "path": "self.id"})
    finally:
        await socket.close()

    assert reply == {"op": "reply", "id": "q-1", "ok": True, "result": EXT, "error": None}


@pytest.mark.parametrize(
    ("frame", "id_"),
    [
        ("not json at all", 0),
        ("[1, 2]", 0),
        ('"text"', 0),
        ("null", 0),
        ({"op": "get"}, 0),
        ({"id": 5, "op": "nonsense"}, 5),
        ({"id": "x", "op": "get", "surprise": True}, "x"),
        ({"id": 9, "op": "get", "params": "not a dict"}, 9),
    ],
)
async def test_a_frame_that_is_not_a_request_is_refused_as_invalid(
    rig: HostRig, frame: Any, id_: int | str
) -> None:
    socket = await open_socket(await granted(rig))
    try:
        reply = await exchange(socket, frame)
        after = await exchange(socket, {"id": 2, "op": "get", "path": "self.id"})
    finally:
        await socket.close()

    assert reply["ok"] is False and reply["id"] == id_
    assert reply["error"]["code"] == "invalid"
    assert reply["error"]["message"].startswith("not a host-model request: ")
    assert after["ok"] is True, "the connection goes on after a bad frame"


async def test_an_answer_with_params_that_are_not_an_answer_is_invalid(rig: HostRig) -> None:
    socket = await open_socket(await granted(rig))
    try:
        reply = await exchange(socket, {"id": 3, "op": "answer", "params": {"outcome": "allow"}})
    finally:
        await socket.close()

    assert reply["id"] == 3 and reply["ok"] is False
    assert reply["error"]["code"] == "invalid"
    assert reply["error"]["message"].startswith("not an answer: ")


async def test_an_answer_to_a_hook_nothing_waits_on_is_not_found(rig: HostRig) -> None:
    socket = await open_socket(await granted(rig))
    try:
        reply = await exchange(
            socket,
            {"id": 4, "op": "answer", "params": {"hook": "h-404", "outcome": "allow"}},
        )
    finally:
        await socket.close()

    assert reply["ok"] is False and reply["error"]["code"] == "not_found"
    assert "nothing waits on 'h-404' from this extension" in reply["error"]["message"]


# ── Signals ───────────────────────────────────────────────────────


async def test_a_signal_the_extension_subscribed_to_is_written_to_its_socket(
    rig: HostRig,
) -> None:
    socket = await open_socket(await granted(rig))
    try:
        await exchange(socket, {"id": 1, "op": "subscribe", "signals": ["session.switched"]})
        rig.model.services.bus.emit("session.switched", {"session_id": "s7"})  # type: ignore[union-attr]
        signal = json.loads(await asyncio.wait_for(socket.recv(), 5))
    finally:
        await socket.close()

    assert signal["op"] == "signal" and signal["signal"] == "session.switched"


def test_a_signal_for_a_subscriber_that_is_not_reading_is_dropped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = _SocketSink()
    message = SignalMessage(signal="chat.run_completed", value=1)
    for _ in range(10_000):
        sink.deliver(message)
    assert sink.queue.full() and not caplog.records

    with caplog.at_level(logging.WARNING):
        sink.deliver(message)

    assert sink.queue.qsize() == 10_000
    assert "dropped chat.run_completed" in caplog.text


def test_a_hook_event_for_a_process_that_is_not_reading_is_lost_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = _SocketSink()
    for _ in range(10_000):
        sink.send("frame")

    with caplog.at_level(logging.WARNING):
        sink.send("one too many")

    assert sink.queue.qsize() == 10_000
    assert "a hook event is lost" in caplog.text


# ── Revoking, cutting off, oversize ───────────────────────────────


async def test_revoking_closes_live_connections_and_forgets_the_secret(rig: HostRig) -> None:
    env = await granted(rig)
    socket = await open_socket(env)

    channel_of(rig).revoke(EXT)
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(socket.recv(), 5)

    assert socket.close_code == 4001 and socket.close_reason == "revoked"
    with pytest.raises(InvalidStatus):
        await connect(env[ENV_HOST_URL], additional_headers=bearer(env))


async def test_revoking_an_extension_nobody_granted_or_connected_does_nothing(
    rig: HostRig,
) -> None:
    env = await granted(rig)
    channel_of(rig).revoke("acme.ghost")
    channel_of(rig).revoke("acme.ghost")

    socket = await open_socket(env)
    await socket.close()


async def test_a_frame_over_the_limit_ends_the_connection(rig: HostRig) -> None:
    env = await granted(rig)
    socket = await open_socket(env)

    await socket.send("x" * (MAX_FRAME + 1))
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(socket.recv(), 10)

    assert socket.close_code == 1009
    again = await open_socket(env)
    await again.close()


async def test_a_connection_that_goes_away_is_forgotten_by_the_hooks_and_the_bus(
    rig: HostRig,
) -> None:
    socket = await open_socket(await granted(rig))
    await exchange(socket, {"id": 1, "op": "subscribe", "signals": ["session.switched"]})
    assert rig.hooks._connections.connected(EXT)

    await socket.close()
    await until(lambda: not rig.hooks._connections.connected(EXT))

    assert not rig.hooks._connections.connected(EXT)
    assert rig.model.services.bus.emit("session.switched", {"session_id": "s7"}) == []  # type: ignore[union-attr]


# ── The process group ─────────────────────────────────────────────


class RecordingProcesses(ExtensionProcesses):
    def __init__(self) -> None:
        super().__init__()
        self.claims: list[tuple[str, str]] = []

    async def claim(self, extension_id: str, raw: str) -> bool:
        self.claims.append((extension_id, raw))
        return await super().claim(extension_id, raw)


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are not claimed on Windows")
async def test_a_process_that_names_the_group_it_leads_has_it_claimed(
    rig: HostRig, tmp_path: Path
) -> None:
    processes = RecordingProcesses()
    channel = HostChannel(rig.model, hooks=rig.hooks, processes=processes)  # type: ignore[arg-type]
    env = await channel.grant(EXT, "s3cret")
    leader = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", start_new_session=True
    )
    try:
        socket = await connect(
            env[ENV_HOST_URL],
            additional_headers={**bearer(env), PROCESS_GROUP_HEADER: str(leader.pid)},
        )
        await socket.recv()
        await until(lambda: processes.group_of(EXT) is not None)
        assert processes.group_of(EXT) == leader.pid
        assert processes.claims == [(EXT, str(leader.pid))]
        await socket.close()
    finally:
        leader.kill()
        await leader.wait()
        await channel.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are not claimed on Windows")
async def test_a_group_igni_did_not_start_is_ignored_and_no_header_claims_nothing(
    rig: HostRig,
) -> None:
    processes = RecordingProcesses()
    channel = HostChannel(rig.model, hooks=rig.hooks, processes=processes)  # type: ignore[arg-type]
    env = await channel.grant(EXT, "s3cret")
    try:
        bare = await connect(env[ENV_HOST_URL], additional_headers=bearer(env))
        await bare.recv()
        assert processes.claims == []

        foreign = await connect(
            env[ENV_HOST_URL],
            additional_headers={**bearer(env), PROCESS_GROUP_HEADER: str(os.getpid())},
        )
        await foreign.recv()
        await until(lambda: processes.claims)
        await asyncio.sleep(0.5)  # long enough for the check on the claimed group to finish
        assert processes.claims == [(EXT, str(os.getpid()))]
        assert processes.group_of(EXT) is None, "this test's own process is not a child of itself"
        await bare.close()
        await foreign.close()
    finally:
        await channel.stop()
