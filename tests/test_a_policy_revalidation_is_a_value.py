"""``PolicyRevalidation``: the outcome of asking the server whether the group changed.

A portal that cannot be reached is an expected failure, so it comes back as
a value the caller reads, never as an exception.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.core.config.group_policy.revalidation import PolicyRevalidation


def test_nothing_changed_and_nothing_failed_by_default():
    outcome = PolicyRevalidation()

    assert outcome.changed is False
    assert outcome.error is None


def test_a_new_pack_is_a_change_without_an_error():
    outcome = PolicyRevalidation(changed=True)

    assert outcome.changed is True
    assert outcome.error is None


def test_a_failure_carries_its_reason_and_is_not_a_change():
    outcome = PolicyRevalidation(error="portal unreachable")

    assert outcome.error == "portal unreachable"
    assert outcome.changed is False


@pytest.mark.parametrize("field", ["changed", "error"])
def test_an_outcome_cannot_be_edited_after_it_is_made(field):
    outcome = PolicyRevalidation()

    with pytest.raises(ValidationError):
        setattr(outcome, field, True if field == "changed" else "x")


def test_equal_outcomes_compare_equal():
    assert PolicyRevalidation(changed=True) == PolicyRevalidation(changed=True)
    assert PolicyRevalidation(changed=True) != PolicyRevalidation(error="x")
