"""The agent records: how sources rank, how reports merge, and the kwargs
an agent is constructed with."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_core.core.agents.schemas import (
    AgentBuildContext,
    AgentConstructorArgs,
    AgentDefinition,
    AgentEntry,
    AgentInfo,
    AgentPriority,
    DbHandle,
    LoadError,
    LoadReport,
    McpClient,
)
from igni_core.core.agno_telemetry import AgnoTelemetry
from igni_core.core.tools.process_supervisor import ProcessSupervisor


def _entry(name: str, priority: AgentPriority, description: str = "d") -> AgentEntry:
    return AgentEntry(
        definition=AgentDefinition(name=name, description=description), priority=priority
    )


class TestPrioritiesRank:
    def test_the_values_are_the_documented_ones(self):
        assert [p.value for p in AgentPriority] == [0, 1, 2, 3, 4, 5, 6, 10]

    def test_a_priority_compares_with_a_plain_int(self):
        assert AgentPriority.EPHEMERAL > 9
        assert AgentPriority.USER_CLAUDE == 0

    def test_an_extension_override_beats_every_file_source(self):
        files = [p for p in AgentPriority if p not in (AgentPriority.EPHEMERAL,)]
        assert max(files) is AgentPriority.EXTENSION_OVERRIDE


class TestADefinition:
    def test_its_defaults(self):
        defn = AgentDefinition(name="a", description="d")

        assert defn.tools == []
        assert defn.mcp_servers == []
        assert defn.reasoning is False
        assert (defn.reasoning_min_steps, defn.reasoning_max_steps) == (1, 10)
        assert defn.can_orchestrate is True
        assert defn.source_path is None
        assert defn.force_isolation is None

    def test_namespaced_is_a_prefixed_copy(self):
        original = AgentDefinition(name="helper", description="d", tools=["Bash"])

        copy = original.namespaced("plug")

        assert copy.name == "plug:helper"
        assert original.name == "helper"
        assert copy.tools == ["Bash"]
        assert copy is not original

    def test_a_prefix_is_applied_as_given(self):
        defn = AgentDefinition(name="helper", description="d")

        assert defn.namespaced("").name == ":helper"
        assert defn.namespaced("a:b").name == "a:b:helper"
        assert defn.namespaced("né").name == "né:helper"

    def test_only_the_worktree_isolation_is_accepted(self):
        assert AgentDefinition(name="a", description="d", force_isolation="worktree")
        with pytest.raises(ValidationError):
            AgentDefinition(name="a", description="d", force_isolation="container")  # type: ignore[arg-type]


class TestTheAgentsPanelShape:
    def test_only_the_name_is_required_and_the_rest_is_empty(self):
        info = AgentInfo(name="a")

        assert info.model_dump() == {
            "name": "a",
            "description": "",
            "tools": [],
            "model": "",
            "color": "",
            "can_orchestrate": True,
            "mcp_servers": [],
            "tags": [],
            "system_prompt": "",
            "source_path": "",
            "is_ephemeral": False,
            "unknown_tools": [],
        }

    def test_every_field_describes_itself_for_the_published_schema(self):
        properties = AgentInfo.model_json_schema()["properties"]

        assert set(properties) == set(AgentInfo.model_fields)
        assert all(prop.get("description") for prop in properties.values())

    def test_the_lists_are_not_shared_between_instances(self):
        first, second = AgentInfo(name="a"), AgentInfo(name="b")
        first.tools.append("Bash")

        assert second.tools == []


class TestAReportMerge:
    def test_a_higher_priority_replaces(self):
        report = LoadReport(entries={"a": _entry("a", AgentPriority.USER_CLAUDE, "old")})
        other = LoadReport(entries={"a": _entry("a", AgentPriority.PROJECT_LOCAL, "new")})

        report.merge(other)

        assert report.entries["a"].definition.description == "new"

    def test_a_lower_priority_is_ignored(self):
        report = LoadReport(entries={"a": _entry("a", AgentPriority.PROJECT_LOCAL, "kept")})
        other = LoadReport(entries={"a": _entry("a", AgentPriority.USER_CLAUDE, "lost")})

        report.merge(other)

        assert report.entries["a"].definition.description == "kept"

    def test_a_tie_keeps_the_existing_entry(self):
        report = LoadReport(entries={"a": _entry("a", AgentPriority.PROJECT_LOCAL, "first")})
        other = LoadReport(entries={"a": _entry("a", AgentPriority.PROJECT_LOCAL, "second")})

        report.merge(other)

        assert report.entries["a"].definition.description == "first"

    def test_new_names_are_added_and_errors_are_appended_in_order(self):
        report = LoadReport(errors=[LoadError(path=Path("one.md"), reason="bad")])
        other = LoadReport(
            entries={"b": _entry("b", AgentPriority.USER_CLAUDE)},
            errors=[LoadError(path=Path("two.md"), reason="worse")],
        )

        report.merge(other)

        assert set(report.entries) == {"b"}
        assert [e.path.name for e in report.errors] == ["one.md", "two.md"]

    def test_merging_an_empty_report_changes_nothing(self):
        report = LoadReport(entries={"a": _entry("a", AgentPriority.USER_CLAUDE)})

        report.merge(LoadReport())

        assert list(report.entries) == ["a"]
        assert report.errors == []


class _Db:
    async def get_session(self, *a, **k): ...
    async def upsert_session(self, *a, **k): ...
    async def close(self): ...


class TestTheBuildContext:
    def test_it_requires_the_shared_supervisor(self):
        with pytest.raises(ValidationError, match="supervisor"):
            AgentBuildContext()  # type: ignore[call-arg]

    def test_a_real_db_is_accepted(self):
        db = _Db()
        ctx = AgentBuildContext(db=db, supervisor=ProcessSupervisor())

        assert ctx.db is db
        assert isinstance(db, DbHandle)

    @pytest.mark.parametrize("bad", [object(), "db", 3, {"get_session": 1}])
    def test_anything_that_is_not_a_db_is_refused(self, bad):
        with pytest.raises(ValidationError):
            AgentBuildContext(db=bad, supervisor=ProcessSupervisor())

    def test_a_db_missing_one_member_is_refused(self):
        class Half:
            async def get_session(self, *a, **k): ...
            async def upsert_session(self, *a, **k): ...

        with pytest.raises(ValidationError):
            AgentBuildContext(db=Half(), supervisor=ProcessSupervisor())

    def test_the_mcp_protocol_wants_its_members(self):
        class Client:
            initialized = True

            async def connect(self, *a, **k): ...
            async def close(self): ...

        assert isinstance(Client(), McpClient)
        assert not isinstance(object(), McpClient)


def _args(**over) -> AgentConstructorArgs:
    base = dict(
        name="a",
        model="m",
        description="d",
        instructions=["one"],
        tools=None,
        num_history_runs=3,
        retries=2,
    )
    base.update(over)
    return AgentConstructorArgs(**base)  # type: ignore[arg-type]


class TestTheConstructorKwargs:
    def test_the_plain_set(self):
        kwargs = _args().to_agno_kwargs()

        assert kwargs == {
            "name": "a",
            "model": "m",
            "description": "d",
            "instructions": ["one"],
            "tools": None,
            "markdown": True,
            "num_history_runs": 3,
            "retries": 2,
            "telemetry": AgnoTelemetry.CONSTRUCTOR_VALUE,
        }

    def test_several_instructions_become_the_one_trained_block(self):
        kwargs = _args(instructions=["x", "y"]).to_agno_kwargs()

        assert kwargs["markdown"] is False
        assert len(kwargs["instructions"]) == 1
        assert kwargs["instructions"][0].startswith("- x\n- y\n<additional_information>")

    def test_no_instructions_pass_through(self):
        assert _args(instructions=None).to_agno_kwargs()["instructions"] is None
        assert _args(instructions=[], markdown=False).to_agno_kwargs()["markdown"] is False

    def test_the_db_key_exists_only_with_a_db(self):
        db = _Db()

        assert "db" not in _args().to_agno_kwargs()
        assert _args(db=db).to_agno_kwargs()["db"] is db

    def test_hooks_are_passed_only_when_there_are_some(self):
        hook = object()

        assert "tool_hooks" not in _args(tool_hooks=None).to_agno_kwargs()
        assert "tool_hooks" not in _args(tool_hooks=[]).to_agno_kwargs()
        assert _args(tool_hooks=[hook]).to_agno_kwargs()["tool_hooks"] == [hook]

    def test_reasoning_off_drops_its_steps(self):
        kwargs = _args(reasoning=False, reasoning_min_steps=2, reasoning_max_steps=5)
        kwargs = kwargs.to_agno_kwargs()

        assert not {"reasoning", "reasoning_min_steps", "reasoning_max_steps"} & set(kwargs)

    def test_reasoning_on_carries_its_steps_even_when_unset(self):
        on = _args(reasoning=True, reasoning_min_steps=2, reasoning_max_steps=5).to_agno_kwargs()
        unset = _args(reasoning=True).to_agno_kwargs()

        assert (on["reasoning"], on["reasoning_min_steps"], on["reasoning_max_steps"]) == (
            True,
            2,
            5,
        )
        assert unset["reasoning_min_steps"] is None
        assert unset["reasoning_max_steps"] is None
