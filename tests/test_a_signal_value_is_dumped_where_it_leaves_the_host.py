"""``plain``, ``Signal`` and ``Emitter``: a value becomes plain JSON data once, at the seam.

The bus hands values to its sinks as they are, so a model that reached it
undumped would reach a page or a socket as an object.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ValidationError

from igni_core.extensions.host.plain import plain
from igni_core.extensions.host.signal_values import Emitter, Signal, Windowed


class Colour(enum.Enum):
    RED = "red"


class Leaf(BaseModel):
    at: datetime
    colour: Colour


class Tree(BaseModel):
    name: str
    leaves: list[Leaf] = []


LEAF = Leaf(at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), colour=Colour.RED)
LEAF_PLAIN = {"at": "2026-01-02T03:04:05Z", "colour": "red"}


def test_a_model_is_dumped_in_json_mode() -> None:
    assert plain(LEAF) == LEAF_PLAIN


def test_models_inside_lists_and_dicts_are_dumped_however_deep() -> None:
    value = {"trees": [Tree(name="oak", leaves=[LEAF])], "by": {"first": [LEAF]}, "n": 3}

    assert plain(value) == {
        "trees": [{"name": "oak", "leaves": [LEAF_PLAIN]}],
        "by": {"first": [LEAF_PLAIN]},
        "n": 3,
    }


@pytest.mark.parametrize("value", [None, 7, "text", 1.5, True, [], {}, [1, "a", None]])
def test_plain_data_passes_through_unchanged(value: Any) -> None:
    assert plain(value) == value


def test_a_tuple_is_not_looked_into() -> None:
    inside = (LEAF,)

    assert plain(inside) is inside


class Moved(Signal):
    WIRE: ClassVar[str] = "thing.moved"

    thing_id: str
    where: str


class MovedWindow(Windowed):
    WIRE: ClassVar[str] = "thing.window_moved"

    thing_id: str


class Message:
    thing_id = "t-1"
    where = "up"
    window = "w-1"


def test_a_signal_is_built_from_the_attributes_an_object_carries() -> None:
    assert Moved.of(Message()).model_dump() == {"thing_id": "t-1", "where": "up"}


def test_a_signal_is_built_from_another_models_attributes() -> None:
    class Aliased(Signal):
        WIRE: ClassVar[str] = "thing.aliased"

        thing_id: str
        where: str

    class Aliasing(BaseModel):
        thing_id: str = "t-9"
        where: str = "down"

    assert Aliased.of(Aliasing()).where == "down"


def test_a_message_missing_a_field_does_not_make_a_signal() -> None:
    class Half:
        thing_id = "t-1"

    with pytest.raises(ValidationError):
        Moved.of(Half())


def test_a_windowed_signal_has_no_window_until_it_is_given_one() -> None:
    assert MovedWindow(thing_id="t-1").window is None


class Recorded:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    def __call__(self, wire: str, value: Any = None, **kwargs: Any) -> list[str]:
        self.calls.append((wire, value, kwargs))
        return []


def test_a_signal_is_sent_on_its_wire_with_its_fields_dumped() -> None:
    emit = Recorded()

    Emitter(emit).send(Moved(thing_id="t-1", where="up"))

    assert emit.calls == [
        (
            "thing.moved",
            {"thing_id": "t-1", "where": "up"},
            {"content": None, "only": None, "window": None},
        )
    ]


def test_the_content_filter_and_the_only_target_travel_with_the_signal() -> None:
    emit = Recorded()

    def content(grants: Any) -> Any:
        return None

    Emitter(emit).send(Moved(thing_id="t-1", where="up"), content=content, only="acme.one")

    kwargs = emit.calls[0][2]
    assert kwargs["content"] is content and kwargs["only"] == "acme.one"


def test_only_a_windowed_signal_names_its_window() -> None:
    emit = Recorded()
    emitter = Emitter(emit)

    emitter.send(MovedWindow(thing_id="t-1", window="w-1"))
    emitter.send(MovedWindow(thing_id="t-1"))
    emitter.send(Moved(thing_id="t-1", where="up"))

    assert [call[2]["window"] for call in emit.calls] == ["w-1", None, None]


def test_a_value_sent_by_wire_name_is_dumped_first() -> None:
    emit = Recorded()

    Emitter(emit).value("thing.listed", [Tree(name="oak", leaves=[LEAF])])

    assert emit.calls == [
        ("thing.listed", [{"name": "oak", "leaves": [LEAF_PLAIN]}], {"content": None})
    ]


def test_a_value_sent_by_wire_name_carries_its_content_filter() -> None:
    emit = Recorded()

    def content(grants: Any) -> Any:
        return {}

    Emitter(emit).value("thing.listed", None, content=content)

    assert emit.calls[0][2]["content"] is content


def test_a_signal_that_is_not_windowed_names_no_window_even_with_a_field_so_called() -> None:
    class Looks(Signal):
        WIRE: ClassVar[str] = "thing.looks"

        window: str

    emit = Recorded()

    Emitter(emit).send(Looks(window="w-1"))

    assert emit.calls[0][2]["window"] is None
