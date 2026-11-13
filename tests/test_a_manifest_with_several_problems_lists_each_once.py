"""How ``ManifestErrors`` words a refusal: one line, or a list, in order."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import igni_core.extensions.manifest as manifest_module
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.manifest_errors import ManifestError, ManifestErrors

BASE = {
    "id": "acme.tickets",
    "version": "0.1.0",
    "contract": ">=1.0 <2",
    "trust": "unsigned",
    "dev": {"command": ["python", "server.py"]},
}


def _describe(**changes) -> str:
    try:
        ExtensionManifest.model_validate({**BASE, **changes})
    except ValidationError as exc:
        return ManifestErrors(ExtensionManifest).describe(exc)
    raise AssertionError("the manifest was accepted")


def test_one_problem_is_one_line():
    message = _describe(version="1")
    assert "\n" not in message
    assert message.startswith("version: ")


def test_several_problems_are_a_counted_bulleted_list_in_order():
    message = _describe(version="1", trust="trusted", bogus=1)
    lines = message.splitlines()
    assert lines[0] == "3 problems:"
    assert [line[:4] for line in lines[1:]] == ["  - "] * 3
    assert lines[1].startswith("  - version: ")
    assert lines[2].startswith("  - trust: ")
    assert lines[3].startswith("  - bogus: not a field igni knows")


def test_the_same_problem_twice_is_reported_once():
    class Pair(BaseModel):
        model_config = ConfigDict(extra="forbid")
        a: int

    with pytest.raises(ValidationError) as caught:
        Pair.model_validate({"a": 1, "zzz": 1})
    twin = ValidationError.from_exception_data(
        "Pair",
        [
            {"type": "extra_forbidden", "loc": ("zzz",), "input": 1},
            {"type": "extra_forbidden", "loc": ("zzz",), "input": 1},
        ],
    )
    assert ManifestErrors(Pair).describe(twin) == ManifestErrors(Pair).describe(caught.value)
    assert "problems" not in ManifestErrors(Pair).describe(twin)


def test_a_field_with_no_close_match_lists_what_is_known_there():
    message = _describe(zzzzzz=1)
    assert message.startswith("zzzzzz: not a field igni knows. Known here: id, version,")
    assert "did you mean" not in message
    assert "$schema" in message
    assert "schema_" not in message


def test_a_field_with_a_close_match_does_not_list_the_rest():
    message = _describe(versoin="1.0.0")
    assert "did you mean 'version'" in message
    assert "Known here" not in message


def test_a_nested_unknown_field_lists_the_nested_model_fields():
    message = _describe(vm={"zzzzzz": 1})
    assert message.startswith(
        "vm.zzzzzz: not a field igni knows. Known here: packages, setupScript"
    )


def test_an_unknown_field_inside_a_list_is_found_by_its_index():
    page = {"kind": "acme.tickets", "label": "L", "sidebr": True}
    message = _describe(contributes={"pages": [page]})
    assert message.startswith("contributes.pages[0].sidebr: not a field igni knows")
    assert "did you mean 'sidebar'" in message


def test_an_unknown_field_whose_parent_cannot_be_resolved_has_no_listing():
    class Loose(BaseModel):
        free: dict[str, int] = Field(default_factory=dict)

    error = ValidationError.from_exception_data(
        "Loose", [{"type": "extra_forbidden", "loc": ("free", "k", "zzz"), "input": 1}]
    )
    assert ManifestErrors(Loose).describe(error) == "free.k.zzz: not a field igni knows"


def test_a_list_index_is_written_in_brackets_and_the_top_has_no_dot():
    error = ValidationError.from_exception_data(
        "M", [{"type": "extra_forbidden", "loc": ("a", 2, "b"), "input": 1}]
    )
    assert ManifestErrors(ExtensionManifest).describe(error).startswith("a[2].b: ")


def test_a_problem_with_no_location_has_no_path_prefix():
    error = ValidationError.from_exception_data(
        "M",
        [
            {
                "type": "value_error",
                "loc": (),
                "input": {},
                "ctx": {"error": ValueError("whole thing is wrong")},
            }
        ],
    )
    assert ManifestErrors(ExtensionManifest).describe(error) == "whole thing is wrong"


def test_a_value_that_is_not_a_string_gets_no_suggestion():
    message = _describe(trust=5)
    assert message.startswith("trust: ")
    assert "did you mean" not in message
    assert 'one of "unsigned", "partner", "first-party"' in message


def test_a_value_far_from_every_option_gets_no_suggestion():
    message = _describe(trust="qqqqqqqq")
    assert "did you mean" not in message


def test_a_nearly_right_value_is_matched_to_the_option():
    assert "did you mean 'first-party'" in _describe(trust="first-prty")


def test_a_wrong_shape_for_a_field_with_advice_says_expected_an_object():
    message = _describe(permissions={"tools": ["x"]})
    assert "permissions.tools[0]: expected an object" in message
    assert '"readOnlyHint"' in message


def test_a_missing_hint_falls_back_to_the_annotations_advice():
    tool = {"name": "acme.tickets.list", "annotations": {"readOnlyHint": True}}
    message = _describe(permissions={"tools": [tool]})
    assert "every tool states all four MCP hints" in message


def test_a_rule_a_validator_states_has_no_prefix_and_no_fix_suffix():
    message = _describe(id="acme.tickets", trust="partner")
    assert message.startswith("acme.tickets: a 'dev' command needs trust 'unsigned'")
    assert "Value error" not in message
    assert message.endswith("whatever it liked.")


def test_an_assertion_prefix_is_dropped_too():
    class Checked(BaseModel):
        n: int

    error = ValidationError.from_exception_data(
        "Checked",
        [{"type": "assertion_error", "loc": ("n",), "input": 1, "ctx": {"error": "n is odd"}}],
    )
    assert ManifestErrors(Checked).describe(error) == "n: n is odd"


def test_a_field_is_known_by_its_alias_and_not_its_python_name():
    message = _describe(vm={"volume_gib": 1})
    assert "volume_gib: not a field igni knows" in message
    assert "did you mean 'volumeGib'" in message


def test_the_error_class_is_one_class_and_a_value_error():
    assert manifest_module.ManifestError is ManifestError
    assert issubclass(ManifestError, ValueError)


def test_what_an_author_reads_says_igni():
    message = _describe(zzzzzz=1)
    assert "igni" in message
    assert "".join(["em", "ber"]) not in message.lower()
