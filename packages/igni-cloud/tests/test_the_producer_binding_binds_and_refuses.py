"""What a bound igni is bound to, and what it says while it is not.

``docs/CLOUD.md`` §9. A warm base's igni starts before anybody it is
for is known: it waits for ``/run/igni/producer.env``, refusing every
client request meanwhile, and binds when the file names a producer, an
organisation, an incarnation and a database — and, when a token is
there, a token of that same producer. So the file's parsing, the
sentences it says while it waits, the tenant it binds to and the
refusal a client gets are all behaviour a client and a node agent see.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from igni_cloud.credentials import EnvironmentCredentials
from igni_cloud.guest import ProducerBinding, ProducerEnv, TokenTenant, UnboundRefusal
from igni_cloud.producer import ProducerIncarnation

from igni_core.core.config.schemas.storage_and_paths import StorageConfig
from igni_core.core.db.scope import Tenant
from igni_core.protocol import messages as msg

ORG = "org-acme"
PRODUCER = "person:u-ada"
INCARNATION = "m-1"
STORED = f"{PRODUCER}@{INCARNATION}"


def _jwt(**claims: Any) -> str:
    def part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"


ADA_TOKEN = _jwt(sub="u-ada", org=ORG, exp=int(time.time()) + 3600)
EVE_TOKEN = _jwt(sub="u-eve", org=ORG, exp=int(time.time()) + 3600)


@pytest.fixture
def cloud(cloud_layer: Any, monkeypatch) -> None:
    """The cloud layer, with nothing named yet: a warm base's igni."""
    for name in (
        StorageConfig.DATABASE_URL_ENV,
        EnvironmentCredentials.TOKEN_FILE_ENV,
        EnvironmentCredentials.TOKEN_ENV,
        ProducerEnv.ORG_ENV,
        ProducerEnv.PRODUCER_ENV,
        ProducerIncarnation.ENV,
    ):
        monkeypatch.setenv(name, "")  # recorded, so put back as it was
        monkeypatch.delenv(name, raising=False)


def _producer_env(
    path: Path, *, database: str = "sqlite:///x.db", token: Path | None = None
) -> None:
    lines = [
        f"IGNI_DATABASE_URL={database}",
        f"IGNI_ORG={ORG}",
        f"IGNI_PRODUCER={PRODUCER}",
        f"IGNI_PRODUCER_INCARNATION={INCARNATION}",
    ]
    if token is not None:
        lines.insert(0, f"IGNI_ACCESS_TOKEN_FILE={token}")
    path.write_text("\n".join(lines) + "\n")


# ── The refusal a client gets while igni is not bound ─────────────


def test_the_refusal_carries_the_code_a_client_branches_on():
    refusal = UnboundRefusal.answer(msg.RPCRequest(id="r1", method="get_status", session_id="s"))
    assert isinstance(refusal, msg.RPCResponse)
    assert refusal.code is msg.RpcErrorCode.UNAVAILABLE, "retry shortly: no producer yet"
    assert refusal.error == UnboundRefusal.REASON


def test_a_message_that_is_not_a_call_is_refused_too():
    refusal = UnboundRefusal.answer(msg.UserMessage(id="m1", text="hi"))
    assert isinstance(refusal, msg.Error)
    assert refusal.text == UnboundRefusal.REASON


async def test_a_refusal_that_cannot_be_sent_does_not_stop_the_others():
    class Transport:
        def __init__(self) -> None:
            self.sent: list[msg.Message] = []

        async def receive(self):
            yield msg.RPCRequest(id="r1", method="get_status", session_id="s")
            yield msg.RPCRequest(id="r2", method="get_status", session_id="s")

        async def send(self, message: msg.Message) -> None:
            self.sent.append(message)
            if message.id == "r1":
                raise RuntimeError("the window is gone")

    transport = Transport()
    await UnboundRefusal(transport).run()
    assert [message.id for message in transport.sent] == ["r1", "r2"], "it keeps going"


# ── Whose token the VM holds ──────────────────────────────────────


def test_nobody_signed_in_names_no_tenant(cloud):
    assert TokenTenant()() is None, "a producer's VM may be made before anybody signs in"


def test_a_persons_token_names_the_person(cloud, monkeypatch):
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, ADA_TOKEN)
    assert TokenTenant()() == Tenant(org=ORG, producer=PRODUCER)


def test_an_apps_token_says_the_token_is_an_apps(cloud, monkeypatch):
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, _jwt(sub="app-1", org=ORG, kind="app"))
    assert TokenTenant()() == Tenant(org=ORG, producer="app:app-1")


