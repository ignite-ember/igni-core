"""The plan store: decisions, the run a plan belongs to, the rejection count.

Plan state is the decision made on the latest plan's run and nothing
else. These pin the edges of that bookkeeping that the flow tests only
touch in passing.
"""

from __future__ import annotations

import pytest

from igni_core.core.tools.plan import PlanDecision, PlanDecisionsBlob, PlanStore


def test_the_package_exports_exactly_the_plan_mode_surface():
    import igni_core.core.tools.plan as plan

    assert sorted(plan.__all__) == sorted(
        [
            "PlanDecision",
            "PlanDecisionsBlob",
            "PlanEnterResult",
            "PlanExitResult",
            "PlanModeEnteredEvent",
            "PlanResearcherRunner",
            "PlanSnapshot",
            "PlanStore",
            "PlanSubmitDecision",
            "PlanSubmitGate",
            "PlanSubmittedEvent",
            "PlanTaskBatch",
            "PlanTaskInput",
            "PlanTool",
            "PlanTransactionCoordinator",
        ]
    )
    for name in plan.__all__:
        assert getattr(plan, name) is not None


class TestRejections:
    def test_each_rejection_is_counted_and_the_count_returned(self):
        store = PlanStore()

        assert [store.record_rejection(), store.record_rejection()] == [1, 2]
        assert store.rejected == 2

    def test_resetting_starts_the_attempts_again(self):
        store = PlanStore()
        store.record_rejection()
        store.reset_rejections()

        assert store.rejected == 0
        assert store.record_rejection() == 1


class TestDecisionAdoption:
    def test_a_decision_teaches_the_latest_plan_its_run(self):
        store = PlanStore()
        store.set_plan("p")
        assert store.latest_state() == "pending"

        store.set_decision("r1", "approved")

        assert store.latest_run_id == "r1"
        assert store.latest_state() == "approved"

    def test_a_decision_does_not_overwrite_a_known_run(self):
        store = PlanStore()
        store.set_plan("p", run_id="r1")

        store.set_decision("r2", PlanDecision.DISMISSED)

        assert store.latest_run_id == "r1"
        assert store.latest_state() == "pending"
        assert store.get_decision("r2") == "dismissed"

    def test_a_decision_with_no_plan_learns_no_run(self):
        store = PlanStore()

        store.set_decision("r1", "approved")

        assert store.latest_run_id == ""
        assert store.latest_state() == ""

    def test_decisions_without_a_known_run_leave_the_plan_pending(self):
        store = PlanStore()
        store.decisions["r1"] = PlanDecision.APPROVED
        store.set_plan("p")

        assert store.latest_state() == "pending"

    def test_a_new_plan_forgets_the_previous_run(self):
        store = PlanStore()
        store.set_plan("a", run_id="r1")
        store.set_decision("r1", "approved")

        store.set_plan("b")

        assert store.latest_run_id == ""
        assert store.latest_state() == "pending"


class TestForgetDecision:
    def test_a_decision_that_was_not_kept_is_removed(self):
        store = PlanStore()
        store.set_decision("r1", "approved")

        store.forget_decision("r1", None)

        assert store.get_decision("r1") is None

    def test_a_previous_decision_is_restored(self):
        store = PlanStore()
        store.set_decision("r1", "dismissed")
        store.set_decision("r1", "approved")

        store.forget_decision("r1", PlanDecision.DISMISSED)

        assert store.get_decision("r1") == "dismissed"

    def test_forgetting_an_absent_decision_is_quiet(self):
        PlanStore().forget_decision("nope", None)


class TestValidation:
    def test_an_empty_run_id_is_refused(self):
        with pytest.raises(ValueError, match="run_id must be non-empty"):
            PlanStore().set_decision("", "approved")

    @pytest.mark.parametrize("bad", ["maybe", None, 3])
    def test_an_invalid_decision_is_refused_and_recorded_nowhere(self, bad):
        store = PlanStore()

        with pytest.raises(ValueError, match="decision must be one of"):
            store.set_decision("r1", bad)

        assert store.decisions == {}

    def test_an_empty_run_id_has_no_decision(self):
        store = PlanStore()
        store.decisions[""] = PlanDecision.APPROVED

        assert store.get_decision("") is None


class TestBlob:
    def test_corrupt_entries_are_dropped_and_good_ones_kept(self):
        blob = PlanDecisionsBlob.from_raw(
            {"r1": "approved", "": "approved", 7: "approved", "r2": ["x"], "r3": "bogus", "r4": 1}
        )

        assert blob.decisions == {"r1": PlanDecision.APPROVED}

    @pytest.mark.parametrize("raw", [None, [], "approved", 3])
    def test_a_blob_that_is_not_a_mapping_is_empty(self, raw):
        assert PlanDecisionsBlob.from_raw(raw).decisions == {}

    def test_loading_overwrites_known_entries_and_none_loads_nothing(self):
        store = PlanStore()
        store.set_decision("r1", "approved")

        store.load_decisions(None)
        assert store.get_decision("r1") == "approved"

        store.load_decisions(PlanDecisionsBlob.from_raw({"r1": "dismissed", "r2": "approved"}))
        assert store.get_decision("r1") == "dismissed"
        assert store.get_decision("r2") == "approved"

    def test_the_snapshot_is_an_independent_copy(self):
        store = PlanStore()
        store.set_decision("r1", "approved")

        snapshot = store.decisions_snapshot()
        snapshot.decisions["r2"] = PlanDecision.DISMISSED

        assert "r2" not in store.decisions


def test_the_plan_snapshot_does_not_leak_history_mutation():
    store = PlanStore()
    store.set_plan("a")
    store.set_plan("b")

    store.snapshot().history.append("x")

    assert store.history == ["a"]
