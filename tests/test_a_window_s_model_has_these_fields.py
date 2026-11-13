"""The fields of a window's model, the two failures it can have, and how a value is checked.

The table is the model's field list; a refusal (``WindowModelError``) is
the writer's fault and a state failure (``WindowStateError``) is the
backend's, and they are told apart on purpose.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from igni_core.core.session.client_state import ClientStateStore
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.window_errors import WindowModelError, WindowStateError
from igni_core.extensions.host.window_field import WindowFieldName, check_type, unset_nones
from igni_core.extensions.host.window_fields import WindowFields
from igni_core.extensions.host.window_state import LastingState
from igni_core.extensions.host.window_values import WindowModelState

FIELDS = (
    "session",
    "project_dir",
    "route",
    "panel",
    "sidebar",
    "preview",
    "selection",
    "composer",
    "toasts",
    "dialog",
    "scroll",
    "focused",
)


@pytest.fixture
def types() -> SchemaTypes:
    return SchemaTypes(HOST_SCHEMA)


@pytest.fixture
def fields(tmp_path: Path, types: SchemaTypes) -> WindowFields:
    return WindowFields(types, LastingState(ClientStateStore(tmp_path / "client_state.db")))


def test_a_refusal_is_the_writers_and_a_state_failure_is_the_backends() -> None:
    assert issubclass(WindowModelError, ValueError)
    assert issubclass(WindowStateError, RuntimeError)
    assert not issubclass(WindowModelError, RuntimeError)
    assert not issubclass(WindowStateError, ValueError)


def test_the_field_names_are_the_twelve_the_schema_knows() -> None:
    assert tuple(str(name) for name in WindowFieldName) == FIELDS
    assert tuple(WindowModelState.model_fields) == FIELDS


def test_the_table_lists_every_field_in_order_with_its_own_handler(fields: WindowFields) -> None:
    assert fields.names() == FIELDS
    assert tuple(h.name for h in fields.handlers()) == FIELDS
    assert all(fields.handler(name).name == name for name in FIELDS)


@pytest.mark.parametrize("name", ["", "Route", "ROUTE", "routes", " route"])
def test_a_name_that_is_not_a_field_is_refused_by_name(fields: WindowFields, name: str) -> None:
    with pytest.raises(WindowModelError, match=f"a window's model has no field {name!r}") as raised:
        fields.handler(name)

    assert raised.value.__cause__ is None


def test_the_toasts_and_the_dialog_change_through_their_own_operations(
    fields: WindowFields,
) -> None:
    assert {h.name for h in fields.handlers() if not h.writable} == {"toasts", "dialog"}


def test_only_a_route_written_again_is_still_a_change(fields: WindowFields) -> None:
    assert {h.name for h in fields.handlers() if h.always_changes} == {"route"}


def test_a_value_is_checked_against_a_type_expression_as_plain_data(fields: WindowFields) -> None:
    assert fields.check("n", "list[str]", ["a", "b"]) == ["a", "b"]
    with pytest.raises(WindowModelError, match=r"^n: expected list\[str\]: ") as raised:
        fields.check("n", "list[str]", "ab")
    assert raised.value.__cause__ is None


def test_a_model_is_dumped_before_it_is_checked(types: SchemaTypes) -> None:
    class Shown(BaseModel):
        open: bool = False
        width: int | None = None

    assert check_type(types, "sidebar", "Sidebar", Shown(open=True)) == {
        "open": True,
        "width": None,
        "section": None,
    }


def test_a_value_read_back_with_its_absent_parts_can_be_written_again(types: SchemaTypes) -> None:
    read_back = {"open": True, "width": None, "section": None}

    assert check_type(types, "sidebar", "Sidebar", read_back) == {
        "open": True,
        "width": None,
        "section": None,
    }


def test_a_key_holding_none_is_left_out_at_every_depth() -> None:
    assert unset_nones(
        {"a": None, "b": {"c": None, "d": 1}, "e": [{"f": None, "g": 2}, None, 3]}
    ) == {
        "b": {"d": 1},
        "e": [{"g": 2}, None, 3],
    }
    assert unset_nones({}) == {}
    assert unset_nones([]) == []
    assert unset_nones("é") == "é"
    assert unset_nones(0) == 0
