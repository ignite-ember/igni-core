"""The lockfile payload's probes and the discovery result's arms, at their edges.

``test_backend_lockfile.py`` pins the happy paths and ``test_the_lockfile_at_its_edges.py``
the file handling. This file pins what is left in ``schemas_lockfile``: a process
we may not signal still counts as alive, the port probe's host and timeout reach the
socket, a payload is stamped with the wall clock, and a discovery result read back
from plain JSON picks its arm by ``status`` and refuses a tag it does not know.
"""

from __future__ import annotations

import os
import socket
import time

import pytest
from pydantic import TypeAdapter, ValidationError

from igni_core.backend.schemas_lockfile import (
    DiscoveryOutcome,
    DiscoveryResult,
    LiveBackend,
    LockfilePayload,
    NoBackend,
    VersionMismatch,
)


def _payload(**over: object) -> LockfilePayload:
    fields: dict[str, object] = {"pid": 4242, "port": 1, "wire_version": "1.0", "created_at": 0}
    fields.update(over)
    return LockfilePayload(**fields)  # type: ignore[arg-type]


def _kill_raising(exc: BaseException):
    def kill(pid: int, sig: int) -> None:
        raise exc

    return kill


def test_a_process_we_may_not_signal_is_still_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "kill", _kill_raising(PermissionError()))

    assert _payload().is_pid_alive() is True


def test_a_process_that_is_gone_is_not_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "kill", _kill_raising(ProcessLookupError()))

    assert _payload().is_pid_alive() is False


def test_any_other_os_error_from_the_probe_is_not_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "kill", _kill_raising(OSError("odd")))

    assert _payload().is_pid_alive() is False


@pytest.mark.parametrize("pid", [0, -1])
def test_a_non_positive_pid_is_never_signalled(monkeypatch: pytest.MonkeyPatch, pid: int) -> None:
    # kill(0, 0) and kill(-1, 0) would address a whole process group.
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("signalled"))

    assert _payload(pid=pid).is_pid_alive() is False


def test_the_port_probe_connects_to_the_host_and_timeout_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[tuple[str, int], float]] = []

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def connect(address: tuple[str, int], timeout: float) -> _Conn:
        seen.append((address, timeout))
        return _Conn()

    monkeypatch.setattr(socket, "create_connection", connect)

    assert _payload(port=7000).is_port_reachable() is True
    assert _payload(port=7000).is_port_reachable(host="10.0.0.9", timeout=2.5) is True
    assert seen == [(("127.0.0.1", 7000), 0.5), (("10.0.0.9", 7000), 2.5)]


def test_the_port_probe_says_no_when_the_connect_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def connect(*_a: object, **_k: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(socket, "create_connection", connect)

    assert _payload().is_port_reachable() is False


def test_a_payload_is_stamped_with_the_wall_clock_in_whole_seconds() -> None:
    before = int(time.time())
    stamped = LockfilePayload.now(pid=7, port=8, wire_version="2.0")
    after = int(time.time())

    assert isinstance(stamped.created_at, int)
    assert before <= stamped.created_at <= after
    assert (stamped.pid, stamped.port, stamped.wire_version, stamped.ws_token) == (
        7,
        8,
        "2.0",
        None,
    )
    assert LockfilePayload.now(pid=7, port=8, wire_version="2.0", ws_token="t").ws_token == "t"


def test_the_age_is_whole_seconds_and_never_negative() -> None:
    p = _payload(created_at=100)

    assert p.age_seconds(now=160.9) == 60
    assert p.age_seconds(now=100) == 0
    assert p.age_seconds(now=40) == 0


def test_a_discovery_result_read_from_json_picks_its_arm_by_status() -> None:
    adapter = TypeAdapter(DiscoveryResult)
    payload = {"pid": 1, "port": 2, "wire_version": "1.0", "created_at": 3}

    live = adapter.validate_python({"status": "live", "payload": payload})
    none = adapter.validate_python({"status": "none"})
    mismatch = adapter.validate_python(
        {"status": "version_mismatch", "payload": payload, "expected": "2.0"}
    )

    assert isinstance(live, LiveBackend) and live.payload.port == 2
    assert isinstance(none, NoBackend)
    assert isinstance(mismatch, VersionMismatch)
    assert mismatch.expected == "2.0"
    assert [live.status, none.status, mismatch.status] == [
        DiscoveryOutcome.LIVE,
        DiscoveryOutcome.NONE,
        DiscoveryOutcome.VERSION_MISMATCH,
    ]


@pytest.mark.parametrize(
    "raw",
    [
        {"status": "gone"},
        {"payload": {"pid": 1, "port": 2, "wire_version": "1", "created_at": 3}},
        {
            "status": "version_mismatch",
            "payload": {"pid": 1, "port": 2, "wire_version": "1", "created_at": 3},
        },
        {
            "status": "live",
            "payload": {"pid": "x", "port": 2, "wire_version": "1", "created_at": 3},
        },
        {"status": "live", "payload": {"port": 2, "wire_version": "1", "created_at": 3}},
    ],
)
def test_a_discovery_result_with_an_unknown_tag_or_a_broken_payload_is_refused(
    raw: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(DiscoveryResult).validate_python(raw)
