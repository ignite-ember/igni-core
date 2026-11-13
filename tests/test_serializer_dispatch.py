"""How the Agno event serializer picks a handler, and what it does with an event nobody wants."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from agno.run.agent import RunContentEvent, RunStartedEvent

from igni_core.protocol import messages as msg
from igni_core.protocol.agno_tool_formatter import AgnoToolEventFormatter
from igni_core.protocol.serializer import AgnoEventSerializer, serialize_event
from igni_core.protocol.tool_error_conventions import ToolResultErrorDetector


class _Says:
    """A handler that takes every event and answers with ``text``."""

    def __init__(self, text: str | None, takes: bool = True) -> None:
        self.text, self.takes = text, takes
        self.seen: list[object] = []

    def matches(self, event: object) -> bool:
        return self.takes

    def build(self, event: object) -> msg.Message | None:
        self.seen.append(event)
        return None if self.text is None else msg.ContentDelta(text=self.text, is_thinking=False)


def _serializer(handlers: list) -> AgnoEventSerializer:
    return AgnoEventSerializer(
        formatter=AgnoToolEventFormatter(),
        error_detector=ToolResultErrorDetector.default(),
        handlers=handlers,
    )


def test_an_event_nobody_wants_is_none_and_is_logged_at_debug(caplog) -> None:
    serializer = _serializer([_Says("x", takes=False)])

    with caplog.at_level(logging.DEBUG, logger="igni_core.protocol.serializer"):
        assert serializer.serialize(object()) is None

    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.DEBUG, "Unserializable Agno event: object")
    ]


def test_no_handlers_means_no_message() -> None:
    assert _serializer([]).serialize(RunContentEvent(content="hi")) is None


def test_the_first_handler_that_matches_answers_and_the_rest_are_not_asked() -> None:
    skipped, first, later = _Says("a", takes=False), _Says("first"), _Says("later")
    event = object()

    result = _serializer([skipped, first, later]).serialize(event)

    assert result == msg.ContentDelta(text="first", is_thinking=False)
    assert first.seen == [event]
    assert skipped.seen == later.seen == []


def test_a_handler_that_matches_and_builds_nothing_ends_the_walk() -> None:
    later = _Says("later")

    assert _serializer([_Says(None), later]).serialize(object()) is None
    assert later.seen == []


def test_the_production_serializer_turns_a_content_event_into_text() -> None:
    result = AgnoEventSerializer.default().serialize(RunContentEvent(content="hello"))

    assert result == msg.ContentDelta(text="hello", is_thinking=False)


def test_a_known_event_with_a_content_attribute_is_answered_by_its_own_handler() -> None:
    """The duck-typed ``.content`` handler is last: it never takes an event
    a typed handler knows. A run start tells the client nothing, so its
    handler answers nothing; the fallback would have shown the text."""
    event = RunStartedEvent(run_id="r1")
    event.content = "not text for the client"  # type: ignore[attr-defined]

    result = AgnoEventSerializer.default().serialize(event)

    assert result is None


def test_an_object_with_only_text_content_is_still_shown() -> None:
    result = AgnoEventSerializer.default().serialize(SimpleNamespace(content="plain"))

    assert result == msg.ContentDelta(text="plain", is_thinking=False)


def test_an_event_with_no_content_and_no_kind_is_dropped() -> None:
    serializer = AgnoEventSerializer.default()

    assert serializer.serialize(None) is None
    assert serializer.serialize(object()) is None
    assert serializer.serialize(SimpleNamespace(content=5)) is None


def test_each_default_serializer_is_its_own_instance() -> None:
    a, b = AgnoEventSerializer.default(), AgnoEventSerializer.default()

    assert a is not b
    assert a._handlers is not b._handlers
    assert a._formatter is not b._formatter


def test_the_free_function_serializes_like_a_default_serializer() -> None:
    assert serialize_event(RunContentEvent(content="x")) == msg.ContentDelta(
        text="x", is_thinking=False
    )
    assert serialize_event(object()) is None
