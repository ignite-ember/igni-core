"""The sub-agent registry: what a cancel can reach."""

from __future__ import annotations

from igni_core.core.tools.orchestrate_registry import SubAgentRegistry


class TestRegistry:
    def test_a_registered_run_is_present_once_however_often_it_is_added(self):
        reg = SubAgentRegistry()
        reg.register("r1")
        reg.register("r1")
        assert "r1" in reg
        assert len(reg) == 1

    def test_an_empty_run_id_is_ignored(self):
        reg = SubAgentRegistry()
        reg.register("")
        assert len(reg) == 0

    def test_discard_removes_and_tolerates_an_absent_id(self):
        reg = SubAgentRegistry(["r1"])
        reg.discard("r1")
        reg.discard("r1")
        reg.discard("never")
        assert "r1" not in reg
        assert len(reg) == 0

    def test_initial_ids_seed_it_and_none_gives_an_empty_one(self):
        assert sorted(SubAgentRegistry(["a", "b"])) == ["a", "b"]
        assert len(SubAgentRegistry(None)) == 0

    def test_membership_of_a_non_string_is_false(self):
        assert 5 not in SubAgentRegistry(["5"])

    def test_iteration_is_over_a_snapshot_so_a_cancel_may_discard_meanwhile(self):
        reg = SubAgentRegistry(["a", "b", "c"])
        seen = []
        for run_id in reg:
            seen.append(run_id)
            reg.discard(run_id)
        assert sorted(seen) == ["a", "b", "c"]
        assert len(reg) == 0

    def test_a_snapshot_is_an_independent_copy(self):
        reg = SubAgentRegistry(["a"])
        snap = reg.snapshot()
        snap.add("x")
        reg.register("y")
        assert snap == {"a", "x"}
        assert reg.snapshot() == {"a", "y"}

    def test_clear_empties_it(self):
        reg = SubAgentRegistry(["a", "b"])
        reg.clear()
        assert len(reg) == 0
