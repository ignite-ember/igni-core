"""The wire contract's two builders: the union over a list, and a class's wire type."""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import TypeAdapter, ValidationError

from igni_core.protocol.schemas import wire
from igni_core.protocol.schemas.be_events import ContentDelta
from igni_core.protocol.schemas.envelope import Message
from igni_core.protocol.schemas.mirroring import Welcome
from igni_core.protocol.schemas.wire import (
    WIRE_MESSAGE_TYPES,
    WireMessage,
    wire_type_of,
    wire_union,
)


class _NoType(Message):
    pass


class _TwoTypes(Message):
    type: Literal["a", "b"] = "a"


class _NotAString(Message):
    type: Literal[1] = 1  # type: ignore[assignment]


class _PlainStr(Message):
    type: str = "plain"


class TestWireTypeOf:
    def test_a_class_reads_its_single_literal(self):
        assert wire_type_of(Welcome) == "welcome"
        assert wire_type_of(ContentDelta) == "content_delta"

    @pytest.mark.parametrize("cls", [_NoType, _TwoTypes, _NotAString, _PlainStr])
    def test_a_class_without_a_single_string_literal_is_refused(self, cls):
        with pytest.raises(ValueError, match=f"{cls.__name__} needs a single Literal"):
            wire_type_of(cls)


class TestWireUnion:
    def test_an_empty_list_is_refused(self):
        with pytest.raises(ValueError, match="at least one message type"):
            wire_union([])

    def test_a_subset_accepts_its_members_and_refuses_the_rest(self):
        adapter = TypeAdapter(wire_union([Welcome]))
        assert isinstance(adapter.validate_python({"type": "welcome"}), Welcome)
        with pytest.raises(ValidationError):
            adapter.validate_python({"type": "content_delta", "text": "hi"})

    def test_an_iterator_is_read_once(self):
        adapter = TypeAdapter(wire_union(iter([Welcome, ContentDelta])))
        assert isinstance(adapter.validate_python({"type": "content_delta"}), ContentDelta)
        assert isinstance(adapter.validate_python({"type": "welcome"}), Welcome)

    def test_the_default_is_the_whole_contract(self):
        adapter = TypeAdapter(wire_union())
        for cls in WIRE_MESSAGE_TYPES:
            assert type(adapter.validate_python(cls().model_dump(mode="json"))) is cls


class TestWireMessage:
    @pytest.mark.parametrize("frame", [{"type": "no_such_message"}, {}, {"type": 7}])
    def test_an_unknown_or_missing_type_is_refused(self, frame):
        with pytest.raises(ValidationError):
            TypeAdapter(WireMessage).validate_python(frame)

    def test_a_known_type_with_a_bad_body_is_refused(self):
        with pytest.raises(ValidationError):
            TypeAdapter(WireMessage).validate_python({"type": "content_delta", "text": 5})

    def test_the_module_exports_exactly_its_four_names(self):
        assert sorted(wire.__all__) == [
            "WIRE_MESSAGE_TYPES",
            "WireMessage",
            "wire_type_of",
            "wire_union",
        ]
