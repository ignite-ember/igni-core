"""The mode policy as a value: how it reads its tiers, what it refuses, where it clamps.

The session-level behaviour (``/accept``, ``/bypass``, the loader's start
clamp) is pinned in ``test_an_organisation_sets_the_loosest_mode``. This
file pins the policy itself: the ranking, malformed tier input, the exact
refusal text, and the fallback chain of ``clamp_start`` that no command
reaches one step at a time.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.core.config.permission_eval import PermissionMode
from igni_core.core.config.permission_eval.mode_policy import (
    MODE_RANK,
    ModeFloor,
    ModePolicy,
    is_at_least_as_strict,
)

PLAN = PermissionMode.PLAN
DONT_ASK = PermissionMode.DONT_ASK
DEFAULT = PermissionMode.DEFAULT
EDITS = PermissionMode.ACCEPT_EDITS
BYPASS = PermissionMode.BYPASS_PERMISSIONS


def _tier(**permissions) -> dict:
    return {"permissions": permissions}


class TestTheRanking:
    def test_plan_and_dont_ask_share_the_bottom_rank_then_it_loosens(self):
        assert MODE_RANK == {PLAN: 0, DONT_ASK: 0, DEFAULT: 1, EDITS: 2, BYPASS: 3}

    @pytest.mark.parametrize("mode", list(PermissionMode))
    def test_a_mode_is_always_as_strict_as_itself(self, mode):
        assert is_at_least_as_strict(mode, mode)

    @pytest.mark.parametrize(
        ("candidate", "floor", "allowed"),
        [
            (PLAN, DEFAULT, True),
            (DONT_ASK, EDITS, True),
            (DEFAULT, BYPASS, True),
            (DEFAULT, EDITS, True),
            (EDITS, DEFAULT, False),
            (BYPASS, EDITS, False),
            (PLAN, DONT_ASK, False),
            (DONT_ASK, PLAN, False),
            (DEFAULT, PLAN, False),
        ],
    )
    def test_only_a_strictly_lower_rank_or_the_same_mode_is_admitted(
        self, candidate, floor, allowed
    ):
        assert is_at_least_as_strict(candidate, floor) is allowed


class TestReadingTheTiers:
    @pytest.mark.parametrize("managed", [None, {}, {"permissions": None}, {"permissions": "plan"}])
    @pytest.mark.parametrize("group", [None, {}, {"permissions": ["mode"]}])
    def test_absent_or_malformed_tiers_set_nothing(self, managed, group):
        policy = ModePolicy.from_tiers(managed, group)

        assert policy.floors == ()
        assert policy.floor is None and policy.floor_source is None
        assert policy.allow_bypass is True and policy.bypass_source is None
        assert policy.is_empty

    @pytest.mark.parametrize("mode", [None, 3, ["plan"], "", "Plan", "PLAN", "auto", "bypass"])
    def test_a_mode_that_does_not_parse_sets_no_floor(self, mode):
        policy = ModePolicy.from_tiers(_tier(mode=mode), None)

        assert policy.floors == () and policy.is_empty

    @pytest.mark.parametrize("flag", ["false", "False", 0, None, [], "no", True])
    def test_only_the_boolean_false_forbids_bypass(self, flag):
        policy = ModePolicy.from_tiers(_tier(allow_bypass=flag), None)

        assert policy.allow_bypass is True and policy.bypass_source is None

    def test_the_literal_false_forbids_bypass_and_names_its_tier(self):
        managed = ModePolicy.from_tiers(_tier(allow_bypass=False), None)
        group = ModePolicy.from_tiers(None, _tier(allow_bypass=False))

        assert (managed.allow_bypass, managed.bypass_source) == (False, "managed settings")
        assert (group.allow_bypass, group.bypass_source) == (False, "group policy")
        assert not managed.is_empty

    def test_both_tiers_forbidding_bypass_are_both_named(self):
        policy = ModePolicy.from_tiers(_tier(allow_bypass=False), _tier(allow_bypass=False))

        assert policy.bypass_source == "managed settings and group policy"

    def test_one_tier_forbidding_is_enough_whatever_the_other_says(self):
        policy = ModePolicy.from_tiers(_tier(allow_bypass=True), _tier(allow_bypass=False))

        assert policy.allow_bypass is False
        assert policy.bypass_source == "group policy"

    def test_the_stricter_floor_comes_first_whichever_tier_set_it(self):
        policy = ModePolicy.from_tiers(_tier(mode="acceptEdits"), _tier(mode="default"))

        assert [(f.mode, f.source) for f in policy.floors] == [
            (DEFAULT, "group policy"),
            (EDITS, "managed settings"),
        ]
        assert (policy.floor, policy.floor_source) == (DEFAULT, "group policy")
        swapped = ModePolicy.from_tiers(_tier(mode="default"), _tier(mode="acceptEdits"))
        assert (swapped.floor, swapped.floor_source) == (DEFAULT, "managed settings")

    def test_equal_floors_are_both_kept(self):
        policy = ModePolicy.from_tiers(_tier(mode="plan"), _tier(mode="plan"))

        assert [f.source for f in policy.floors] == ["managed settings", "group policy"]

    def test_a_floor_makes_the_policy_non_empty(self):
        assert not ModePolicy.from_tiers(_tier(mode="default"), None).is_empty
        assert ModePolicy().is_empty

    def test_the_records_are_frozen(self):
        policy = ModePolicy.from_tiers(_tier(mode="plan"), None)

        with pytest.raises(ValidationError):
            policy.allow_bypass = False
        with pytest.raises(ValidationError):
            policy.floors[0].mode = BYPASS
        with pytest.raises(ValidationError):
            ModeFloor(mode=PLAN, source="x").source = "y"


class TestWhatIsRefused:
    def test_an_empty_policy_refuses_nothing(self):
        assert all(ModePolicy().refusal(mode) is None for mode in PermissionMode)

    def test_the_bypass_refusal_names_the_tier(self):
        policy = ModePolicy.from_tiers(None, _tier(allow_bypass=False))

        assert policy.refusal(BYPASS) == (
            "Error: bypassPermissions is turned off by your organisation's group policy "
            "(permissions.allow_bypass: false)."
        )
        assert policy.refusal(EDITS) is None

    def test_a_bypass_ban_built_without_a_source_says_policy(self):
        assert ModePolicy(allow_bypass=False).refusal(BYPASS) == (
            "Error: bypassPermissions is turned off by your organisation's policy "
            "(permissions.allow_bypass: false)."
        )

    def test_the_floor_refusal_names_both_modes_and_the_tier(self):
        policy = ModePolicy.from_tiers(_tier(mode="default"), None)

        assert policy.refusal(EDITS) == (
            "Error: acceptEdits is looser than the default mode set by your organisation's "
            "managed settings; this session cannot leave default for a looser mode."
        )
        assert policy.refusal(DEFAULT) is None
        assert policy.refusal(PLAN) is None

    def test_a_ban_is_reported_before_a_floor(self):
        policy = ModePolicy.from_tiers(_tier(mode="default", allow_bypass=False), None)

        assert "turned off" in policy.refusal(BYPASS)

    def test_the_stricter_tier_decides_the_refusal(self):
        policy = ModePolicy.from_tiers(_tier(mode="acceptEdits"), _tier(mode="default"))

        assert "group policy" in policy.refusal(EDITS)

    def test_plan_and_dont_ask_do_not_admit_each_other(self):
        assert ModePolicy.from_tiers(_tier(mode="plan"), None).refusal(DONT_ASK) is not None
        assert ModePolicy.from_tiers(_tier(mode="dontAsk"), None).refusal(PLAN) is not None


class TestWhereAStartIsClamped:
    def test_an_allowed_mode_starts_as_asked(self):
        policy = ModePolicy.from_tiers(_tier(mode="acceptEdits"), None)

        assert policy.clamp_start(EDITS) is EDITS
        assert policy.clamp_start(PLAN) is PLAN
        assert ModePolicy().clamp_start(BYPASS) is BYPASS

    def test_a_looser_mode_lands_on_the_floor(self):
        policy = ModePolicy.from_tiers(_tier(mode="default"), None)

        assert policy.clamp_start(BYPASS) is DEFAULT
        assert policy.clamp_start(EDITS) is DEFAULT

    def test_with_two_floors_it_lands_on_the_stricter(self):
        policy = ModePolicy.from_tiers(_tier(mode="acceptEdits"), _tier(mode="default"))

        assert policy.clamp_start(BYPASS) is DEFAULT

    def test_a_banned_bypass_without_a_floor_starts_in_default(self):
        policy = ModePolicy.from_tiers(_tier(allow_bypass=False), None)

        assert policy.clamp_start(BYPASS) is DEFAULT

    def test_a_banned_bypass_floor_is_itself_refused_so_it_falls_to_default(self):
        policy = ModePolicy.from_tiers(_tier(mode="bypassPermissions", allow_bypass=False), None)

        assert policy.refusal(BYPASS) is not None
        assert policy.clamp_start(BYPASS) is DEFAULT

    def test_under_a_plan_floor_default_is_refused_and_lands_on_plan(self):
        policy = ModePolicy.from_tiers(_tier(mode="plan"), None)

        assert policy.clamp_start(DEFAULT) is PLAN
        assert policy.clamp_start(DONT_ASK) is PLAN

    def test_under_a_dont_ask_floor_plan_is_refused_and_lands_on_dont_ask(self):
        policy = ModePolicy.from_tiers(_tier(mode="dontAsk"), None)

        assert policy.clamp_start(PLAN) is DONT_ASK

    def test_when_two_floors_exclude_each_other_and_default_the_strictest_floor_wins(self):
        both = ModePolicy.from_tiers(_tier(mode="plan"), _tier(mode="dontAsk"))

        assert both.refusal(PLAN) is not None and both.refusal(DONT_ASK) is not None
        assert both.refusal(DEFAULT) is not None
        assert both.clamp_start(BYPASS) is both.floors[0].mode
        assert both.clamp_start(BYPASS) is PLAN
