"""``ExtensionProcesses``: a process names its group, igni believes it only for its own child.

Real children, the real ``ps`` check. Only the failures of ``ps`` and of
``killpg``, and the platform, are brought about by hand.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from igni_core.extensions.host import processes as processes_module
from igni_core.extensions.host.processes import PROCESS_GROUP_HEADER, ExtensionProcesses

posix = pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")


@pytest.fixture
def leader() -> Iterator[subprocess.Popen[bytes]]:
    """A child of this process leading a group of its own."""
    child = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_the_header_is_named() -> None:
    assert PROCESS_GROUP_HEADER == "X-Igni-Process-Group"


@posix
async def test_a_group_led_by_igni_s_own_child_is_recorded(leader: subprocess.Popen[bytes]) -> None:
    processes = ExtensionProcesses()

    assert await processes.claim("acme.one", str(leader.pid)) is True

    assert processes.group_of("acme.one") == leader.pid
    assert processes.group_of("acme.other") is None


@posix
async def test_a_later_claim_replaces_the_earlier_group(leader: subprocess.Popen[bytes]) -> None:
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        processes = ExtensionProcesses()
        await processes.claim("acme.one", str(leader.pid))

        await processes.claim("acme.one", str(other.pid))

        assert processes.group_of("acme.one") == other.pid
    finally:
        other.kill()
        other.wait()


@posix
@pytest.mark.parametrize("raw", ["", "abc", "1.5", "0", "1", "-1", "-4242", " "])
async def test_a_claim_that_is_not_a_group_above_one_is_ignored(raw: str) -> None:
    processes = ExtensionProcesses()

    assert await processes.claim("acme.one", raw) is False

    assert processes.group_of("acme.one") is None


@posix
async def test_igni_itself_is_not_a_child_of_igni() -> None:
    processes = ExtensionProcesses()

    assert await processes.claim("acme.one", str(os.getpid())) is False
    assert processes.group_of("acme.one") is None


@posix
async def test_a_process_that_does_not_exist_is_not_claimed() -> None:
    gone = subprocess.Popen(["true"])
    gone.wait()
    processes = ExtensionProcesses()

    assert await processes.claim("acme.one", str(gone.pid)) is False


@posix
async def test_a_refused_claim_leaves_the_group_recorded_earlier(
    leader: subprocess.Popen[bytes],
) -> None:
    processes = ExtensionProcesses()
    await processes.claim("acme.one", str(leader.pid))

    await processes.claim("acme.one", str(os.getpid()))

    assert processes.group_of("acme.one") == leader.pid


class Ps:
    """A stand-in for the ``ps`` process, answering what it is told to."""

    def __init__(self, out: bytes = b"", hangs: bool = False) -> None:
        self.out = out
        self.hangs = hangs

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.hangs:
            await asyncio.sleep(60)
        return self.out, b""


def with_ps(monkeypatch: pytest.MonkeyPatch, ps: Ps | OSError) -> None:
    async def start(*args: Any, **kwargs: Any) -> Ps:
        if isinstance(ps, OSError):
            raise ps
        return ps

    monkeypatch.setattr(processes_module.asyncio, "create_subprocess_exec", start)


@posix
async def test_a_claim_is_ignored_when_ps_cannot_be_run(monkeypatch: pytest.MonkeyPatch) -> None:
    with_ps(monkeypatch, FileNotFoundError("ps"))
    processes = ExtensionProcesses()

    assert await processes.claim("acme.one", "4242") is False
    assert processes.group_of("acme.one") is None


@posix
@pytest.mark.parametrize("out", [b"", b"   \n", b"garbage\n", b"1.5"])
async def test_a_claim_is_ignored_when_ps_says_nothing_usable(
    monkeypatch: pytest.MonkeyPatch, out: bytes
) -> None:
    with_ps(monkeypatch, Ps(out))

    assert await ExtensionProcesses().claim("acme.one", "4242") is False


@posix
async def test_a_claim_is_recorded_when_ps_names_igni_as_the_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_ps(monkeypatch, Ps(f"  {os.getpid()}\n".encode()))
    processes = ExtensionProcesses()

    assert await processes.claim("acme.one", "4242") is True
    assert processes.group_of("acme.one") == 4242


@posix
@pytest.mark.parametrize("raw", ["abc", "0", "1", "-3"])
async def test_a_claim_that_is_no_group_is_ignored_even_where_ps_would_vouch(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    with_ps(monkeypatch, Ps(f"{os.getpid()}\n".encode()))
    processes = ExtensionProcesses()

    assert await processes.claim("acme.one", raw) is False
    assert processes.group_of("acme.one") is None


@posix
async def test_a_claim_is_ignored_when_ps_takes_too_long(monkeypatch: pytest.MonkeyPatch) -> None:
    with_ps(monkeypatch, Ps(hangs=True))

    async def impatient(awaitable: Any, timeout: float) -> Any:
        assert timeout == 5
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(processes_module.asyncio, "wait_for", impatient)

    assert await ExtensionProcesses().claim("acme.one", "4242") is False


async def test_nothing_is_claimed_on_windows(
    monkeypatch: pytest.MonkeyPatch, leader: subprocess.Popen[bytes]
) -> None:
    monkeypatch.setattr(processes_module.sys, "platform", "win32")
    processes = ExtensionProcesses()

    assert await processes.claim("acme.one", str(leader.pid)) is False
    assert processes.group_of("acme.one") is None


@posix
async def test_ending_a_group_kills_it_and_forgets_it(leader: subprocess.Popen[bytes]) -> None:
    processes = ExtensionProcesses()
    await processes.claim("acme.one", str(leader.pid))

    assert processes.kill("acme.one") is True

    assert leader.wait(timeout=10) == -signal.SIGKILL
    assert processes.group_of("acme.one") is None
    assert processes.kill("acme.one") is False


def test_ending_an_extension_that_has_no_group_is_false() -> None:
    assert ExtensionProcesses().kill("acme.unknown") is False


@posix
async def test_ending_a_group_that_is_already_gone_is_false_and_forgets_it(
    leader: subprocess.Popen[bytes],
) -> None:
    processes = ExtensionProcesses()
    await processes.claim("acme.one", str(leader.pid))
    leader.kill()
    leader.wait()

    assert processes.kill("acme.one") is False
    assert processes.group_of("acme.one") is None


@posix
async def test_ending_a_group_igni_may_not_signal_is_false_and_forgets_it(
    monkeypatch: pytest.MonkeyPatch, leader: subprocess.Popen[bytes]
) -> None:
    processes = ExtensionProcesses()
    await processes.claim("acme.one", str(leader.pid))

    def refused(group: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(processes_module.os, "killpg", refused)

    assert processes.kill("acme.one") is False
    assert processes.group_of("acme.one") is None
    assert leader.poll() is None, "nothing was signalled"


@posix
async def test_forgetting_drops_the_group_without_ending_it(
    leader: subprocess.Popen[bytes],
) -> None:
    processes = ExtensionProcesses()
    await processes.claim("acme.one", str(leader.pid))

    processes.forget("acme.one")
    processes.forget("acme.never")

    assert processes.group_of("acme.one") is None
    assert processes.kill("acme.one") is False
    assert leader.poll() is None


@posix
async def test_two_sets_of_groups_do_not_see_each_other(
    leader: subprocess.Popen[bytes],
) -> None:
    mine, yours = ExtensionProcesses(), ExtensionProcesses()

    await mine.claim("acme.one", str(leader.pid))

    assert yours.group_of("acme.one") is None
    assert yours.kill("acme.one") is False
    assert leader.poll() is None