def test_a_spaces_token_says_the_token_is_a_spaces(cloud, monkeypatch):
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, _jwt(sub="space-1", org=ORG, kind="space"))
    assert TokenTenant()() == Tenant(org=ORG, producer="space:space-1")


def test_a_token_with_no_organisation_names_no_tenant(cloud, monkeypatch):
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, _jwt(sub="u-ada"))
    assert TokenTenant()() is None


# ── What the file says while igni waits ───────────────────────────


def test_a_producer_env_that_cannot_be_read_says_so(cloud, tmp_path: Path):
    path = tmp_path / "producer.env"
    path.mkdir()  # a directory where the file should be
    attempt = ProducerBinding(path, environ={}).attempt()
    assert not attempt.bound
    assert "to be readable" in attempt.waiting_for, "not the same as not being there yet"


def test_a_producer_env_naming_an_unknown_kind_is_refused_by_name(cloud, tmp_path: Path):
    path = tmp_path / "producer.env"
    path.write_text(
        f"IGNI_DATABASE_URL=sqlite:///x.db\nIGNI_ORG={ORG}\nIGNI_PRODUCER=robot:x\n"
        f"IGNI_PRODUCER_INCARNATION={INCARNATION}\n"
    )
    attempt = ProducerBinding(path, environ={}).attempt()
    assert not attempt.bound
    assert "to name a producer" in attempt.waiting_for
    assert "robot" in attempt.waiting_for, "it says which kind it does not know"


def test_a_producer_env_naming_no_incarantion_waits_for_one(cloud, tmp_path: Path):
    path = tmp_path / "producer.env"
    path.write_text(f"IGNI_DATABASE_URL=sqlite:///x.db\nIGNI_ORG={ORG}\nIGNI_PRODUCER={PRODUCER}\n")
    attempt = ProducerBinding(path, environ={}).attempt()
    assert not attempt.bound
    assert "IGNI_PRODUCER_INCARNATION" in attempt.waiting_for


def test_the_values_are_taken_as_the_file_writes_them():
    env = ProducerEnv.parse(
        'IGNI_DATABASE_URL="postgresql://r:p@db/igni"\n'  # double quotes
        "IGNI_ORG='acme'\n"  # single quotes
        'IGNI_QUOTE="\n'  # a value that is one quote character
        'IGNI_EMPTY=""\n'  # a quoted empty value
        "IGNI_EMAIL=\n"  # an empty value
        "IGNI_PRODUCER=person:u=ada\n"  # a value holding '='
        "IGNI_PRODUCER_OWNER=app:a-1\n"
        "PATH=/evil\n"  # not igni's own
    )
    assert env.values["IGNI_DATABASE_URL"] == "postgresql://r:p@db/igni"
    assert env.values["IGNI_ORG"] == "acme"
    assert env.values["IGNI_QUOTE"] == '"', "one quote character is kept whole"
    assert env.values["IGNI_EMPTY"] == "", "one layer of quotes is stripped"
    assert env.values["IGNI_EMAIL"] == "", "an empty value is kept as the empty string"
    assert env.values["IGNI_PRODUCER"] == "person:u=ada", "kept whole after the first '='"
    assert "PATH" not in env.values, "not igni's own variable"
    assert env.owner == "app:a-1"
    assert env.org == "acme"
    assert env.incarnation == "", "the file does not name one"


def test_a_producer_env_naming_nothing_has_no_owner():
    env = ProducerEnv.parse("IGNI_ORG=acme\n")
    assert env.owner is None, "a space's app: none here"
    assert env.producer == ""
    assert env.database_url == ""
    assert env.identity() == ("", "acme", "", "")


def test_the_environment_is_put_back_after_the_token_is_read(cloud, tmp_path: Path, monkeypatch):
    token = tmp_path / "token"
    token.write_text(ADA_TOKEN)
    path = tmp_path / "producer.env"
    _producer_env(path, token=token)
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_FILE_ENV, str(tmp_path / "before"))
    ProducerBinding(path, environ={}).attempt()
    assert os.environ[EnvironmentCredentials.TOKEN_FILE_ENV] == str(tmp_path / "before"), (
        "the process's own variable is put back"
    )


