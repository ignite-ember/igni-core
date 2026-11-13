"""The spawn budget in isolation: the cap, the refusal text, give-back and reset."""

from __future__ import annotations

import sys
import threading

from igni_core.core.tools.orchestrate_budget import SpawnBudget

_REFUSAL = "Error: Maximum total agents ({}) reached."


class TestReserve:
    def test_reserving_up_to_the_cap_succeeds_and_one_more_is_refused(self):
        budget = SpawnBudget(3)
        assert budget.try_reserve(3) is None
        assert budget.count == 3
        assert budget.try_reserve(1) == _REFUSAL.format(3)
        assert budget.count == 3

    def test_a_request_larger_than_the_cap_is_refused_and_changes_nothing(self):
        budget = SpawnBudget(2)
        assert budget.try_reserve(3) == _REFUSAL.format(2)
        assert budget.count == 0

    def test_a_refused_reservation_leaves_earlier_slots_alone(self):
        budget = SpawnBudget(2)
        budget.try_reserve(1)
        assert budget.try_reserve(2) is not None
        assert budget.count == 1
        assert budget.try_reserve(1) is None

    def test_the_default_is_one_slot_and_zero_is_free(self):
        budget = SpawnBudget(1)
        assert budget.try_reserve(0) is None
        assert budget.count == 0
        assert budget.try_reserve() is None
        assert budget.try_reserve() == _REFUSAL.format(1)

    def test_a_cap_of_zero_refuses_everything(self):
        budget = SpawnBudget(0)
        assert budget.try_reserve() == _REFUSAL.format(0)
        assert budget.max_agents == 0

    def test_the_cap_is_coerced_to_an_int(self):
        budget = SpawnBudget("4")  # type: ignore[arg-type]
        assert budget.max_agents == 4
        assert budget.try_reserve(4) is None
        assert budget.try_reserve(1) == _REFUSAL.format(4)

    def test_threads_racing_for_slots_never_overshoot_the_cap(self):
        budget = SpawnBudget(10)
        granted: list[bool] = []
        start = threading.Barrier(40)
        switch = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)

        def worker() -> None:
            start.wait()
            granted.append(budget.try_reserve() is None)

        threads = [threading.Thread(target=worker) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        sys.setswitchinterval(switch)
        assert sum(granted) == 10
        assert budget.count == 10


class TestGiveBackAndReset:
    def test_release_frees_slots_for_a_later_reservation(self):
        budget = SpawnBudget(2)
        budget.try_reserve(2)
        budget.release(1)
        assert budget.count == 1
        assert budget.try_reserve(1) is None

    def test_a_double_release_floors_at_zero(self):
        budget = SpawnBudget(2)
        budget.try_reserve(1)
        budget.release(1)
        budget.release(1)
        assert budget.count == 0
        assert budget.try_reserve(3) is not None

    def test_reset_drops_the_count_to_zero(self):
        budget = SpawnBudget(2)
        budget.try_reserve(2)
        budget.reset()
        assert budget.count == 0
        assert budget.try_reserve(2) is None

    def test_a_release_after_reset_cannot_go_negative(self):
        budget = SpawnBudget(2)
        budget.try_reserve(1)
        budget.reset()
        budget.release(1)
        assert budget.count == 0


class _CountingLock:
    """A lock that records how often the budget took it."""

    def __init__(self) -> None:
        self.held = 0
        self.entered = 0

    def __enter__(self) -> None:
        self.held += 1
        self.entered += 1

    def __exit__(self, *_exc: object) -> None:
        self.held -= 1


class TestTheLock:
    def test_each_change_is_made_while_holding_the_budgets_lock(self):
        budget = SpawnBudget(5)
        lock = _CountingLock()
        budget._lock = lock  # type: ignore[assignment]
        budget.try_reserve(2)
        budget.try_reserve(9)
        budget.release(1)
        budget.reset()
        assert lock.entered == 4
        assert lock.held == 0
