"""What :class:`AgentBuilder` hands Agno for one agent definition.

Each test builds an agent from a real model registry and tool registry and
reads back what Agno's ``Agent`` is constructed with: which model (and its
per-agent overrides), which tools (including the MCP servers the agent may
see), and the instructions it is given. Agno's ``Agent`` itself is swapped
for a recorder through the builder's ``_agent_cls`` seam.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.agents.builder import AgentBuilder
from igni_core.core.agents.schemas import AgentBuildContext, AgentDefinition
from igni_core.core.agents.tool_audience import ToolAudience
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.schedule import ScheduleTools


class FakeMcp:
    """Stands in for a connected MCP server's client."""

    initialized = True

    async def connect(self, *args: Any, **kwargs: Any) -> None: ...

    async def close(self) -> None: ...


def _entry(model_id: str) -> ModelRegistryEntry:
    return ModelRegistryEntry(
        provider="openai_like",
        model_id=model_id,
        url="https://models.example.test/v1",
        api_key="k",
        context_window=8_000,
        vision=False,
    )


class _Recorder:
    """Keeps the keyword arguments the builder constructed an agent with."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _record_the_agent(monkeypatch):
    monkeypatch.setattr(AgentBuilder, "_agent_cls", _Recorder)


def _build(
    settings: Any,
    definition: AgentDefinition,
    *,
    base_dir: str | None = None,
    mcp_clients: dict[str, Any] | None = None,
    audience: ToolAudience | None = None,
) -> Any:
    context = AgentBuildContext(
        settings=settings,
        base_dir=base_dir,
        mcp_clients=mcp_clients,
        supervisor=ProcessSupervisor(),
    )
    return _view(AgentBuilder(context, audience=audience).build(definition))


def _view(recorded: _Recorder) -> SimpleNamespace:
    kwargs = recorded.kwargs
    return SimpleNamespace(
        tools=kwargs.get("tools"),
        instructions=kwargs.get("instructions"),
        model=kwargs["model"],
        kwargs=kwargs,
    )


def _definition(**fields: Any) -> AgentDefinition:
    fields.setdefault("name", "worker")
    fields.setdefault("description", "Does work")
    return AgentDefinition(**fields)


@pytest.fixture
def two_models(settings):
    settings.models.registry["other"] = _entry("vendor/other")
    return settings


class TestTheModel:
    def test_no_model_means_the_default(self, two_models) -> None:
        two_models.models.default = "other"
        agent = _build(two_models, _definition(model=None))
        assert agent.model.id == "vendor/other"

    def test_an_empty_model_means_the_default(self, two_models) -> None:
        two_models.models.default = "other"
        agent = _build(two_models, _definition(model=""))
        assert agent.model.id == "vendor/other"

    def test_the_legacy_sentinel_means_the_default_not_itself(self, two_models) -> None:
        two_models.models.default = "other"
        agent = _build(two_models, _definition(model="MiniMax-M2.7"))
        assert agent.model.id == "vendor/other"

    def test_a_named_model_is_used(self, two_models) -> None:
        agent = _build(two_models, _definition(model="other"))
        assert agent.model.id == "vendor/other"

    def test_an_unknown_model_is_an_error_not_a_silent_fallback(self, two_models) -> None:
        with pytest.raises(Exception, match="nope"):
            _build(two_models, _definition(model="nope"))

    def test_temperature_and_max_tokens_land_on_the_model(self, two_models) -> None:
        agent = _build(two_models, _definition(temperature=0.3, max_tokens=321))
        assert agent.model.temperature == 0.3
        assert agent.model.max_tokens == 321

    def test_a_temperature_of_zero_is_still_applied(self, two_models) -> None:
        agent = _build(two_models, _definition(temperature=0.0))
        assert agent.model.temperature == 0.0

    def test_without_overrides_the_registry_values_stand(self, two_models) -> None:
        plain = _build(two_models, _definition())
        tuned = _build(two_models, _definition(temperature=0.9, max_tokens=5))
        assert tuned.model.temperature == 0.9
        assert plain.model.temperature != 0.9
        assert plain.model.max_tokens != 5


class TestTheAgentItself:
    def test_it_carries_the_definition_and_the_fixed_settings(self, settings) -> None:
        kw = _build(settings, _definition(name="reviewer", description="Reviews")).kwargs
        assert kw["name"] == "reviewer"
        assert kw["description"] == "Reviews"
        assert kw["retries"] == 0
        assert kw["num_history_runs"] == settings.storage.max_history_runs

    def test_reasoning_bounds_are_passed_only_when_reasoning_is_on(self, settings) -> None:
        on = _build(
            settings,
            _definition(reasoning=True, reasoning_min_steps=2, reasoning_max_steps=7),
        ).kwargs
        off = _build(
            settings,
            _definition(reasoning=False, reasoning_min_steps=2, reasoning_max_steps=7),
        ).kwargs
        assert on["reasoning"] is True
        assert (on["reasoning_min_steps"], on["reasoning_max_steps"]) == (2, 7)
        assert "reasoning" not in off
        assert "reasoning_min_steps" not in off
        assert "reasoning_max_steps" not in off

    def test_the_context_can_be_swapped(self, settings) -> None:
        first = AgentBuildContext(settings=settings, supervisor=ProcessSupervisor())
        second = first.model_copy(update={"base_dir": "/somewhere"})
        builder = AgentBuilder(first)
        assert builder.context is first
        builder.replace_context(second)
        assert builder.context is second
        agent = _view(builder.build(_definition(system_prompt="Be brief.")))
        assert agent.instructions == [
            "- Be brief.\n- Working directory: /somewhere\n<additional_information>\n- Use markdown to format your answers.\n</additional_information>"
        ]


class TestTheTools:
    def test_no_tools_means_no_tools_and_no_mcp(self, settings) -> None:
        agent = _build(settings, _definition(tools=[]), mcp_clients={"srv": FakeMcp()})
        assert not agent.tools

    def test_tools_resolve_through_the_registry_with_the_scheduler_appended(
        self, settings, tmp_path
    ) -> None:
        agent = _build(settings, _definition(tools=["Bash"]), base_dir=str(tmp_path))
        assert isinstance(agent.tools[-1], ScheduleTools)
        assert len(agent.tools) == 2

    def test_an_unknown_tool_name_is_refused(self, settings) -> None:
        with pytest.raises(ValueError, match="NoSuchTool"):
            _build(settings, _definition(tools=["NoSuchTool"]))

    def test_every_connected_server_when_no_whitelist(self, settings) -> None:
        a, b = FakeMcp(), FakeMcp()
        agent = _build(settings, _definition(tools=["Bash"]), mcp_clients={"a": a, "b": b})
        assert a in agent.tools and b in agent.tools

    def test_a_whitelist_naming_an_absent_server_attaches_nothing(self, settings) -> None:
        a = FakeMcp()
        agent = _build(
            settings,
            _definition(tools=["Bash"], mcp_servers=["missing"]),
            base_dir="/proj",
            mcp_clients={"a": a},
        )
        assert a not in agent.tools
        assert agent.instructions == ["Working directory: /proj"]

    def test_no_clients_at_all_attaches_nothing(self, settings) -> None:
        agent = _build(settings, _definition(tools=["Bash"]), mcp_clients={})
        assert len(agent.tools) == 2

    def test_a_server_offered_to_other_agents_only_is_withheld(self, settings, tmp_path) -> None:
        own = tmp_path / "plugins" / "ext"
        own.mkdir(parents=True)
        audience = ToolAudience()
        audience.restrict("docs", own)
        docs, other = FakeMcp(), FakeMcp()
        clients = {"docs": docs, "other": other}

        outsider = _build(
            settings,
            _definition(tools=["Bash"], source_path=tmp_path / "x.md"),
            mcp_clients=clients,
            audience=audience,
        )
        faceless = _build(
            settings, _definition(tools=["Bash"]), mcp_clients=clients, audience=audience
        )
        insider = _build(
            settings,
            _definition(tools=["Bash"], source_path=own / "agents" / "a.md"),
            mcp_clients=clients,
            audience=audience,
        )

        assert docs not in outsider.tools and other in outsider.tools
        assert docs not in faceless.tools
        assert docs in insider.tools and other in insider.tools

    def test_a_withheld_server_is_not_named_in_the_instructions(self, settings, tmp_path) -> None:
        audience = ToolAudience()
        audience.restrict("docs", tmp_path / "ext")
        agent = _build(
            settings,
            _definition(tools=["Bash"], source_path=tmp_path / "x.md"),
            base_dir="/proj",
            mcp_clients={"docs": FakeMcp(), "other": FakeMcp()},
            audience=audience,
        )
        text = " ".join(agent.instructions)
        assert "MCP tools from: other. Project path" in text
        assert "docs" not in text

    def test_a_client_already_among_the_tools_is_not_added_twice(
        self, settings, monkeypatch
    ) -> None:
        client = FakeMcp()

        class Registry:
            def __init__(self, **_: Any) -> None:
                pass

            def resolve(self, _names: Any) -> list[Any]:
                return [client]

        monkeypatch.setattr(AgentBuilder, "_tool_registry_cls", Registry)
        agent = _build(settings, _definition(tools=["Bash"]), mcp_clients={"a": client})
        assert agent.tools.count(client) == 1


class TestTheSharedState:
    def test_the_registry_gets_the_contexts_supervisor_directory_and_broadcast(
        self, settings, monkeypatch
    ) -> None:
        seen: dict[str, Any] = {}

        class Registry:
            def __init__(self, **kwargs: Any) -> None:
                seen.update(kwargs)

            def resolve(self, _names: Any) -> list[Any]:
                return []

        monkeypatch.setattr(AgentBuilder, "_tool_registry_cls", Registry)
        supervisor = ProcessSupervisor()
        context = AgentBuildContext(
            settings=settings, base_dir="/proj", supervisor=supervisor, broadcast=None
        )
        AgentBuilder(context).build(_definition(tools=["Bash"]))
        assert seen["supervisor"] is supervisor
        assert seen["base_dir"] == "/proj"
        assert seen["broadcast"] is None

    def test_the_context_db_reaches_the_agent_and_none_means_no_db_key(self, settings) -> None:
        class Db:
            async def get_session(self, *args: Any, **kwargs: Any) -> Any: ...

            async def upsert_session(self, *args: Any, **kwargs: Any) -> Any: ...

            async def close(self) -> None: ...

        db = Db()
        with_db = AgentBuildContext(settings=settings, supervisor=ProcessSupervisor(), db=db)
        without = AgentBuildContext(settings=settings, supervisor=ProcessSupervisor())
        assert _view(AgentBuilder(with_db).build(_definition())).kwargs["db"] is db
        assert "db" not in _view(AgentBuilder(without).build(_definition())).kwargs


class TestTheInstructions:
    def test_prompt_then_working_directory_then_the_mcp_hint(self, settings) -> None:
        agent = _build(
            settings,
            _definition(tools=["Bash"], system_prompt="You review."),
            base_dir="/proj",
            mcp_clients={"a": FakeMcp(), "b": FakeMcp()},
        )
        text = agent.instructions[0]
        parts = [
            "- You review.\n",
            "- Working directory: /proj\n",
            "- You have MCP tools from: a, b. Project path: /proj\n"
            "If an MCP tool returns empty/no data, do NOT retry with different arguments. "
            "Report what happened and ask the user.\n",
        ]
        assert text.startswith("".join(parts))

    def test_with_nothing_to_say_there_are_no_instructions(self, settings) -> None:
        agent = _build(settings, _definition(system_prompt=""), base_dir=None)
        assert agent.instructions is None

    def test_an_empty_base_dir_gives_no_working_directory_line(self, settings) -> None:
        agent = _build(settings, _definition(system_prompt="Hi."), base_dir="")
        assert agent.instructions == ["Hi."]

    def test_the_working_directory_alone_is_enough(self, settings, tmp_path: Path) -> None:
        agent = _build(settings, _definition(system_prompt=""), base_dir=str(tmp_path))
        assert agent.instructions == [f"Working directory: {tmp_path}"]
