"""A spawn that never started still spent a slot, for the rest of the session.

``SpawnRunner.run`` reserves budget before it builds the sub-runnable,
which is right — the cap exists to stop work starting, so it has to be
checked first. What was missing was the other half: when ``_build``
comes back with an error, nothing was spawned and the reservation was
never handed back. ``SpawnBudget`` had ``try_reserve`` and ``reset``
(teardown only) and no way to undo a single reservation.

The failure that matters is the one the model actually produces:
inventing an agent name. ``AgentPool.get`` finds nothing for it and
``AgentSpawn._build_agent`` turns that into an error string — DPO-4 in
the tracker found a phantom ``researcher`` named on 79 of 79 rejected
sides. Twenty of those retired the session's whole 20-agent budget
without one sub-agent having run, and the refusal the model then got —
"Maximum total agents (20) reached." — says nothing about why.

``TeamSpawn`` is the sharper version: it reserves ``len(names)``, so a
single five-name team with one typo in it burned five.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.tools.orchestrate_budget import SpawnBudget
from igni_core.core.tools.orchestrate_spawn_agent import AgentSpawn
from igni_core.core.tools.orchestrate_spawn_team import TeamSpawn
from igni_core.core.tools.orchestrate_spawn_types import (
    OrchestrateScope,
    SpawnContext,
    SpawnDeps,
    SpawnWiring,
)


class _Pool:
    """``AgentPool`` stand-in that knows exactly one agent."""

    db = None

    def get(self, name: str) -> Any:
        if name != "reviewer":
            return None
        return SimpleNamespace(name=name)

    def missing(self, name: str) -> str:
        return f"Agent '{name}' not found"

    def get_entry(self, name: str) -> Any:
        return None


def _settings() -> Any:
    return SimpleNamespace(
        orchestration=SimpleNamespace(
            sub_team_timeout=30,
            max_task_iterations=3,
        )
    )


async def _unreached_streamer(context: Any) -> tuple[str, list[str]]:
    raise AssertionError("streamed a spawn whose build failed")


def _deps(**overrides: Any) -> SpawnDeps:
    """All five fields are required on ``SpawnDeps``. The defaults here
    fail loudly if a build-error path ever reaches them."""
    return SpawnDeps(
        **{
            "agent_streamer": _unreached_streamer,
            "team_streamer": _unreached_streamer,
            "team_factory": lambda _spec: SimpleNamespace(),
            "model_registry_factory": lambda _settings: SimpleNamespace(get_model=lambda: None),
            "nested_toolkit_factory": lambda _spec: SimpleNamespace(),
            **overrides,
        }
    )


def _context(budget: SpawnBudget, tmp_path: Path, **overrides: Any) -> SpawnContext:
    """The context every spawn in this file is built from."""
    kwargs: dict[str, Any] = {
        "scope": OrchestrateScope(
            pool=_Pool(),
            settings=_settings(),
            budget=budget,
            current_depth=0,
            max_depth=3,
            project_dir=tmp_path,
        ),
        "wiring": SpawnWiring(session_id="s1", hook_executor=None),
        "deps": _deps(),
        "task": "t",
    }
    kwargs.update(overrides)
    return SpawnContext(**kwargs)


class TestTheBudgetIsGivenBack:
    @pytest.mark.asyncio
    async def test_a_phantom_agent_name_does_not_spend_a_slot(self, tmp_path):
        """The regression, at the exact count. Before the fix
        ``budget.count`` was 1 here."""
        budget = SpawnBudget(max_agents=20)
        spawn = AgentSpawn(
            context=_context(budget, tmp_path, task="find the retry logic"),
            agent_name="researcher",
            isolation="",
        )

        result = await spawn.run()

        assert "researcher" in result  # the build error, not a budget error
        assert budget.count == 0

    @pytest.mark.asyncio
    async def test_twenty_phantom_spawns_leave_the_budget_intact(self, tmp_path):
        """The actual user-visible failure: the cap is reached without a
        single agent having run, and every later *valid* spawn is
        refused for the rest of the session. Before the fix the 21st
        call returned "Maximum total agents (20) reached."."""
        budget = SpawnBudget(max_agents=20)
        for _ in range(20):
            await AgentSpawn(
                context=_context(budget, tmp_path),
                agent_name="researcher",
                isolation="",
            ).run()

        assert budget.count == 0
        assert budget.try_reserve(1) is None  # a real spawn can still get in

    @pytest.mark.asyncio
    async def test_a_team_gives_back_every_seat_it_reserved(self, tmp_path):
        """``TeamSpawn._reserve_count`` is ``len(names)``. One bad name
        in a five-name team failed the whole build, so all five seats
        had to come back — not one."""
        budget = SpawnBudget(max_agents=20)
        spawn = TeamSpawn(
            context=_context(budget, tmp_path),
            names=["reviewer", "reviewer", "reviewer", "reviewer", "researcher"],
            mode="coordinate",
        )

        result = await spawn.run()

        assert "researcher" in result
        assert budget.count == 0

    @pytest.mark.asyncio
    async def test_a_successful_spawn_still_spends_its_slot(self, tmp_path):
        """The cap has to keep working. A release wired to the wrong
        branch would pass every test above and make the budget
        unenforceable."""
        budget = SpawnBudget(max_agents=20)

        async def _stream(_context: Any) -> tuple[str, list[str]]:
            return "done", []

        spawn = AgentSpawn(
            context=_context(budget, tmp_path, deps=_deps(agent_streamer=_stream)),
            agent_name="reviewer",
            isolation="",
        )

        result = await spawn.run()

        assert "done" in result
        assert budget.count == 1

    @pytest.mark.asyncio
    async def test_a_refused_reservation_is_not_released(self, tmp_path):
        """``try_reserve`` returning an error means nothing was taken.
        Releasing there would hand back slots that belong to the
        spawns still running."""
        budget = SpawnBudget(max_agents=1)
        budget.try_reserve(1)
        spawn = AgentSpawn(
            context=_context(budget, tmp_path),
            agent_name="researcher",
            isolation="",
        )

        result = await spawn.run()

        assert "Maximum total agents (1) reached." in result
        assert budget.count == 1


class TestRelease:
    def test_release_cannot_mint_slots(self):
        """Floors at zero. A double release — a subclass calling it as
        well as the template — must not make the cap larger than it is.
        """
        budget = SpawnBudget(max_agents=5)
        budget.try_reserve(2)

        budget.release(2)
        budget.release(2)

        assert budget.count == 0
        assert budget.try_reserve(5) is None
        assert budget.try_reserve(1) is not None

    @pytest.mark.asyncio
    async def test_the_template_releases_what_it_reserved_not_what_it_recomputes(self, tmp_path):
        """``run`` reads ``_reserve_count`` once and gives that same
        number back. Calling it a second time on the release side would
        be correct today and wrong the moment a subclass let ``_build``
        change what it depends on — here ``_build`` empties the names
        on its way to failing, so a recomputed release returns 0."""
        budget = SpawnBudget(max_agents=20)

        class _Forgetful(TeamSpawn):
            def _build(self) -> str | None:
                self._state.names = []
                return "Error: nope"

        spawn = _Forgetful(
            context=_context(budget, tmp_path),
            names=["reviewer", "reviewer", "researcher"],
            mode="coordinate",
        )

        assert await spawn.run() == "Error: nope"
        assert budget.count == 0
