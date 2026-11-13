"""``spawn_team``: how a sub-team is built, what it costs and what it says.

A team costs one budget slot per member, is refused before it reserves
anything when it has no members, is built with Agno telemetry off and the
pool's database, and answers with a header that names its members.
``TeamSpawn.build_team`` is also how a team a restart cut off is rebuilt,
so it is exercised directly as well as through ``run``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.hooks.untrusted import UntrustedOutputHook
from igni_core.core.tools.orchestrate_budget import SpawnBudget
from igni_core.core.tools.orchestrate_modes import TeamMode
from igni_core.core.tools.orchestrate_spawn_team import TeamSpawn
from igni_core.core.tools.orchestrate_spawn_types import (
    DEFAULT_TEAM_MAX_ITERATIONS,
    OrchestrateScope,
    SpawnContext,
    SpawnDeps,
    SpawnWiring,
    TeamBuildRequest,
    TeamSpec,
)


class _Member:
    """Stands in for an Agno agent: copyable, with a hook list."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.tool_hooks: list[Any] = ["pool-hook"]


class _Pool:
    db = object()

    def __init__(self, descriptions: dict[str, str | None]) -> None:
        self.members = {name: _Member(name) for name in descriptions}
        self.descriptions = descriptions

    def get(self, name: str) -> Any:
        return self.members.get(name)

    def get_entry(self, name: str) -> Any:
        description = self.descriptions.get(name)
        if description is None:
            return None
        return SimpleNamespace(definition=SimpleNamespace(description=description))

    def missing(self, name: str) -> str:
        return f"Agent '{name}' not found"


class _Seen:
    """What the doubles behind the spawn were asked."""

    def __init__(self) -> None:
        self.specs: list[TeamSpec] = []
        self.streamed: list[Any] = []
        self.stream_result: tuple[str, list[str]] = ("the answer", ["activity line"])
        self.stream_error: Exception | None = None
        self.settings_seen: list[Any] = []


def _deps(seen: _Seen) -> SpawnDeps:
    async def team_streamer(context: Any) -> tuple[str, list[str]]:
        seen.streamed.append(context)
        if seen.stream_error is not None:
            raise seen.stream_error
        return seen.stream_result

    def team_factory(spec: TeamSpec) -> Any:
        seen.specs.append(spec)
        return SimpleNamespace(spec=spec)

    def registry_factory(settings: Any) -> Any:
        seen.settings_seen.append(settings)
        return SimpleNamespace(get_model=lambda: "the-model")

    return SpawnDeps(
        agent_streamer=lambda context: None,
        team_streamer=team_streamer,
        team_factory=team_factory,
        model_registry_factory=registry_factory,
        nested_toolkit_factory=lambda spec: None,
    )


def _settings(timeout: float = 30, iterations: int = 7) -> Any:
    return SimpleNamespace(
        orchestration=SimpleNamespace(sub_team_timeout=timeout, max_task_iterations=iterations)
    )


def _spawn(
    names: list[str],
    *,
    mode: str = "",
    pool: _Pool | None = None,
    seen: _Seen | None = None,
    budget: SpawnBudget | None = None,
    depth: int = 0,
    timeout: float = 30,
    task: str = "review it",
) -> tuple[TeamSpawn, _Seen, SpawnBudget]:
    seen = seen or _Seen()
    budget = budget or SpawnBudget(max_agents=20)
    pool = pool or _Pool({"a": "Alpha agent", "b": "Beta agent"})
    scope = OrchestrateScope(
        pool=pool,
        settings=_settings(timeout),
        budget=budget,
        current_depth=depth,
        max_depth=5,
        project_dir=None,
    )
    context = SpawnContext(
        scope=scope,
        wiring=SpawnWiring(session_id="s1", hook_executor=None),
        deps=_deps(seen),
        task=task,
    )
    return TeamSpawn(context=context, names=names, mode=mode), seen, budget


class TestWhatATeamCosts:
    async def test_no_names_is_refused_before_anything_is_reserved(self):
        spawn, seen, budget = _spawn([])

        assert await spawn.run() == "Error: No agent names provided."
        assert budget.count == 0
        assert seen.specs == []

    async def test_one_slot_per_member_duplicates_included(self):
        spawn, _, budget = _spawn(["a", "b", "a"])

        await spawn.run()

        assert budget.count == 3

    async def test_a_team_larger_than_the_budget_is_refused_whole(self):
        spawn, seen, budget = _spawn(["a", "b", "a"], budget=SpawnBudget(max_agents=2))

        assert await spawn.run() == "Error: Maximum total agents (2) reached."
        assert budget.count == 0
        assert seen.specs == []