def test_a_variable_that_was_not_set_is_not_left_behind(cloud, tmp_path: Path, monkeypatch):
    token = tmp_path / "token"
    token.write_text(ADA_TOKEN)
    path = tmp_path / "producer.env"
    _producer_env(path, token=token)
    monkeypatch.delenv(EnvironmentCredentials.TOKEN_FILE_ENV, raising=False)
    ProducerBinding(path, environ={}).attempt()
    assert EnvironmentCredentials.TOKEN_FILE_ENV not in os.environ


# ── Binding, and the token that comes late ────────────────────────


async def test_a_token_that_comes_after_the_file_still_binds(cloud, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ProducerBinding, "RELOOK_SECONDS", 0.05)
    token = tmp_path / "token"
    token.write_text(EVE_TOKEN)
    path = tmp_path / "producer.env"
    _producer_env(path, token=token)
    binding = ProducerBinding(path, environ={}, poll_seconds=0.01)
    waiting = asyncio.create_task(binding.bind())
    try:
        await asyncio.sleep(0.1)
        assert not waiting.done(), "the token is another producer's: not bound yet"
        token.write_text(ADA_TOKEN)
        bound = await asyncio.wait_for(waiting, timeout=5)
        assert bound.database_url == "sqlite:///x.db"
        assert binding.tenant == Tenant(org=ORG, producer=STORED)
    finally:
        waiting.cancel()
        await binding.stop()


async def test_a_binding_that_is_cancelled_leaves_nothing_behind(cloud, tmp_path: Path):
    binding = ProducerBinding(tmp_path / "producer.env", environ={}, poll_seconds=0.01)
    waiting = asyncio.create_task(binding.bind())
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert binding.bound is None
    assert binding.tenant is None
    await binding.stop()


async def test_a_binding_that_is_cancelled_stops_its_refusal(cloud, tmp_path: Path):
    class Transport:
        async def receive(self):
            await asyncio.sleep(3600)
            yield  # pragma: no cover — the refusal is cancelled first

        async def send(self, _message: msg.Message) -> None:  # pragma: no cover
            return

    binding = ProducerBinding(tmp_path / "producer.env", environ={}, poll_seconds=0.01)
    waiting = asyncio.create_task(binding.bind(Transport()))
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    refusals = [task for task in asyncio.all_tasks() if task.get_name() == "unbound-refusal"]
    for _ in range(100):
        if not refusals:
            break
        await asyncio.sleep(0.01)
    assert refusals == [], "the refusal is cancelled and awaited with it"
    await binding.stop()


async def test_stopping_before_binding_is_harmless(tmp_path: Path):
    binding = ProducerBinding(tmp_path / "producer.env", environ={})
    await binding.stop()
    assert binding.bound is None


# ── After binding: the file that comes to name another producer ───


async def test_a_producer_env_that_is_removed_is_waited_for(cloud, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ProducerBinding, "RELOOK_SECONDS", 0.05)
    path = tmp_path / "producer.env"
    _producer_env(path)
    binding = ProducerBinding(path, environ={}, poll_seconds=0.01)
    await asyncio.wait_for(binding.bind(), timeout=5)
    changed: list[ProducerEnv] = []
    binding.watch(changed.append)
    try:
        path.unlink()
        await asyncio.sleep(0.3)
        assert changed == [], "removed: the next write says what it is"
        _producer_env(path, database="sqlite:///other.db")
        for _ in range(300):
            if changed:
                break
            await asyncio.sleep(0.01)
        assert [env.database_url for env in changed] == ["sqlite:///other.db"], (
            "the watch is still watching"
        )
    finally:
        await binding.stop()


async def test_a_producer_env_rewritten_as_it_was_puts_its_values_back(
    cloud, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(ProducerBinding, "RELOOK_SECONDS", 0.05)
    path = tmp_path / "producer.env"
    _producer_env(path)
    environ: dict[str, str] = {}
    binding = ProducerBinding(path, environ=environ, poll_seconds=0.01)
    await asyncio.wait_for(binding.bind(), timeout=5)
    changed: list[ProducerEnv] = []
    binding.watch(changed.append)
    try:
        environ.pop(ProducerEnv.ORG_ENV, None)
        # The node agent writes the file the atomic way: a new file, renamed.
        temp = path.with_suffix(".tmp")
        temp.write_text(path.read_text())
        os.replace(temp, path)
        for _ in range(300):
            if environ.get(ProducerEnv.ORG_ENV):
                break
            await asyncio.sleep(0.01)
        assert environ[ProducerEnv.ORG_ENV] == ORG, "the file's values are put back"
        assert changed == [], "rewritten as it was: nothing to say"
    finally:
        await binding.stop()
