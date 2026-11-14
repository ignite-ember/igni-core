"""The anchor table's own answers: who owns what, in what order, and how far expansion goes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.core.prompts.fragments import (
    AnchorRegistration,
    FragmentSupplier,
    PromptFragments,
)


def test_owner_of_names_the_owner_or_nobody():
    fragments = PromptFragments()
    fragments.register("THING", "acme.ext", lambda: "x")
    assert fragments.owner_of("THING") == "acme.ext"
    assert fragments.owner_of("OTHER") is None


def test_the_anchors_are_listed_in_name_order_and_leaving_removes_only_the_leaver_s():
    fragments = PromptFragments()
    for anchor, owner in (("ZED", "a"), ("ALPHA", "b"), ("MID", "a")):
        fragments.register(anchor, owner, lambda: "x")
    assert fragments.anchors() == ["ALPHA", "MID", "ZED"]
    fragments.remove_owner("a")
    fragments.remove_owner("nobody")
    assert fragments.anchors() == ["ALPHA"]


def test_what_a_supplier_returns_is_not_expanded_again():
    fragments = PromptFragments()
    fragments.register("OUTER", "a", lambda: "see {{INNER}}")
    fragments.register("INNER", "a", lambda: "deep")
    assert fragments.expand("{{OUTER}}") == "see {{INNER}}"


def test_a_supplier_that_returns_nothing_leaves_nothing_and_neighbours_still_fill():
    fragments = PromptFragments()
    fragments.register("EMPTY", "a", lambda: "")
    fragments.register("FULL", "a", lambda: "f")
    assert fragments.expand("[{{EMPTY}}{{FULL}}{{FULL}}]") == "[ff]"


def test_a_lone_brace_pair_and_odd_names_are_left_as_written():
    fragments = PromptFragments()
    fragments.register("X", "a", lambda: "x")
    text = "{{ and {{x}} and {{1X}} and {{X"
    assert fragments.expand(text) == text


def test_a_claim_is_a_frozen_model_and_a_refusal_carries_its_reason():
    claim = FragmentSupplier(owner="a", supply=lambda: "x")
    with pytest.raises(ValidationError):
        claim.owner = "b"  # type: ignore[misc]
    done = PromptFragments().register("bad name", "a", lambda: "x")
    assert isinstance(done, AnchorRegistration)
    assert done.ok is False and "bad name" in done.reason
    ok = PromptFragments().register("GOOD_1", "a", lambda: "x")
    assert ok.ok is True and ok.reason == ""
    with pytest.raises(ValidationError):
        ok.ok = False  # type: ignore[misc]


def test_two_tables_do_not_share_claims():
    first, second = PromptFragments(), PromptFragments()
    first.register("A", "o", lambda: "x")
    assert second.anchors() == [] and second.expand("{{A}}") == "{{A}}"
