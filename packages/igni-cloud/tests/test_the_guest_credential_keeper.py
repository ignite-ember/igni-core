"""The guest's keeper of the VM credential: who has one, and who is told when it moves.

``docs/CLOUD.md`` §9, ``igni_cloud/guest/sign_in.py``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from igni_cloud.credentials import EnvironmentCredentials
from igni_cloud.guest.binding import ProducerEnv
from igni_cloud.guest.credential import CredentialState, StateChange
from igni_cloud.guest.resume import RestoreStep, RestoreWatch
from igni_cloud.guest.sign_in import GuestCredential

from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.enums import PushChannel


class Transport:
    def __init__(self, *, failing: bool = False) -> None:
        self.sent: list[msg.Message] = []
        self.failing = failing

    async def send(self, message: msg.Message) -> None:
        if self.failing:
            raise ConnectionError("no window")
        self.sent.append(message)

    def observe(self, observer: Any) -> None: ...

    @property
    def client_count(self) -> int:
        return 0


async def after_wake(*, burst: bool = True) -> None: ...


def credential() -> tuple[GuestCredential, EnvironmentCredentials]:
    tokens = EnvironmentCredentials()
    return GuestCredential(tokens, after_wake=after_wake), tokens


BOUND = ProducerEnv.parse("IGNI_PRODUCER=person:abc\nIGNI_ORG=org1")


@pytest.fixture(autouse=True)
def _run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IGNI_RUN_DIR", str(tmp_path))
    monkeypatch.delenv("IGNI_VM_CREDENTIAL", raising=False)
    monkeypatch.setenv("IGNI_VM_CREDENTIAL", str(tmp_path / "vm-credential"))


def test_a_vm_not_bound_to_a_producer_keeps_no_credential() -> None:
    guest, _ = credential()
    guest.open(None, lambda: "https://igni.example", transport=Transport(), moved=lambda: None)
    assert guest.keeper is None
    assert guest.state() is None


def test_without_an_api_url_there_is_nothing_to_keep_it_with() -> None:
    guest, _ = credential()
    guest.open(BOUND, None, transport=Transport(), moved=lambda: None)
    assert guest.keeper is None
    assert guest.state() is None


def test_a_bound_vm_keeps_one_that_starts_by_needing_a_sign_in() -> None:
    guest, _ = credential()
    guest.open(BOUND, lambda: "https://igni.example", transport=Transport(), moved=lambda: None)
    assert guest.keeper is not None
    assert guest.state() == CredentialState.NEEDS_SIGN_IN.value


async def test_signing_in_without_a_keeper_says_this_is_not_a_producers_vm() -> None:
    guest, _ = credential()
    guest.open(None, None, transport=Transport(), moved=lambda: None)
    change = await guest.sign_in("session", "org1")
    assert change == StateChange(state=CredentialState.NEEDS_SIGN_IN, message="not a producer's VM")
    assert await guest.sign_in("session") == change


async def test_serving_and_stopping_without_a_keeper_touch_nothing() -> None:
    guest, tokens = credential()
    guest.open(None, None, transport=Transport(), moved=lambda: None)
    sentinel = object()
    tokens._keeper = sentinel  # type: ignore[assignment]
    restores = RestoreWatch(Path("restored"), watch=False)
    guest.serve(restores)
    await guest.stop()
    assert tokens._keeper is sentinel, "the tokens' keeper is not this VM's to change"
    assert restores._steps_by[RestoreStep.LATER] == []


async def test_serving_makes_the_keeper_the_tokens_and_stopping_lets_go(
    caplog: pytest.LogCaptureFixture,
) -> None:
    guest, tokens = credential()
    transport, moved = Transport(), []
    guest.open(
        BOUND, lambda: "https://igni.example", transport=transport, moved=lambda: moved.append(1)
    )
    restores = RestoreWatch(Path("restored"), watch=False)
    with caplog.at_level(logging.WARNING, logger="igni_cloud.guest.sign_in"):
        guest.serve(restores)
        assert tokens._keeper is guest.keeper
        assert len(restores._steps_by[RestoreStep.LATER]) == 1
        for _ in range(100):
            if transport.sent and moved:
                break
            await asyncio.sleep(0.01)
    await guest.stop()
    assert tokens._keeper is None
    (push,) = transport.sent
    assert isinstance(push, msg.PushNotification)
    assert push.channel == PushChannel.VM_CREDENTIAL
    assert push.payload["state"] == CredentialState.NEEDS_SIGN_IN.value
    assert moved == [1]
    assert "VM credential: needs_sign_in" in caplog.text


async def test_a_state_that_is_not_ready_is_warned_of_and_ready_is_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    guest, _ = credential()
    moved: list[int] = []
    guest.open(None, None, transport=Transport(), moved=lambda: moved.append(1))
    with caplog.at_level(logging.INFO, logger="igni_cloud.guest.sign_in"):
        guest._state_moved(StateChange(state=CredentialState.READY))
        assert caplog.records == []
        guest._state_moved(StateChange(state=CredentialState.NEEDS_SIGN_IN, message="expired"))
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "VM credential: needs_sign_in (expired)"
    assert moved == [1, 1]


async def test_a_state_that_moves_before_there_is_a_transport_still_calls_moved() -> None:
    guest, _ = credential()
    moved: list[int] = []
    guest._moved = lambda: moved.append(1)
    guest._state_moved(StateChange(state=CredentialState.READY))
    assert moved == [1]


async def test_a_push_that_cannot_be_sent_does_not_stop_the_state_being_recorded() -> None:
    guest, _ = credential()
    moved: list[int] = []
    guest.open(None, None, transport=Transport(failing=True), moved=lambda: moved.append(1))
    guest._state_moved(StateChange(state=CredentialState.READY))
    await asyncio.sleep(0.05)
    assert moved == [1]
