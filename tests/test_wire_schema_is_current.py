"""The committed wire schema is what the generator produces.

The generator had been writing an empty schema. Its rule for "is this
a protocol message" was ``cls.__module__ == protocol.messages``, and
the schemas had since moved into ``protocol/schemas/*`` with
``messages.py`` left as a re-export shim — so every class failed the
test and the output contained **zero messages**.

Nothing caught it, because nothing ran it. The snapshot the frontend
contract-tests against was hand-edited from then on, and by the time
anyone looked it was missing ``event_seq`` from most messages: the
frontend was checking its reads against a picture of the protocol
that had stopped being true.

So the generator runs here, on every suite, and the file has to match.
"""

from __future__ import annotations

import json

from igni_core.contracts.wire_schema import SNAPSHOT_PATH, WireSchemaPublisher
from igni_core.protocol.registry import MessageRegistry

REGENERATE = "uv run python scripts/dump_wire_schema.py"


def test_the_snapshot_matches_the_protocol():
    """The whole point. A field added, renamed or removed on a message
    fails here until the snapshot is regenerated."""
    publisher = WireSchemaPublisher()
    assert SNAPSHOT_PATH.read_text() == publisher.render(), (
        f"{SNAPSHOT_PATH.name} is out of date with the protocol. Run:\n    {REGENERATE}"
    )


def test_the_generator_finds_messages_at_all():
    """The failure that actually happened, asserted directly.

    The schema-matches test above would have passed happily against an
    empty snapshot and an empty generator — both sides wrong in the
    same way is exactly how this stayed broken.
    """
    schema = WireSchemaPublisher().build()

    assert len(schema.messages) > 40, (
        "the generator found almost no messages — its idea of what counts "
        "as a protocol message has probably drifted from the code again"
    )


def test_the_generator_agrees_with_the_transport():
    """Everything the wire can carry is in the schema.

    ``MessageRegistry`` is what the transports consult to turn a
    ``type`` string into a class. A message it accepts but the schema
    omits is a message the frontend can receive and no test can check.
    """
    schema = WireSchemaPublisher().build()
    missing = set(MessageRegistry().known_types()) - set(schema.messages)

    assert not missing, f"the transport accepts these but the schema omits them: {sorted(missing)}"


def test_the_snapshot_is_valid_json_with_both_halves():
    """Guards the file itself. It has been truncated by a stray shell
    redirect before now — the generator writes the file and prints a
    log line, so `> wire-schema.json` captures the log line and throws
    the schema away."""
    data = json.loads(SNAPSHOT_PATH.read_text())

    assert data["messages"]
    assert data["rpc"]


def test_the_envelope_bases_are_published_but_are_not_on_the_wire():
    """The two shapes every message inherits, and nothing else.

    They are published under their class names — they carry no wire
    ``type`` of their own — so the contract documents what a message
    looks like. They are not on the wire, which is what
    :attr:`~igni_core.contracts.wire_schema.WireSchema.wire_messages`
    says: a contract carrying only those two is a contract whose
    selector matched nothing.
    """
    schema = WireSchemaPublisher().build()

    assert {"Message", "RunScopedMessage"} <= set(schema.messages)
    assert not {"Message", "RunScopedMessage"} & set(schema.wire_messages)
    assert len(schema.wire_messages) == len(schema.messages) - 2


def test_the_publisher_writes_what_it_renders(tmp_path):
    """The file on disk is the contract, not a copy of one."""
    publisher = WireSchemaPublisher()
    target = tmp_path / "wire-schema.json"

    written, schema = publisher.write(target)

    assert written == target
    assert target.read_text() == publisher.render(schema)
    assert json.loads(target.read_text()) == schema.model_dump()
