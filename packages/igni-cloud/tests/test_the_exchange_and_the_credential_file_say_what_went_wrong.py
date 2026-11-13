"""``igni_cloud/guest/credential.py``: the refusals and failures the happy path never meets.

``test_the_vm_holds_its_own_credential.py`` runs the rotation end to end;
this is what the exchange, the file and the keeper make of an answer
that is not a grant.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from igni_cloud.guest.credential import (
    CredentialFile,
    CredentialState,
    ExchangeFailed,
    InvalidGrant,
    StateChange,
    VmCredentialExchange,
    VmCredentials,
)

from igni_core.core.activity import ActivityRegistry

GRANT = {
    "access_token": "token-1",
    "token_type": "Bearer",
    "expires_in": 900,
    "vm_credential": "credential-1",
}


def _exchange(
    answer: Callable[[httpx.Request], httpx.Response] | httpx.Response,
    url: str | None = "https://igni.test",
) -> tuple[VmCredentialExchange, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request) if callable(answer) else answer

    return VmCredentialExchange(lambda: url, transport=httpx.MockTransport(handle)), seen


async def test_a_grant_is_posted_as_a_form_to_the_token_endpoint():
    exchange, seen = _exchange(httpx.Response(200, json=GRANT), "https://igni.test/")
    grant = await exchange.sign_in("the-session", "org-acme")
    assert grant.access_token == "token-1" and grant.vm_credential == "credential-1"
    request = seen[0]
    assert request.method == "POST" and str(request.url) == "https://igni.test/v1/oauth/token"
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert form == {
        "grant_type": VmCredentialExchange.SIGN_IN_GRANT,
        "subject_token": "the-session",
        "organisation": "org-acme",
    }
    await exchange.aclose()


async def test_a_sign_in_without_an_organisation_names_none():
    exchange, seen = _exchange(httpx.Response(200, json=GRANT))
    await exchange.sign_in("the-session")
    assert "organisation" not in seen[0].content.decode()
    await exchange.aclose()


async def test_a_refused_grant_carries_the_servers_reason():
    exchange, _ = _exchange(
        httpx.Response(400, json={"error": "invalid_grant", "error_description": "reused"})
    )
    with pytest.raises(InvalidGrant, match="reused"):
        await exchange.rotate("c")
    bare, _ = _exchange(httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(InvalidGrant, match="invalid_grant"):
        await bare.rotate("c")
    await exchange.aclose()
    await bare.aclose()


@pytest.mark.parametrize(
    "answer, why",
    [
        (httpx.Response(400, content=b"<html>bad request</html>"), "400"),
        (httpx.Response(400, json={"error": "invalid_request"}), "400"),
        (httpx.Response(401, json={"error": "invalid_grant"}), "401"),
        (httpx.Response(503, text="down"), "503"),
        (httpx.Response(200, content=b"not json"), "malformed"),
        (httpx.Response(200, json={"access_token": "t"}), "malformed"),
    ],
)
async def test_an_answer_that_is_not_a_refusal_or_a_grant_is_a_failure_to_try_again(
    answer: httpx.Response, why: str
):
    exchange, _ = _exchange(answer)
    with pytest.raises(ExchangeFailed, match=why):
        await exchange.rotate("c")
    await exchange.aclose()


async def test_a_server_that_cannot_be_reached_is_a_failure_to_try_again():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    exchange, _ = _exchange(down)
    with pytest.raises(ExchangeFailed, match="could not be reached"):
        await exchange.rotate("c")
    await exchange.aclose()


@pytest.mark.parametrize("url", [None, ""])
async def test_without_an_igni_server_there_is_nothing_to_ask(url: str | None):
    exchange, seen = _exchange(httpx.Response(200, json=GRANT), url)
    with pytest.raises(ExchangeFailed, match="no igni server"):
        await exchange.sign_in("s")
    assert seen == []


async def test_closing_drops_the_client_and_the_next_exchange_makes_another():
    exchange, _ = _exchange(httpx.Response(200, json=GRANT))
    first = exchange.client()
    assert exchange.client() is first
    await exchange.aclose()
    await exchange.aclose()
    assert exchange.client() is not first
    await exchange.aclose()


def test_the_credential_file_reads_what_is_there_stripped_or_nothing(tmp_path: Path):
    file = CredentialFile(tmp_path / "vm-credential")
    assert file.read() is None, "missing"
    file.path.write_text("  \n\t ")
    assert file.read() is None, "blank"
    file.path.write_text(" credential-1 \n")
    assert file.read() == "credential-1"
    file.path.unlink()
    file.path.mkdir()
    assert file.read() is None, "unreadable"


def test_the_credential_is_written_private_and_the_directory_is_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    synced: list[bool] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    file = CredentialFile(tmp_path / "vol" / "vm-credential")
    file.write("credential-1")
    assert file.read() == "credential-1"
    assert stat.S_IMODE(file.path.stat().st_mode) == 0o600
    assert synced == [False, True], "the file, then the rename's directory"
    assert [p.name for p in file.path.parent.iterdir()] == ["vm-credential"]


class Keeper:
    def __init__(self, tmp_path: Path, answer: Any, *, producer: str = "person:u-ada") -> None:
        self.exchange, self.seen = _exchange(answer)
        self.credential = CredentialFile(tmp_path / "vol" / "vm-credential")
        self.credential.path.parent.mkdir(parents=True, exist_ok=True)
        self.token = tmp_path / "run" / "token"
        self.changes: list[StateChange] = []
        self.now = 1_800_000_000.0
        self.keeper = VmCredentials(
            credential_file=self.credential,
            token_file=self.token,
            exchange=self.exchange,
            producer=producer,
            on_state=self.changes.append,
            clock=lambda: self.now,
        )


async def test_a_refused_sign_in_asks_the_person_to_sign_in_again(tmp_path: Path):
    vm = Keeper(
        tmp_path,
        httpx.Response(400, json={"error": "invalid_grant", "error_description": "expired"}),
    )
    change = await vm.keeper.sign_in("s")
    assert change == StateChange(
        state=CredentialState.NEEDS_SIGN_IN, message="the sign-in was refused: expired"
    )
    assert vm.credential.read() is None and not vm.token.exists()
    await vm.keeper.stop()


async def test_a_sign_in_that_cannot_reach_the_server_leaves_the_state_unreachable(tmp_path: Path):
    vm = Keeper(tmp_path, httpx.Response(502))
    change = await vm.keeper.sign_in("s")
    assert change.state is CredentialState.UNREACHABLE
    assert "502" in change.message
    assert vm.credential.read() is None
    await vm.keeper.stop()


async def test_a_state_is_announced_once_and_a_listener_that_raises_changes_nothing(
    tmp_path: Path,
):
    vm = Keeper(tmp_path, httpx.Response(200, json=GRANT))
    told: list[CredentialState] = []

    def listener(change: StateChange) -> None:
        told.append(change.state)
        raise RuntimeError("the listener is broken")

    vm.keeper._on_state = listener
    await vm.keeper.sign_in("s")
    assert vm.keeper.state is CredentialState.READY
    assert vm.token.read_text() == "token-1", "the grant was taken all the same"
    await vm.keeper.refresh()
    assert told == [CredentialState.READY], "the same state is not announced again"
    await vm.keeper.stop()


async def test_a_token_that_is_still_fresh_is_not_exchanged_again_unless_forced(tmp_path: Path):
    vm = Keeper(tmp_path, httpx.Response(200, json=GRANT))
    await vm.keeper.sign_in("s")
    assert len(vm.seen) == 1
    await vm.keeper.refresh(force=False)
    assert len(vm.seen) == 1, "fresh: nothing to renew"
    vm.now += 900 - 59  # within the minute before it expires
    await vm.keeper.refresh(force=False)
    assert len(vm.seen) == 2, "due"
    await vm.keeper.refresh(force=True)
    assert len(vm.seen) == 3, "forced"
    await vm.keeper.stop()


async def test_a_credential_that_vanished_is_missing_not_unreachable(tmp_path: Path):
    app = Keeper(tmp_path, httpx.Response(200, json=GRANT), producer="app:a-1")
    change = await app.keeper.refresh()
    assert change == StateChange(state=CredentialState.NEEDS_REISSUE, message="no VM credential")
    person = Keeper(tmp_path, httpx.Response(200, json=GRANT))
    assert (await person.keeper.refresh()).state is CredentialState.NEEDS_SIGN_IN
    assert app.seen == [] and person.seen == []
    await app.keeper.stop()
    await person.keeper.stop()


async def test_the_token_is_dropped_even_when_it_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    answers = iter(
        [httpx.Response(200, json=GRANT), httpx.Response(400, json={"error": "invalid_grant"})]
    )
    vm = Keeper(tmp_path, lambda _request: next(answers))
    await vm.keeper.sign_in("s")

    def stuck(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", stuck)
    change = await vm.keeper.refresh()
    assert change.state is CredentialState.NEEDS_SIGN_IN
    assert vm.keeper.expires_at == 0.0
    monkeypatch.undo()
    await vm.keeper.stop()


@pytest.mark.parametrize(
    "producer, app",
    [
        ("app:a-1", True),
        ("space:s-1", True),
        ("person:u-ada", False),
        ("nonsense", False),
        ("", False),
    ],
)
def test_only_an_apps_credential_is_reissued_not_signed_in_again(
    tmp_path: Path, producer: str, app: bool
):
    assert Keeper(tmp_path, httpx.Response(200, json=GRANT), producer=producer).keeper.is_app is app


async def test_the_file_a_sign_in_writes_does_not_send_the_watcher_to_renew_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """With no credential on the volume the keeper watches for the file; a sign-in writes
    it. Seen before the sign-in had finished (at once, where inotify watches the
    directory), the watcher renewed the credential just issued: a second exchange."""
    monkeypatch.setattr(VmCredentials, "FILE_POLL_SECONDS", 0.01)
    vm = Keeper(tmp_path, httpx.Response(200, json=GRANT))
    await vm.keeper.start()
    watcher = vm.keeper._watcher
    assert watcher is not None
    write_token = vm.keeper._token.write

    def slow_token_write(data: bytes) -> None:
        # The credential file is on the volume; the sign-in has not finished.
        time.sleep(0.3)
        write_token(data)

    vm.keeper._token.write = slow_token_write  # type: ignore[method-assign]
    await vm.keeper.sign_in("s")
    await asyncio.wait([watcher], timeout=2)
    await asyncio.sleep(0.05)

    assert watcher.done() and vm.keeper._watcher is None, "a sign-in ends the watch"
    assert len(vm.seen) == 1, "the sign-in's exchange, and no renewal after it"
    assert vm.keeper.state is CredentialState.READY
    await vm.keeper.stop()


async def test_a_restore_arms_the_renewal_again_from_the_wall_clock(tmp_path: Path):
    vm = Keeper(tmp_path, httpx.Response(200, json=GRANT))
    await vm.keeper.start()
    await vm.keeper.sign_in("s")
    armed: list[float] = []
    vm.keeper._arm = armed.append  # type: ignore[method-assign]
    vm.now += 500
    ActivityRegistry.shared().rearm()
    await asyncio.sleep(0.01)
    assert armed == [pytest.approx(900 - 500 - 60)]
    vm.now += 1000
    ActivityRegistry.shared().rearm()
    await asyncio.sleep(0.01)
    assert armed[-1] == 0.0, "overdue: at once"
    await vm.keeper.stop()


async def test_stopping_ends_the_timer_the_watcher_and_the_rearming(tmp_path: Path):
    vm = Keeper(tmp_path, httpx.Response(200, json=GRANT))
    await vm.keeper.stop()  # nothing started: nothing to end
    await vm.keeper.start()  # no credential on the volume: it watches for the file
    watcher = vm.keeper._watcher
    assert watcher is not None and not watcher.done()
    await vm.keeper.sign_in("s")
    timer = vm.keeper._timer
    assert timer is not None and not timer.done()
    armed: list[float] = []
    await vm.keeper.stop()
    assert watcher.cancelled() and timer.cancelled()
    assert vm.keeper._timer is None and vm.keeper._watcher is None
    vm.keeper._arm = armed.append  # type: ignore[method-assign]
    ActivityRegistry.shared().rearm()
    await asyncio.sleep(0.01)
    assert armed == [], "no longer listening for a restore"
