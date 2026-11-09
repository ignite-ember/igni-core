"""igni-client speaks the protocol igni speaks — held here, where the protocol lives.

igni-sdk's `packages/igni-client` mirrors igni's wire protocol by hand: it cannot
import `igni_core` (a client must not install igni to talk to it). A
mirror drifts the day a field is renamed on one side only, and the SDK's
own tests, run against its own fake, would not notice. So every claim the
client makes about the wire is checked against the real thing:

* each message it models has igni's `type` and exactly igni's fields;
* each method it calls exists, and so does each it refuses to send;
* every frame it sends is one igni accepts, with no field igni drops;
* the handshake, the lockfile it reads and the "not bound yet" answer it
  retries on are igni's.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

from igni_core.backend.schemas_lockfile import LockfilePayload
from igni_core.protocol import messages as msg_module
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.envelope import Message
from igni_core.transport import ws_auth
from tests.siblings import IGNI_SDK, needs_sdk, skip_module_without_sdk

ROOT = Path(__file__).resolve().parent.parent
CLIENT_SRC = IGNI_SDK / "packages" / "igni-client" / "src"
skip_module_without_sdk()
pytestmark = needs_sdk
sys.path.insert(0, str(CLIENT_SRC))

from igni_client import ApprovalRequested, IgniClient  # noqa: E402 — a package of its own
from igni_client import connection as client_connection  # noqa: E402
from igni_client import targets as client_targets  # noqa: E402
from igni_client.events import EVENT_TYPES  # noqa: E402
from igni_client.testing import Delay, FakeIgni, Steps  # noqa: E402


def _igni_messages() -> dict[str, type[Message]]:
    """Every message igni's transport accepts or sends, by wire `type`."""
    found: dict[str, type[Message]] = {}
    for name in dir(msg_module):
        cls = getattr(msg_module, name)
        if isinstance(cls, type) and issubclass(cls, Message) and cls is not Message:
            default = cls.model_fields.get("type")
            if default is not None and isinstance(default.default, str):
                found[default.default] = cls
    return found


IGNI = _igni_messages()


@pytest.mark.parametrize("wire_type", sorted(EVENT_TYPES))
def test_each_message_the_client_models_is_igni_s_field_for_field(wire_type: str) -> None:
    assert wire_type in IGNI, f"igni sends no message of type {wire_type!r}"
    client_fields = set(EVENT_TYPES[wire_type].model_fields)
    igni_fields = set(IGNI[wire_type].model_fields)
    assert client_fields == igni_fields, (
        f"{wire_type}: the client has {sorted(client_fields - igni_fields)} that igni does not, "
        f"and lacks {sorted(igni_fields - client_fields)}"
    )


def test_the_run_events_igni_streams_are_all_modelled() -> None:
    # A run event the client does not model still arrives, as `Unknown` —
    # but `Session.send()` users match on the typed ones, so a new run
    # event should be added to the client, not discovered by a user.
    from igni_core.protocol.schemas import be_events

    run_events = {
        cls.model_fields["type"].default
        for name in be_events.__all__
        if isinstance(cls := getattr(be_events, name), type)
        and issubclass(cls, Message)
        and "type" in cls.model_fields
    }
    assert run_events <= set(EVENT_TYPES), sorted(run_events - set(EVENT_TYPES))


def test_every_method_the_client_calls_exists() -> None:
    called = set()
    for path in (CLIENT_SRC / "igni_client").glob("*.py"):
        called |= set(re.findall(r"\.rpc\(\s*\"([a-z_]+)\"", path.read_text()))
    methods = {m.value for m in RpcMethod}
    assert called, "the scan found no calls"
    assert called <= methods, sorted(called - methods)


def test_what_the_client_refuses_to_send_names_igni_s_own_methods() -> None:
    methods = {m.value for m in RpcMethod}
    assert methods >= client_connection.REFUSED_METHODS


async def test_every_frame_the_client_sends_is_one_igni_accepts() -> None:
    """Drive every feature against the fake, then read each sent frame as igni would."""
    async with FakeIgni() as fake:
        fake.runs.script = lambda text: (
            [Steps.approval("deploy"), *Steps.answer("done")]
            if text == "deploy"
            else [Delay(seconds=30)]
            if text == "long"
            else Steps.answer("hi")
        )
        async with IgniClient(fake.local()) as igni:
            session = await igni.sessions.create(project_dir="/workspace")
            await igni.sessions.open(session.id)
            await igni.sessions.list()
            await igni.sessions.default()
            await igni.models.list()
            await session.use_model("igni-1")
            await session.status()
            await session.history()
            await session.ask("hello")
            async for event in session.send("deploy", files={"a.py": "x"}):
                if isinstance(event, ApprovalRequested):
                    await event.approve()
            await session.queue("later")
            follow = asyncio.ensure_future(session.ask("long"))
            await asyncio.sleep(0.05)
            await session.cancel()
            await asyncio.wait_for(follow, 5)

    sent = [r.frame for r in fake.socket.received]
    kinds = {frame.type for frame in sent}
    assert {
        "rpc_request",
        "user_message",
        "hitl_response_batch",
        "queue_message",
        "cancel",
    } <= kinds
    for frame in sent:
        cls = IGNI.get(frame.type)
        assert cls is not None, f"igni does not accept {frame.type!r}"
        wire = frame.model_dump(exclude_unset=True)
        unknown = set(wire) - set(cls.model_fields)
        assert not unknown, f"{frame.type}: igni drops {sorted(unknown)}"
        cls.model_validate(wire)
        if frame.type == "hitl_response_batch":
            for decision in wire["decisions"]:
                decision_cls = IGNI["hitl_decision"]
                assert not set(decision) - set(decision_cls.model_fields)
                decision_cls.model_validate(decision)


def test_the_handshake_is_igni_s() -> None:
    assert client_targets.SUBPROTOCOL == ws_auth.SUBPROTOCOL
    assert client_targets.TOKEN_PROTOCOL_PREFIX == ws_auth.TOKEN_PROTOCOL_PREFIX


def test_the_lockfile_the_client_reads_is_igni_s() -> None:
    assert {"port", "ws_token"} <= set(LockfilePayload.model_fields)
    source = (CLIENT_SRC / "igni_client" / "targets.py").read_text()
    assert '".igni" / "backend.lock"' in source


def test_the_not_bound_answer_the_client_retries_on_is_igni_cloud_s() -> None:
    binding = ROOT / "packages" / "igni-cloud" / "src" / "igni_cloud" / "guest" / "binding.py"
    assert json.dumps(client_connection.UNBOUND) in binding.read_text()
