"""Tests for ``backend.message_dispatcher.serialize_result`` — the JSON-safety
converter every RPC response runs through.

It handles five branches in order:
  1. ``None`` → ``None``
  2. ``str / int / float / bool`` → returned as-is
  3. ``list / tuple`` → recursively serialized, returns a list
  4. ``dict`` → keys coerced to str, values recursively serialized
  5. Pydantic models (anything with ``model_dump``) → that dict
  6. Fallback: ``str(value)`` for arbitrary objects

The interesting risks are all in the dict branch (non-str keys
must coerce, not raise) and the Pydantic branch (model_dump must
land BEFORE the str() fallback, since pydantic models have a
__str__).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from igni_core.backend.message_dispatcher import serialize_result


class TestPrimitives:
    def test_none(self):
        assert serialize_result(None) is None

    def test_string(self):
        assert serialize_result("hello") == "hello"
        # Empty string stays a string (not None) — important
        # for distinguishing missing-field from empty-field at
        # the FE.
        assert serialize_result("") == ""

    def test_int(self):
        assert serialize_result(42) == 42
        assert serialize_result(0) == 0
        assert serialize_result(-1) == -1

    def test_float(self):
        # Floats pass through. NaN/Inf would technically be
        # un-JSON-safe but the function doesn't reject them;
        # the JSON encoder downstream does.
        assert serialize_result(3.14) == 3.14

    def test_bool(self):
        # ``isinstance(True, int)`` is True in Python — pin
        # that the bool branch lands FIRST so True/False
        # don't get coerced to 1/0.
        assert serialize_result(True) is True
        assert serialize_result(False) is False
        assert isinstance(serialize_result(True), bool)


class TestLists:
    def test_empty_list(self):
        assert serialize_result([]) == []

    def test_list_of_primitives(self):
        assert serialize_result([1, "a", True, None]) == [1, "a", True, None]

    def test_tuple_returns_list(self):
        # JSON has no tuple — the function converts to list
        # (which round-trips through json.dumps cleanly).
        assert serialize_result((1, 2, 3)) == [1, 2, 3]
        assert isinstance(serialize_result((1, 2, 3)), list)

    def test_nested_list(self):
        # Recursive — the inner list also goes through the
        # serializer.
        assert serialize_result([[1, 2], [3, 4]]) == [[1, 2], [3, 4]]


class TestDicts:
    def test_empty_dict(self):
        assert serialize_result({}) == {}

    def test_dict_with_str_keys(self):
        assert serialize_result({"a": 1, "b": 2}) == {"a": 1, "b": 2}

    def test_dict_with_non_str_keys_coerces_to_str(self):
        # The BE occasionally builds dicts keyed by ints
        # (e.g. run_id integers from an older Agno schema).
        # JSON requires string keys; without the coercion the
        # serializer would crash at the JSON layer downstream.
        out = serialize_result({1: "one", 2: "two"})
        assert out == {"1": "one", "2": "two"}
        for k in out:
            assert isinstance(k, str)

    def test_dict_with_none_keys_coerces(self):
        # Edge case — defensive coverage. None becomes "None".
        # Pin so a future "raise on None keys" change is a
        # deliberate choice.
        assert serialize_result({None: "x"}) == {"None": "x"}

    def test_dict_values_recursively_serialized(self):
        # The whole point — nested structures flatten cleanly.
        nested = {"outer": {"inner": [1, 2, {"deep": True}]}}
        assert serialize_result(nested) == {"outer": {"inner": [1, 2, {"deep": True}]}}


class TestPydanticModels:
    class _Sample(BaseModel):
        name: str
        count: int

    def test_model_dump_path(self):
        # Pydantic models have a __str__ that would otherwise
        # win the fallback branch — must land in the
        # model_dump branch FIRST or RPC responses would be
        # serialized as Python repr strings ("name='x'
        # count=1") instead of JSON dicts.
        m = self._Sample(name="x", count=1)
        out = serialize_result(m)
        assert out == {"name": "x", "count": 1}
        assert isinstance(out, dict)

    def test_model_dump_inside_list(self):
        # Recursive case — list of pydantic models.
        # The list branch should hit each element, and each
        # element's model_dump should fire.
        items = [
            self._Sample(name="a", count=1),
            self._Sample(name="b", count=2),
        ]
        out = serialize_result(items)
        assert out == [
            {"name": "a", "count": 1},
            {"name": "b", "count": 2},
        ]


class TestFallback:
    def test_arbitrary_object_uses_str(self):
        # Anything that doesn't match the earlier branches
        # falls through to str(). Common for Path objects,
        # custom enums without model_dump, etc.
        class Custom:
            def __str__(self):
                return "custom-repr"

        assert serialize_result(Custom()) == "custom-repr"

    def test_path_object_serialized_as_str(self):
        # Path is a common BE return type — the str() fallback
        # gives the OS-path string the FE can render.
        p = Path("/tmp/x.txt")
        assert serialize_result(p) == str(p)

    def test_set_falls_through_to_str(self):
        # Set isn't in the explicit branches — it falls
        # through to str() (not list). Pinned so a future
        # "support sets" change is deliberate (since the
        # current behaviour serializes ``{1, 2}`` as
        # ``"{1, 2}"`` which is probably wrong but pinned).
        out = serialize_result({1, 2})
        assert isinstance(out, str)


class TestNestedCombinations:
    def test_list_of_dicts_of_lists(self):
        # The shape RPC responses actually produce — sessions
        # listing, todos, etc. Make sure recursion handles
        # all the layers.
        value = [
            {"name": "a", "tags": ["x", "y"]},
            {"name": "b", "tags": ["z"]},
        ]
        assert serialize_result(value) == value

    def test_dict_with_pydantic_values(self):
        class _M(BaseModel):
            v: int

        out = serialize_result({"first": _M(v=1), "second": _M(v=2)})
        assert out == {"first": {"v": 1}, "second": {"v": 2}}

    def test_tuple_of_pydantic_models(self):
        class _M(BaseModel):
            v: int

        out = serialize_result((_M(v=1), _M(v=2)))
        # Tuple → list, models → dicts.
        assert out == [{"v": 1}, {"v": 2}]
