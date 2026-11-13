"""The tools-build bags are frozen, default to absent, and build session toolkits.

What this file pins, one claim per test:

* the cloud credentials default to no token and no server, and an empty
  token is kept as given rather than turned into absence;
* every bag refuses assignment after construction, and refuses to be
  built without a required field;
* the optional fields of the source and top bags default to ``None``;
* ``for_session`` builds the five session toolkits, each bound to the
  session it was given, and lets a failing constructor's error out;
* the module exports exactly the six bags.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from igni_core.core.session.agent_builder import tools_builder_deps as deps
from igni_core.core.session.agent_builder.tools_builder_deps import (
    CloudCredentials,
    OrchestrationDeps,
    SessionToolkits,
    ToolBuildDeps,
    ToolRegistryDeps,
    ToolSourceDeps,
)
from igni_core.core.tools.loop import LoopTools
from igni_core.core.tools.loop_progress import LoopProgressTool
from igni_core.core.tools.plan import PlanTool
from igni_core.core.tools.slash import SlashCommandTool
from igni_core.core.tools.todo import TodoTools


def test_cloud_credentials_default_to_no_token_and_no_server():
    creds = CloudCredentials()
    assert creds.token is None
    assert creds.server_url is None


def test_an_empty_token_is_kept_as_given():
    assert CloudCredentials(token="").token == ""


def test_cloud_credentials_refuse_assignment():
    creds = CloudCredentials(token="t")
    with pytest.raises(FrozenInstanceError):
        creds.token = "other"  # type: ignore[misc]


def test_the_source_bag_refuses_assignment_and_defaults_the_group_dir():
    sources = ToolSourceDeps(plugin_loader=object(), disabled_plugins=set())  # type: ignore[arg-type]
    assert sources.group_tools_dir is None
    with pytest.raises(FrozenInstanceError):
        sources.group_tools_dir = Path(".")  # type: ignore[misc]


def test_the_source_bag_needs_a_loader_and_a_disabled_set():
    with pytest.raises(TypeError):
        ToolSourceDeps(plugin_loader=object())  # type: ignore[call-arg,arg-type]


def test_the_registry_bag_needs_the_supervisor():
    with pytest.raises(TypeError):
        ToolRegistryDeps(  # type: ignore[call-arg]
            project_dir=Path("."),
            settings=object(),  # type: ignore[arg-type]
            permissions_cls=object,  # type: ignore[arg-type]
            registry_cls=object,  # type: ignore[arg-type]
            cloud=CloudCredentials(),
            broadcast=object(),  # type: ignore[arg-type]
        )


def test_the_top_bag_defaults_lsp_and_monitor_to_none_and_is_frozen():
    toolkits = SessionToolkits.for_session(SimpleNamespace())
    bag = ToolBuildDeps(
        registry=object(),  # type: ignore[arg-type]
        sources=object(),  # type: ignore[arg-type]
        orchestration=object(),  # type: ignore[arg-type]
        mcp_manager=object(),  # type: ignore[arg-type]
        reasoning_factory=lambda s: None,
        resolve_tool_names=lambda r: [],
        session_toolkits=toolkits,
    )
    assert bag.lsp_manager is None
    assert bag.monitor_manager is None
    with pytest.raises(FrozenInstanceError):
        bag.lsp_manager = object()  # type: ignore[misc,assignment]


def test_the_top_bag_and_orchestration_bag_need_every_required_field():
    with pytest.raises(TypeError):
        ToolBuildDeps()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        OrchestrationDeps(pool=object(), session_id="s")  # type: ignore[call-arg,arg-type]


def test_no_bag_field_is_the_session():
    for bag in (
        CloudCredentials,
        ToolRegistryDeps,
        ToolSourceDeps,
        OrchestrationDeps,
        SessionToolkits,
        ToolBuildDeps,
    ):
        assert "session" not in {f.name for f in fields(bag)}


def test_for_session_builds_the_five_toolkits_bound_to_that_session():
    session = SimpleNamespace()
    toolkits = SessionToolkits.for_session(session)
    assert isinstance(toolkits.loop, LoopTools)
    assert isinstance(toolkits.loop_progress, LoopProgressTool)
    assert isinstance(toolkits.todo, TodoTools)
    assert isinstance(toolkits.plan, PlanTool)
    assert isinstance(toolkits.slash, SlashCommandTool)
    for kit in (
        toolkits.loop,
        toolkits.loop_progress,
        toolkits.todo,
        toolkits.plan,
        toolkits.slash,
    ):
        assert kit._session is session


def test_a_failing_toolkit_constructor_is_not_swallowed():
    with (
        patch.object(deps, "PlanTool", side_effect=RuntimeError("no plan")),
        pytest.raises(RuntimeError, match="no plan"),
    ):
        SessionToolkits.for_session(SimpleNamespace())


def test_the_module_exports_exactly_the_six_bags():
    assert sorted(deps.__all__) == [
        "CloudCredentials",
        "OrchestrationDeps",
        "SessionToolkits",
        "ToolBuildDeps",
        "ToolRegistryDeps",
        "ToolSourceDeps",
    ]
