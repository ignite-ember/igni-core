"""The wire registry: what arrives, what it rejects, and what it knows.

Three things are asserted here.

* The wire contract is *stated*. Every message class in the schemas
  package is named in
  :data:`igni_core.protocol.schemas.wire.WIRE_MESSAGE_TYPES`, so a
  message that reaches a schema module without joining the list fails
  here instead of silently never arriving.
* The registry is a value, not a process singleton. Two registries are
  two objects, and one built for a subset of the contract accepts only
  that subset — the old class-level ``_default`` was a process-wide
  instance every transport shared.
* A frame that is not a message is *skipped*, never raised on. The
  transports treat ``None`` as "ignore this frame", so anything that
  escapes :meth:`MessageRegistry.deserialize` kills the read loop —
  which is exactly what a JSON array on the wire used to do.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest
from pydantic import TypeAdapter

from igni_core.protocol import schemas
from igni_core.protocol.registry import MessageRegistry
from igni_core.protocol.schemas.be_events import ContentDelta
from igni_core.protocol.schemas.envelope import Message, RunScopedMessage
from igni_core.protocol.schemas.mirroring import Welcome
from igni_core.protocol.schemas.wire import (
    WIRE_MESSAGE_TYPES,
    WireMessage,
    wire_type_of,
)


def _message_classes_in_the_schemas_package() -> set[type[Message]]:
    """Every concrete :class:`Message` subclass the schemas package defines."""
    found: set[type[Message]] = set()
    for info in pkgutil.iter_modules(schemas.__path__, prefix="igni_core.protocol.schemas."):
        module = importlib.import_module(info.name)
        for _name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != info.name:
                continue
            if issubclass(cls, Message) and cls not in (Message, RunScopedMessage):
                found.add(cls)
    return found


class TestTheWireContract:
    """The list of message classes is the contract — it has to be complete."""

    def test_every_message_class_is_named_in_the_contract(self):
        missing = _message_classes_in_the_schemas_package() - set(WIRE_MESSAGE_TYPES)
        assert not missing, (
            "these message classes are not on the wire contract, so the "
            f"deserializer would reject them: {sorted(c.__name__ for c in missing)}"
        )

    def test_the_contract_names_only_message_classes(self):
        assert all(issubclass(cls, Message) for cls in WIRE_MESSAGE_TYPES)

    def test_every_wire_type_is_distinct(self):
        types = [wire_type_of(cls) for cls in WIRE_MESSAGE_TYPES]
        assert len(set(types)) == len(types), "two classes claim the same wire type"

    def test_the_registry_knows_every_wire_type(self):
        assert MessageRegistry().known_types() == sorted(
            wire_type_of(cls) for cls in WIRE_MESSAGE_TYPES
        )

    def test_the_contract_union_accepts_every_message(self):
        """The union the deserializer validates against covers the list."""
        adapter = TypeAdapter(WireMessage)
        for cls in WIRE_MESSAGE_TYPES:
            frame = cls().model_dump(mode="json")
            restored = adapter.validate_python(frame)
            assert isinstance(restored, cls), f"{cls.__name__} did not survive the union"


class TestTheRegistryIsAValue:
    """No process-wide instance: what a transport holds is what it got."""

    def test_the_class_holds_no_registry_instance(self):
        """No class attribute may hold the process-wide registry.

        That is the classvar-used-as-singleton this class used to carry
        (``_default``, built on first use and shared by every
        transport). An attribute typed as a registry counts even before
        it holds one.
        """
        annotations = getattr(MessageRegistry, "__annotations__", {})
        offenders = [
            name
            for name, value in vars(MessageRegistry).items()
            if isinstance(value, MessageRegistry)
            or "MessageRegistry" in str(annotations.get(name, ""))
        ]
        assert not offenders, f"the registry keeps process-wide state on {offenders}"

    def test_two_registries_are_two_objects(self):
        assert MessageRegistry() is not MessageRegistry()

    def test_a_registry_can_be_built_for_a_subset(self):
        registry = MessageRegistry([Welcome])
        assert registry.known_types() == ["welcome"]
        assert registry.deserialize('{"type": "welcome"}') is not None
        assert registry.deserialize('{"type": "content_delta", "text": "hi"}') is None


class TestDeserialize:
    """What the transports hand the read loop."""

    def test_a_message_frame_comes_back_as_its_class(self):
        restored = MessageRegistry().deserialize(
            '{"type": "content_delta", "text": "hello", "is_thinking": false}'
        )
        assert isinstance(restored, ContentDelta)
        assert restored.text == "hello"

    @pytest.mark.parametrize(
        "frame",
        [
            # Not JSON at all.
            "not json at all",
            "",
            # Valid JSON, but not a frame.
            "[1, 2, 3]",
            '"a string"',
            "42",
            "null",
            "true",
            # A frame with no usable discriminator.
            '{"no_type": 1}',
            '{"type": 42}',
            '{"type": ["not", "a", "string"]}',
            # A frame whose payload does not fit the class its type names.
            '{"type": "content_delta", "text": 5}',
            # A frame whose type nobody knows.
            '{"type": "unknown_type", "payload": {}}',
        ],
    )
    def test_a_frame_that_is_not_a_message_is_skipped(self, frame):
        """The read loop must survive anything the peer sends."""
        assert MessageRegistry().deserialize(frame) is None
