"""The producers of the cloud: a person, an app, or a space — and their incarnations.

``docs/CLOUD.md`` §9. The producer string is the one the token names:
``person:<id>``, ``app:<id>`` or ``space:<id>``, compared exactly. A
space belongs to an app, its owner. A producer's rows belong to an
*incarnation*, so a person removed and added again is a new one.
"""

from __future__ import annotations

import pytest
from igni_cloud.producer import ProducerIncarnation, ProducerKind, ProducerRef, UnknownProducer
from pydantic import ValidationError


def test_the_variables_the_node_agent_writes_are_named():
    assert ProducerIncarnation.ENV == "IGNI_PRODUCER_INCARNATION"
    assert ProducerRef.OWNER_ENV == "IGNI_PRODUCER_OWNER"
    assert ProducerIncarnation.SEPARATOR == "@"


def test_an_id_with_no_colon_is_refused():
    with pytest.raises(UnknownProducer):
        ProducerRef.parse("space")


def test_an_id_with_nothing_after_the_colon_is_refused():
    with pytest.raises(UnknownProducer):
        ProducerRef.parse("person:")


def test_a_kind_igni_does_not_know_is_refused():
    with pytest.raises(UnknownProducer, match="person, app, space"):
        ProducerRef.parse("robot:x")


def test_the_id_is_the_producers_own_and_is_taken_whole():
    """Everything after the first colon is the id, whatever is in it."""
    assert ProducerRef.parse("person:a:b").id == "a:b"
    assert ProducerRef.parse("person:a@b").id == "a@b"


def test_the_ref_is_the_canonical_string():
    for text in ("person:u-ada", "app:reporter", "space:s-42"):
        assert ProducerRef.parse(text).ref == text
    assert ProducerRef.parse("space:s-42", owner="app:reporter").ref == "space:s-42"


def test_nobody_signs_in_an_app_or_a_space():
    assert ProducerRef.parse("person:u-ada").unattended is False
    assert ProducerRef.parse("app:reporter").unattended is True
    assert ProducerRef.parse("space:s-42", owner="app:reporter").unattended is True


def test_an_owner_that_is_not_an_app_is_refused():
    with pytest.raises(UnknownProducer):
        ProducerRef.parse("space:s-42", owner="person:u-ada")


def test_only_a_space_has_an_owner():
    for text in ("person:u-ada", "app:reporter"):
        with pytest.raises(UnknownProducer):
            ProducerRef.parse(text, owner="app:reporter")


def test_the_rows_hold_the_incarnation():
    incarnation = ProducerIncarnation.of(ProducerRef.parse("person:u-ada"), "m-7f3")
    assert incarnation.stored == "person:u-ada@m-7f3"
    space = ProducerIncarnation.of(ProducerRef.parse("space:s-42", owner="app:reporter"), "s-42")
    assert space.stored == "space:s-42@s-42"


def test_an_incarnation_is_an_id_of_letters_and_digits():
    assert ProducerIncarnation.of(ProducerRef.parse("person:u-ada"), "m").incarnation == "m"
    for bad in ("", "m/1", "m@1", "m 1", "m-1\n", "ü"):
        with pytest.raises(UnknownProducer):
            ProducerIncarnation.of(ProducerRef.parse("person:u-ada"), bad)


def test_an_incarnation_is_at_most_a_hundred_and_twenty_eight_characters():
    producer = ProducerRef.parse("person:u-ada")
    longest = "m" * 128
    assert ProducerIncarnation.of(producer, longest).incarnation == longest
    with pytest.raises(UnknownProducer):
        ProducerIncarnation.of(producer, "m" * 129)


def test_the_refs_are_frozen():
    ref = ProducerRef.parse("person:u-ada")
    with pytest.raises(ValidationError):
        ref.id = "u-grace"
    incarnation = ProducerIncarnation.of(ref, "m-1")
    with pytest.raises(ValidationError):
        incarnation.incarnation = "m-2"


def test_the_three_kinds_are_the_ones_igni_knows():
    assert {kind.value for kind in ProducerKind} == {"person", "app", "space"}
