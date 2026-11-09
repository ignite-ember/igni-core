"""Tests for the json-render spec contract.

``@json-render/core`` owns the component catalog and the FE's
renderer is the source of truth on shape. These models type the
*envelope* — the root id, the element map, the state bag — and stay
permissive about everything else, because a spec the FE would have
rendered must never be rejected on the BE.

Two rules the tests hold:

* :meth:`JsonRenderSpec.absorb` never raises. The card is drawn (or
  placeholdered) by the FE before the tool runs, so a payload the BE
  cannot model must not fail the call.
* :data:`SpecArgument` — the annotation the tool's ``spec`` argument
  carries — never rejects either, because Agno validates a tool
  call's arguments against it *before* the tool body runs.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from igni_core.core.tools.visualize_schemas import (
    JsonRenderElement,
    JsonRenderSpec,
    SpecArgument,
)


class TestJsonRenderSpec:
    def test_it_reads_the_envelope(self):
        spec = JsonRenderSpec.model_validate(
            {
                "root": "r",
                "elements": {
                    "r": {"type": "Text", "props": {"text": "hi"}, "children": ["c"]},
                    "c": {"type": "Stack", "props": {}, "children": []},
                },
                "state": {"tab": "home"},
            }
        )
        assert spec.root == "r"
        assert spec.elements["r"].type == "Text"
        assert spec.elements["r"].props == {"text": "hi"}
        assert spec.elements["r"].children == ["c"]
        assert spec.state == {"tab": "home"}

    def test_an_unknown_top_level_key_survives(self):
        # The renderer's vocabulary is not ours to enumerate — a key
        # we do not model must travel through, not be dropped.
        spec = JsonRenderSpec.model_validate({"root": "r", "elements": {}, "theme": "dark"})
        assert spec.model_dump()["theme"] == "dark"

    def test_an_unknown_element_key_survives(self):
        element = JsonRenderElement.model_validate({"type": "Text", "visible": {"eq": "x"}})
        assert element.visible == {"eq": "x"}

    def test_a_boolean_visibility_is_a_visibility(self):
        # The FE's renderer takes ``true``/``false`` for "always
        # visible"/"always hidden", and the visualizer's prompt says
        # so — a boolean is a condition, not a mistake.
        element = JsonRenderElement.model_validate({"type": "Text", "visible": True})
        assert element.visible is True

    def test_element_fields_default_to_empty(self):
        element = JsonRenderElement.model_validate({"type": "Text"})
        assert element.props == {}
        assert element.children == []
        assert element.on == {}
        assert element.visible is None

    def test_absorb_takes_the_mapping_the_model_wrote(self):
        # The mapping the model wrote is the shape that arrives at
        # the tool.
        spec = JsonRenderSpec.absorb(
            {"root": "r", "elements": {"r": {"type": "Text", "props": {"text": "hi"}}}}
        )
        assert spec.root == "r"
        assert spec.elements["r"].type == "Text"

    def test_absorb_returns_a_model_unchanged(self):
        model = JsonRenderSpec(root="r", elements={})
        assert JsonRenderSpec.absorb(model) is model

    @pytest.mark.parametrize(
        "raw",
        [
            "not a spec at all",
            None,
            ["a", "list"],
            42,
            {"elements": {"r": "not an element"}},
            {"root": 5},
            {"elements": ["not", "a", "map"]},
        ],
    )
    def test_absorb_never_raises(self, raw):
        # A malformed payload must not fail the call: the FE has
        # already drawn the card by the time the tool runs.
        spec = JsonRenderSpec.absorb(raw)
        assert isinstance(spec, JsonRenderSpec)
        assert spec.root == ""
        assert spec.elements == {}

    def test_the_models_reject_what_they_own(self):
        # The models are permissive about the renderer's vocabulary,
        # not about their own field types — a caller that builds a
        # spec by hand still gets told when it is wrong.
        with pytest.raises(ValidationError):
            JsonRenderElement.model_validate({"type": 5})
        with pytest.raises(ValidationError):
            JsonRenderSpec.model_validate({"root": "r", "elements": "not a map"})

    def test_a_truthy_word_is_not_a_visibility(self):
        # The FE's renderer takes ``true``/``false`` and condition
        # objects. A string is neither, and reading it as one would
        # be the BE deciding what the card means.
        with pytest.raises(ValidationError):
            JsonRenderElement.model_validate({"type": "Text", "visible": "yes"})

    def test_the_envelope_is_required(self):
        # The visualizer's prompt says ``root`` and ``elements`` are
        # always there and ``state`` is optional. The model says the
        # same, so the schema the model reads cannot ask for a spec
        # the renderer would not accept.
        with pytest.raises(ValidationError):
            JsonRenderSpec.model_validate({"elements": {}})
        spec = JsonRenderSpec.model_validate({"root": "r", "elements": {}})
        assert spec.state == {}


class TestTheToolsArgument:
    """The annotation the ``visualize`` tool takes its spec as.

    Agno validates a tool call's arguments against the tool's own
    annotations *before* the tool body runs, so this annotation is
    where a spec the FE would have drawn could still fail the call.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "not a spec at all",
            None,
            42,
            ["a", "list"],
            {"elements": {}},
            {"root": "r", "elements": {"r": "not an element"}},
            {"root": 5},
        ],
    )
    def test_it_never_rejects(self, raw):
        adapter = TypeAdapter(SpecArgument)
        assert isinstance(adapter.validate_python(raw), JsonRenderSpec)

    def test_it_keeps_a_good_spec_intact(self):
        adapter = TypeAdapter(SpecArgument)
        spec = adapter.validate_python(
            {"root": "r", "elements": {"r": {"type": "Text", "visible": True}}}
        )
        assert spec.root == "r"
        assert spec.elements["r"].visible is True
