"""The schema's type expressions as validators: what fits, what is coerced, what is refused."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from igni_core.extensions.host.spec import HostSchema, PermissionSpec, field, typedef
from igni_core.extensions.host.types import SchemaTypes

SCHEMA = HostSchema(
    version="1.0.0",
    permissions=(PermissionSpec(name="p", doc="P."),),
    types=(
        typedef(
            "Note",
            "A note.",
            field("text", "str"),
            field("count", "int", optional=True),
            field("tags", "list[str]", optional=True),
        ),
        typedef("Box", "A box.", field("note", "Note"), field("extra", "json", optional=True)),
    ),
    objects=(),
)


@pytest.fixture
def types() -> SchemaTypes:
    return SchemaTypes(SCHEMA)


def test_scalars_validate_as_their_python_types(types: SchemaTypes):
    assert types.python_type("str") is str
    assert types.python_type("int") is int
    assert types.python_type("float") is float
    assert types.python_type("bool") is bool
    assert types.python_type("None") is type(None)
    assert types.validate("str", "a") == "a"
    assert types.validate("None", None) is None


def test_a_json_scalar_accepts_anything(types: SchemaTypes):
    for value in (1, "x", None, [1, {"a": 2}], {"k": [None]}):
        assert types.validate("json", value) == value


def test_a_value_is_coerced_loosely_to_the_type(types: SchemaTypes):
    assert types.validate("int", "12") == 12
    assert types.validate("Note", {"text": "x", "count": "3"}).count == 3
    assert types.validate("float", "1.5") == 1.5


def test_lists_dicts_and_optionals_nest(types: SchemaTypes):
    assert types.validate("list[dict[str, int | None]]", [{"a": "1", "b": None}]) == [
        {"a": 1, "b": None}
    ]
    assert types.validate("list[Note] | None", None) is None
    assert types.validate("dict[str, Note]", {"k": {"text": "t"}})["k"].text == "t"


def test_a_none_is_refused_for_a_type_that_is_not_optional(types: SchemaTypes):
    with pytest.raises(ValidationError):
        types.validate("int", None)
    with pytest.raises(ValidationError):
        types.validate("Note", None)
    with pytest.raises(ValidationError):
        types.validate("list[int]", [1, None])


def test_a_named_type_needs_its_required_fields_and_defaults_the_optional_ones_to_none(
    types: SchemaTypes,
):
    note = types.validate("Note", {"text": "x"})

    assert isinstance(note, BaseModel)
    assert (note.text, note.count, note.tags) == ("x", None, None)
    with pytest.raises(ValidationError, match="text"):
        types.validate("Note", {})
    with pytest.raises(ValidationError):
        types.validate("Note", {"text": "x", "count": "many"})


def test_a_named_type_keeps_the_fields_it_does_not_know(types: SchemaTypes):
    note = types.validate("Note", {"text": "x", "surprise": [1]})

    assert note.model_dump() == {"text": "x", "count": None, "tags": None, "surprise": [1]}
    assert types.validate_plain("Note", {"text": "x", "surprise": 1})["surprise"] == 1


def test_a_plain_value_is_json_data_all_the_way_down(types: SchemaTypes):
    plain = types.validate_plain(
        "list[Box]",
        [{"note": {"text": "a", "count": "2"}, "extra": {"k": [1]}}, {"note": {"text": "b"}}],
    )

    assert plain == [
        {"note": {"text": "a", "count": 2, "tags": None}, "extra": {"k": [1]}},
        {"note": {"text": "b", "count": None, "tags": None}, "extra": None},
    ]
    assert type(plain[0]) is dict and type(plain[0]["note"]) is dict


def test_check_says_none_when_a_value_fits_and_why_when_it_does_not(types: SchemaTypes):
    assert types.check("Note", {"text": "x"}) is None
    assert types.check("int", "7") is None
    reason = types.check("Note", {"count": 1})
    assert reason is not None and "text" in reason
    assert types.check("int", "seven") is not None


def test_a_type_the_schema_does_not_name_is_a_key_error(types: SchemaTypes):
    with pytest.raises(KeyError, match="Ghost"):
        types.validate("Ghost", {})
    with pytest.raises(KeyError):
        types.python_type("list[Ghost]")


def test_an_expression_that_is_not_one_is_a_value_error(types: SchemaTypes):
    with pytest.raises(ValueError, match="not a type expression"):
        types.validate("note", {})


def test_the_same_expression_is_one_adapter_and_the_same_name_one_model(types: SchemaTypes):
    assert types.adapter("list[Note]") is types.adapter("list[Note]")
    assert types.adapter("Note") is not types.adapter("list[Note]")
    assert types.python_type("Note") is types.python_type("Box").model_fields["note"].annotation
    assert types.python_type("Note") is types.python_type("Note")
