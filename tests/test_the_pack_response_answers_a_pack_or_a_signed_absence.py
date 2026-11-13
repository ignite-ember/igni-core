"""``PackResponse``: the body of ``GET /v1/portal/me/group/pack`` and what it answers.

Either a pack, carrying the tag and the signed form it arrived with, or a
signed "this person has no pack". A response that is neither is an error,
not a half-empty pack.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from igni_core.core.config.group_policy.answers import SignedAbsence
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.response import PackResponse

WHEN = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class TestParsing:
    def test_an_empty_body_takes_the_defaults(self):
        response = PackResponse.model_validate({})

        assert response.group_id == "" and response.group_name == ""
        assert response.fetched_at is None
        assert response.default_model is None
        assert response.entries == []
        assert response.signed_pack is None
        assert response.no_pack is False

    def test_unknown_keys_are_ignored(self):
        response = PackResponse.model_validate({"group_id": "g", "added_later": {"x": 1}})

        assert response.group_id == "g"
        assert not hasattr(response, "added_later")

    def test_entries_are_read_as_entries(self):
        response = PackResponse.model_validate(
            {
                "entries": [
                    {
                        "kind": "agents",
                        "entry_name": "a",
                        "content": "c",
                        "content_type": "markdown",
                    }
                ]
            }
        )

        assert [(e.kind, e.entry_name, e.enabled) for e in response.entries] == [
            ("agents", "a", True)
        ]

    def test_an_entry_of_the_wrong_shape_is_refused(self):
        with pytest.raises(ValidationError):
            PackResponse.model_validate({"entries": [{"kind": "agents"}]})


class TestAnswer:
    def test_a_pack_carries_the_fields_the_tag_and_the_signed_text(self):
        response = PackResponse.model_validate(
            {
                "group_id": "g-1",
                "group_name": "Legal",
                "fetched_at": "2030-01-02T03:04:05Z",
                "default_model": "m-1",
                "signed_pack": "a.b.c",
            }
        )

        answer = response.answer("tag-1")

        assert isinstance(answer, GroupPolicyPack)
        assert (answer.group_id, answer.group_name) == ("g-1", "Legal")
        assert answer.fetched_at == WHEN
        assert answer.default_model == "m-1"
        assert (answer.etag, answer.signed_pack) == ("tag-1", "a.b.c")

    def test_a_pack_without_a_tag_has_none(self):
        answer = PackResponse(fetched_at=WHEN).answer(None)

        assert isinstance(answer, GroupPolicyPack)
        assert answer.etag is None and answer.signed_pack is None

    def test_no_pack_with_a_signature_is_a_signed_absence_with_the_tag(self):
        response = PackResponse(no_pack=True, signed_pack="a.b.c")

        assert response.answer("tag-2") == SignedAbsence(signed_pack="a.b.c", etag="tag-2")

    def test_the_plain_fields_do_not_turn_a_signed_absence_into_a_pack(self):
        response = PackResponse(no_pack=True, signed_pack="a.b.c", group_id="g", fetched_at=WHEN)

        assert isinstance(response.answer(None), SignedAbsence)

    def test_a_pack_without_a_time_is_an_error(self):
        with pytest.raises(ValueError, match="no fetched_at"):
            PackResponse(group_id="g", signed_pack="a.b.c").answer("t")

    def test_no_pack_without_a_signature_is_not_an_absence(self):
        """Only a signed absence may clear anything; an unsigned claim falls to the pack path."""
        with pytest.raises(ValueError, match="no fetched_at"):
            PackResponse(no_pack=True).answer(None)

        answer = PackResponse(no_pack=True, fetched_at=WHEN).answer(None)
        assert isinstance(answer, GroupPolicyPack)

    def test_an_empty_signature_is_not_a_signature(self):
        with pytest.raises(ValueError, match="no fetched_at"):
            PackResponse(no_pack=True, signed_pack="").answer(None)
