"""The wire contract igni-core publishes: ``docs/wire-schema.json``.

Every message the wire can carry, and every RPC payload the web client
reads outside a message envelope, as field lists. igni-app keeps a copy
beside the web client (``clients/web/src/protocol/wire-schema.json``)
and tests its reads against that copy; refreshing the copy is how
igni-app takes a new protocol.

The builder used to live in ``scripts/dump_wire_schema.py`` and
selected classes with ``cls.__module__ != messages.__name__``: "was
this defined in protocol/messages.py?". Then the schemas moved into
``protocol/schemas/*`` and ``messages.py`` became a re-export shim.
Every class's ``__module__`` became ``...schemas.be_events`` and the
like, the filter excluded all of them, and the generator produced a
file with **zero messages** in it. Nobody noticed, because nothing ran
it: the snapshot was maintained by hand from then on, which is the
opposite of what a generated file is for. By the time it was looked
at, the committed copy was missing ``event_seq`` from most messages.

It lives in the package now so a test can run it and compare —
``tests/test_wire_schema_is_current.py``. A generator nobody runs
rots; one a test runs cannot.

Membership no longer comes from scanning an import namespace either.
The schema and the transports read the same list —
:data:`igni_core.protocol.schemas.wire.WIRE_MESSAGE_TYPES` — so the
contract the frontend is shown and the contract the wire accepts are
one thing.

**Why this is not under ``protocol/``.** The contract is published from
two halves: the protocol messages, and the backend's RPC models — and
the backend imports the protocol. A builder inside the protocol package
could therefore only reach the backend through function-local imports,
which hid the dependency it has and made ``igni_core.protocol``
unimportable without dragging the whole backend in behind it. The
contract sits above both halves instead: this module imports them at
the top, and nothing in the running product imports it but the scripts
that publish it and the tests that check it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel, Field

from igni_core.backend.schemas_context import PendingMessage
from igni_core.backend.schemas_loop import LoopStatusSnapshot
from igni_core.backend.schemas_mcp import MCPServerSnapshot
from igni_core.core.agents.schemas import AgentInfo
from igni_core.core.scheduler.models import ScheduledTask
from igni_core.notifications.schemas import (
    FloorNotification,
    NotificationCounts,
    NotificationsListResult,
    NotificationTarget,
)
from igni_core.protocol.schemas.envelope import Message, RunScopedMessage
from igni_core.protocol.schemas.wire import WIRE_MESSAGE_TYPES, wire_type_of

#: Where igni-core publishes it. igni-app keeps a copy beside the web
#: client (``clients/web/src/protocol/wire-schema.json``) and tests its
#: reads against that copy; refreshing the copy is how igni-app takes a
#: new protocol.
SNAPSHOT_PATH = Path(__file__).resolve().parents[3] / "docs" / "wire-schema.json"

#: The envelope every message inherits, published under its class name
#: rather than a wire type: neither carries a ``type`` of its own, and
#: together they document the shape the rest of the contract extends.
_ENVELOPE_BASES: tuple[type[Message], ...] = (Message, RunScopedMessage)

_ENVELOPE_BASE_NAMES = frozenset(cls.__name__ for cls in _ENVELOPE_BASES)

#: RPC payloads that reach the FE outside a :class:`Message` envelope,
#: and the model each one is reflected from.
#:
#: These used to be hand-written field lists, on the premise that the
#: producers built "plain dicts … so there is no model to reflect on".
#: Every one of them returns a Pydantic model, and the list had already
#: drifted: ``mcp_server`` was missing ``tools_disabled``, which
#: :class:`~igni_core.backend.schemas_mcp.MCPServerSnapshot` has carried
#: since the per-tool toggle shipped — so the one field the snapshot
#: test existed to protect was the one it silently stopped covering.
#: Reflecting makes that class of drift impossible.
#:
#: Immutable: it is a contract, not a registry something writes to.
RPC_PAYLOAD_MODELS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {
        # backend/server.py::loop_status
        "loop_status": LoopStatusSnapshot,
        # backend/server.py::get_pending_messages
        "pending_message": PendingMessage,
        # backend/server.py::get_mcp_server_details
        "mcp_server": MCPServerSnapshot,
        # backend/rpc_handlers/notifications.py
        "notifications_list": NotificationsListResult,
        "floor_notification": FloorNotification,
        "notification_target": NotificationTarget,
        "notification_counts": NotificationCounts,
        # backend/server.py::get_agent_details
        "agent_info": AgentInfo,
        # backend/server_scheduler.py::list_all
        "scheduled_task": ScheduledTask,
    }
)


class EmptyWireContractError(RuntimeError):
    """The reflected contract came out empty.

    A generation of zero messages is not a protocol that shrank; it is
    an enumeration that stopped matching — and a generator that can
    destroy its own output has to check its output first.
    """


class WireSchema(BaseModel):
    """The published contract: the field names the frontend may read.

    Two halves — the messages that travel on the wire, and the RPC
    payloads that answer off it — each a lookup from a name to the
    fields that name carries.
    """

    messages: dict[str, list[str]] = Field(default_factory=dict)
    rpc: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def wire_messages(self) -> dict[str, list[str]]:
        """The messages that carry a wire ``type`` — the contract proper.

        The envelope bases are in :attr:`messages` too, because they
        document what every message inherits. They are not on the wire,
        so a contract carrying only those two is a contract whose
        selector matched nothing.
        """
        return {
            name: fields
            for name, fields in self.messages.items()
            if name not in _ENVELOPE_BASE_NAMES
        }


class WireSchemaPublisher:
    """Builds the published wire contract, and writes it.

    The contract is reflected, not written by hand: the message side
    from the classes the transports accept off the wire, the RPC side
    from the models the backend answers with. Both lists are injected,
    so a caller can publish a contract for a narrower surface — and a
    test can publish an empty one to prove the refusal works.
    """

    def __init__(
        self,
        message_types: Sequence[type[Message]] = WIRE_MESSAGE_TYPES,
        rpc_payloads: Mapping[str, type[BaseModel]] = RPC_PAYLOAD_MODELS,
    ) -> None:
        self._message_types = tuple(message_types)
        self._rpc_payloads = MappingProxyType(dict(rpc_payloads))

    def build(self) -> WireSchema:
        """The contract, reflected from the protocol and the RPC models.

        The messages are the wire contract —
        :data:`~igni_core.protocol.schemas.wire.WIRE_MESSAGE_TYPES` —
        the same list :class:`~igni_core.protocol.registry.MessageRegistry`
        accepts off the wire, so a message the frontend is shown is a
        message the backend can send and a test can check.
        """
        messages = {cls.__name__: sorted(cls.model_fields) for cls in _ENVELOPE_BASES}
        messages.update(
            {wire_type_of(cls): sorted(cls.model_fields) for cls in self._message_types}
        )
        rpc = {name: sorted(model.model_fields) for name, model in self._rpc_payloads.items()}
        return WireSchema(messages=messages, rpc=rpc)

    def render(self, schema: WireSchema | None = None) -> str:
        """The exact bytes of the snapshot file, for writing or comparing."""
        contract = schema or self.build()
        return json.dumps(contract.model_dump(), indent=2, sort_keys=True) + "\n"

    def write(self, path: Path | None = None) -> tuple[Path, WireSchema]:
        """Write the contract; where it went, and what was written.

        The failure mode this guards is a silent exit 0 and a three-line
        file where the contract was: when ``messages`` became a re-export
        shim the selector matched nothing, and anybody who ran the
        generator destroyed the snapshot the web suite validates against.
        Refusing is the half of that fix that has to live here — a test
        that runs the generator catches a *stale* contract, not an
        *emptied* one, because it regenerates before comparing.
        """
        schema = self.build()
        if not schema.wire_messages or not schema.rpc:
            raise EmptyWireContractError(
                "refusing to write an empty wire contract "
                f"({len(schema.wire_messages)} messages, {len(schema.rpc)} rpc payloads). "
                "The contract list matched nothing — check that "
                "igni_core.protocol.schemas.wire still lists the Message subclasses, "
                "and that the RPC payload catalog still names the models it reflects."
            )
        target = path or SNAPSHOT_PATH
        target.write_text(self.render(schema))
        return target, schema
