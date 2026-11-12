"""``PUSH_PAYLOADS`` is the table the published contract says each push carries.

``test_rpc_contract_is_current`` holds the committed file to the table;
this holds the table to the channels and to what the bridge really sends:
every channel has a payload model, what the bridge dumps validates against
the model its channel names, and the orchestrate union tells its events
apart by ``type`` and refuses one it does not know.
"""

from __future__ import annotations

import typing
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.push_payloads import PUSH_PAYLOADS, PushPayload
from igni_core.core.tools.orchestrate_events import ORCHESTRATE_EVENT_ADAPTER
from igni_core.core.tools.orchestrate_events import OrchestrateEvent as ToolsOrchestrateEvent
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.push import PushNotification


def _members(union: Any) -> list[type[BaseModel]]:
    return list(typing.get_args(typing.get_args(union)[0]))


def _minimal(model: type[BaseModel]) -> BaseModel:
    """An instance of ``model`` with each required string field filled."""
    return model(**{n: "x" for n, f in model.model_fields.items() if f.is_required()})


ORCHESTRATE = PUSH_PAYLOADS[PushChannel.ORCHESTRATE_EVENT]
TOOL_EVENTS = _members(ToolsOrchestrateEvent)


class _Bridge:
    def __init__(self) -> None:
        self.pushed: list[PushNotification] = []
        self.bridge = PushNotificationBridge(transport=None, loop=None, queue=[])  # type: ignore[arg-type]
        self.bridge._schedule_push = self.pushed.append  # type: ignore[method-assign]


class TestTheTable:
    @pytest.mark.parametrize("channel", [c for c in PushChannel if c is not PushChannel.UNKNOWN])
    def test_every_channel_has_a_payload_and_a_sentence_about_it(self, channel: PushChannel):
        entry = PUSH_PAYLOADS[channel]

        assert isinstance(entry, PushPayload)
        assert entry.doc.strip().endswith(".")
        assert entry.mode in ("serialization", "validation")

    def test_a_channel_nobody_recognises_has_no_payload(self):
        assert PushChannel.UNKNOWN not in PUSH_PAYLOADS

    def test_only_the_orchestrate_channel_is_dumped_without_its_nones(self):
        assert {c for c, p in PUSH_PAYLOADS.items() if p.mode == "validation"} == {
            PushChannel.ORCHESTRATE_EVENT
        }


class TestWhatTheBridgeSendsFitsItsChannel:
    @pytest.mark.parametrize("event_type", TOOL_EVENTS, ids=lambda t: t.__name__)
    def test_an_orchestrate_event_dumped_without_its_nones_validates_and_keeps_its_type(
        self, event_type: type[BaseModel]
    ):
        event = _minimal(event_type)
        sent = _Bridge()

        sent.bridge.on_orchestrate_progress(event)  # type: ignore[arg-type]

        [push] = sent.pushed
        assert push.channel == PushChannel.ORCHESTRATE_EVENT
        parsed = TypeAdapter(ORCHESTRATE.model).validate_python(push.payload)
        assert parsed.type == event.type  # type: ignore[attr-defined]
        assert push.payload["type"] == event.type  # type: ignore[attr-defined]
        assert None not in push.payload.values()

    def test_the_file_edit_payload_validates_against_its_channel(self):
        sent = _Bridge()

        sent.bridge._on_file_edited("/p/a.py")

        [push] = sent.pushed
        PUSH_PAYLOADS[push.channel].model.model_validate(push.payload)  # type: ignore[arg-type]


class TestTheOrchestrateUnion:
    def test_the_type_picks_the_variant(self):
        adapter = TypeAdapter(ORCHESTRATE.model)

        started = adapter.validate_python({"type": "tool_started", "agent_path": "a", "tool": "t"})
        err = adapter.validate_python({"type": "run_error", "agent_path": "a", "error": "boom"})

        assert (type(started).__name__, type(err).__name__) == ("ToolStartedEvent", "RunErrorEvent")

    def test_an_unknown_type_is_refused(self):
        with pytest.raises(ValidationError, match="union_tag_invalid|does not match"):
            TypeAdapter(ORCHESTRATE.model).validate_python({"type": "nope", "agent_path": "a"})

    def test_a_payload_without_a_type_is_refused(self):
        with pytest.raises(ValidationError):
            TypeAdapter(ORCHESTRATE.model).validate_python({"agent_path": "a"})

    def test_a_visualization_delta_is_one_of_its_variants(self):
        names = {m.__name__ for m in _members(ORCHESTRATE.model)}

        assert "VisualizationDeltaPayload" in names
        assert "ToolStartedEvent" in names

    def test_the_reader_side_adapter_parses_what_the_bridge_dumped(self):
        sent = _Bridge()
        sent.bridge.on_orchestrate_progress(_minimal(TOOL_EVENTS[0]))  # type: ignore[arg-type]

        [push] = sent.pushed
        event = ORCHESTRATE_EVENT_ADAPTER.validate_python(push.payload)

        assert type(event) is TOOL_EVENTS[0]
