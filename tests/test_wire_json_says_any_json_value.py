"""A field of any JSON value is published as that, not as ``{}``."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from igni_core.protocol.json_value import JSON_OBJECT_EXTRA, JSON_VALUE_SCHEMA, WireJson


class _Holder(BaseModel):
    value: WireJson = None
    bag: dict[str, WireJson] = {}


def test_the_schema_names_every_json_kind():
    assert JSON_VALUE_SCHEMA["type"] == [
        "object",
        "array",
        "string",
        "number",
        "integer",
        "boolean",
        "null",
    ]
    assert JSON_VALUE_SCHEMA["description"] == "Any JSON value."


def test_an_object_field_is_a_map_to_any_json_value():
    assert JSON_OBJECT_EXTRA == {"additionalProperties": JSON_VALUE_SCHEMA}


def test_a_field_is_published_as_any_json_value_not_as_nothing():
    props = _Holder.model_json_schema()["properties"]
    assert props["value"]["description"] == "Any JSON value."
    assert props["value"]["type"] == JSON_VALUE_SCHEMA["type"]
    assert props["value"] != {}
    assert props["bag"]["additionalProperties"] == {**JSON_VALUE_SCHEMA}


def test_a_field_accepts_whatever_it_is_given_and_hands_it_back():
    for given in [1, "s", None, [1, {"a": 2}], {"k": [True]}, 2.5]:
        assert _Holder(value=given).value == given


def test_a_non_json_value_is_not_refused_and_is_written_as_a_string():
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    holder = _Holder(value=when)
    assert holder.value == when
    assert holder.model_dump(mode="json")["value"] == "2026-01-02T03:04:05Z"