class TestHowTheTeamIsBuilt:
    async def test_the_spec_carries_the_pools_db_markdown_no_telemetry_and_a_depth_name(self):
        pool = _Pool({"a": "Alpha", "b": "Beta"})
        spawn, seen, _ = _spawn(["a", "b"], pool=pool, depth=2)

        await spawn.run()

        (spec,) = seen.specs
        assert spec.name == "sub-team-depth-3"
        assert spec.db is pool.db
        assert spec.markdown is True
        assert spec.telemetry is False
        assert spec.model == "the-model"
        assert [m.name for m in spec.members] == ["a", "b"]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", TeamMode.COORDINATE),
            ("route", TeamMode.ROUTE),
            ("broadcast", TeamMode.BROADCAST),
            ("tasks", TeamMode.TASKS),
            ("nonsense", TeamMode.COORDINATE),
        ],
    )
    async def test_the_mode_is_parsed_and_defaults_to_coordinate(self, raw, expected):
        spawn, seen, _ = _spawn(["a"], mode=raw)

        await spawn.run()

        assert seen.specs[0].mode is expected

    @pytest.mark.parametrize("raw", ["", "route", "broadcast", "coordinate"])
    async def test_only_tasks_mode_reads_the_iteration_cap(self, raw):
        spawn, seen, _ = _spawn(["a"], mode=raw)

        await spawn.run()

        assert seen.specs[0].max_iterations == DEFAULT_TEAM_MAX_ITERATIONS

    async def test_tasks_mode_takes_the_configured_cap(self):
        spawn, seen, _ = _spawn(["a"], mode="tasks")

        await spawn.run()

        assert seen.specs[0].max_iterations == 7

    async def test_members_are_copies_with_their_own_quarantining_hooks(self):
        pool = _Pool({"a": "Alpha"})
        spawn, seen, _ = _spawn(["a"], pool=pool)

        await spawn.run()

        member = seen.specs[0].members[0]
        assert member is not pool.members["a"]
        assert isinstance(member.tool_hooks[0], UntrustedOutputHook)
        assert member.tool_hooks[1:] == ["pool-hook"]
        assert pool.members["a"].tool_hooks == ["pool-hook"]

    async def test_a_member_without_a_hook_list_still_gets_the_quarantine(self):
        pool = _Pool({"a": "Alpha"})
        del pool.members["a"].tool_hooks

        spawn, seen, _ = _spawn(["a"], pool=pool)
        await spawn.run()

        (hook,) = seen.specs[0].members[0].tool_hooks
        assert isinstance(hook, UntrustedOutputHook)

    async def test_an_unknown_member_is_the_pools_own_message_and_gives_the_slots_back(self):
        spawn, seen, budget = _spawn(["a", "ghost", "b"])

        assert await spawn.run() == "Agent 'ghost' not found"
        assert seen.specs == []
        assert budget.count == 0


class TestBuildTeamIsReusable:
    def test_it_builds_the_same_team_without_a_spawn(self):
        """How a team a restart cut off is rebuilt before it is continued."""
        seen = _Seen()
        pool = _Pool({"a": "Alpha"})
        request = TeamBuildRequest(
            pool=pool, settings=_settings(), names=["a"], mode="route", depth=4
        )

        built = TeamSpawn.build_team(request, _deps(seen))

        assert built.error is None
        assert built.mode is TeamMode.ROUTE
        assert built.team.spec.name == "sub-team-depth-4"
        assert built.team.spec.db is pool.db

    def test_an_unknown_member_comes_back_as_an_error_not_a_raise(self):
        built = TeamSpawn.build_team(
            TeamBuildRequest(pool=_Pool({}), settings=_settings(), names=["x"], mode=""),
            _deps(_Seen()),
        )

        assert built.team is None
        assert built.mode is None
        assert built.error == "Agent 'x' not found"


class TestWhatTheTeamSays:
    async def test_the_answer_is_a_header_and_the_response_without_the_activity(self):
        long = "d" * 90
        pool = _Pool({"a": long, "b": "Beta agent"})
        spawn, seen, _ = _spawn(["a", "b"], mode="route", pool=pool, task="review it")

        out = await spawn.run()

        header, _, response = out.partition("\n\nResponse:\n")
        assert response == "the answer"
        lines = header.split("\n")
        assert lines[0] == "[Team: a, b] (mode: route)"
        assert lines[1] == "[Members:"
        assert lines[2] == f"  - a: {'d' * 60}"
        assert lines[3] == "  - b: Beta agent]"
        assert lines[4] == "[Task: review it]"
        assert lines[5].startswith("[Time: ") and lines[5].endswith("s]")
        assert "activity line" not in out

    async def test_a_member_without_a_pool_entry_has_an_empty_description(self):
        pool = _Pool({"a": "Alpha"})
        pool.descriptions["a"] = None

        spawn, _, _ = _spawn(["a"], pool=pool)

        assert "  - a: ]" in await spawn.run()

    async def test_the_stream_is_labelled_with_the_team_and_its_mode(self):
        spawn, seen, _ = _spawn(["a", "b"], mode="broadcast", task="go")

        await spawn.run()

        (context,) = seen.streamed
        assert context.agent_path == ["team(broadcast:a,b)"]
        assert context.task == "go"
        assert context.card_id == spawn._hooks.card_id

    async def test_the_hook_label_and_mode(self):
        spawn, _, _ = _spawn(["a", "b"], mode="tasks")
        await spawn.run()

        assert spawn._agent_label() == "team(a,b)"
        assert spawn._start_mode() == "tasks"

    async def test_a_timeout_names_the_team(self):
        spawn, seen, _ = _spawn(["a", "b"], timeout=0.05)

        async def stall(context: Any) -> tuple[str, list[str]]:
            await asyncio.sleep(3600)
            return "", []

        spawn._context = spawn._context.model_copy(
            update={"deps": spawn._context.deps.model_copy(update={"team_streamer": stall})}
        )

        assert await spawn.run() == (
            "Sub-team 'team(coordinate:a,b)' exceeded spawn timeout (0.05s) and was aborted."
        )

    async def test_an_error_while_streaming_is_reported_not_raised(self):
        spawn, seen, _ = _spawn(["a"])
        seen.stream_error = RuntimeError("model fell over")

        assert await spawn.run() == "Error running sub-team: model fell over"
