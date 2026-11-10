"""The main agent's tools come from typed bags, and the builder never sees the session.

The tools builder took twenty keyword parameters and kept twenty-one
fields — one per collaborator the session owned — and it took the whole
``Session`` *alongside* attributes copied off that same session. So the
"session-free" claim in its docstring was never true, and the
constructor was a list of collaborators rather than a description of what
the builder actually needs.

What this file pins, one claim per test:

* the builder takes one typed bag, and no field of that bag holds the
  session;
* the session-facing toolkits arrive already built, so the session
  never reaches the builder at all;
* the assembly order is the one the pre-refactor procedural version
  used — the order carried meaning, so it is not free to change, and
  the MCP clients come after the core toolkits but before the custom
  ones;
* a connected MCP client is attached exactly once: whether it is
  reached under two names, or was already pulled in as a core tool;
* the registry is built from the bag — the cloud credentials, the
  settings permissions and the project directory all come from it;
* the tool-name resolver is handed the registry the builder built, not
  the bag it built it from;
* the custom toolkits are looked for where the bag says, not where a
  session would say;
* the coordinator fills the bag from the session — the group tools
  directory, the cloud token, the broadcast sink and the session id
  all travel in it.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_type_hints

from agno.tools import Toolkit

from igni_core.core.agents import AgentPool
from igni_core.core.config.schemas.permissions import PermissionsConfig
from igni_core.core.config.settings import Settings
from igni_core.core.config.tool_permissions import ToolPermissions
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.lsp import LspServerManager
from igni_core.core.lsp.schemas import LspServerConfig
from igni_core.core.mcp.client import MCPClientManager
from igni_core.core.monitors import MonitorManager
from igni_core.core.monitors.config import MonitorConfig
from igni_core.core.plugins import PluginLoader
from igni_core.core.prompts.fragments import PromptFragments
from igni_core.core.prompts.loader import PromptLoader
from igni_core.core.session.agent_builder.coordinator import MainAgentBuilder
from igni_core.core.session.agent_builder.main_tools import MainToolSet
from igni_core.core.session.agent_builder.tools_builder import ToolsBuilder
from igni_core.core.session.agent_builder.tools_builder_deps import (
    CloudCredentials,
    OrchestrationDeps,
    SessionToolkits,
    ToolBuildDeps,
    ToolRegistryDeps,
    ToolSourceDeps,
)
from igni_core.core.session.core import Session
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.core.tools.custom_loader_schemas import DiscoveryResult, PluginToolDir
from igni_core.core.tools.orchestrate_budget import SpawnBudget
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.registry import ToolRegistry

#: The toolkits the stub registry hands back, so the assembled list can be
#: read by name rather than by position alone.
_CORE = Toolkit(name="core-marker")
_CUSTOM = Toolkit(name="custom-marker")
_MCP_CLIENT = Toolkit(name="mcp-marker")

#: What the coordinator's injected reasoning factory hands back, so the
#: test can tell the injected factory from any other.
_REASONING = Toolkit(name="reasoning-marker")


def _broadcast(event) -> None:
    """A broadcast sink that drops the event. The builder never emits."""


class _Stubs:
    """What the stub collaborators recorded, for the test to look at.

    One instance per test, so no state leaks between them.
    """

    def __init__(self) -> None:
        self.registries: list[ToolRegistry] = []
        self.registry_kwargs: list[dict[str, Any]] = []
        self.permission_kwargs: list[dict[str, Any]] = []
        self.resolved_with: list[ToolRegistry] = []
        self.group_tools_dirs: list[Path | None] = []
        self.plugin_tool_dirs: list[list[PluginToolDir]] = []
        self.disabled_seen: list[set[str] | None] = []
        self.reasoning_settings: list[Settings] = []
        self.core_tools: list[Toolkit] = [_CORE]
        self.custom_tools: list[Toolkit] = [_CUSTOM]


def _stub_registry_class(stubs: _Stubs) -> type[ToolRegistry]:
    """A registry class that answers with markers and records itself.

    The builder constructs the registry itself, so the test cannot hand
    one in — the class records what it built for the test to look at.
    """

    class _StubRegistry(ToolRegistry):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            stubs.registries.append(self)
            stubs.registry_kwargs.append(kwargs)

        def resolve(self, tool_names):
            return list(stubs.core_tools)

        def load_custom_tools(
            self,
            project_dir=None,
            *,
            plugin_tool_dirs=None,
            group_tools_dir=None,
        ):
            stubs.group_tools_dirs.append(group_tools_dir)
            stubs.plugin_tool_dirs.append(list(plugin_tool_dirs or []))
            return DiscoveryResult(toolkits=list(stubs.custom_tools))

    return _StubRegistry


def _stub_permissions_class(stubs: _Stubs) -> type[ToolPermissions]:
    """A permissions class that records the settings it was built with."""

    class _StubPermissions(ToolPermissions):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            stubs.permission_kwargs.append(kwargs)

    return _StubPermissions


class _StubPluginLoader(PluginLoader):
    """A plugin loader that ships one tools directory and scans no disk."""

    def __init__(self, stubs: _Stubs | None = None) -> None:
        super().__init__(Path("/nonexistent"))
        self._stubs = stubs

    def collect_tool_dirs(self, disabled=None):
        if self._stubs is not None:
            self._stubs.disabled_seen.append(disabled)
        return [PluginToolDir(plugin_name="alpha", tools_dir=Path("/plugins/alpha"))]


class _StubMcpManager(MCPClientManager):
    """An MCP manager whose connected servers map to given clients."""

    def __init__(self, clients: dict[str, Toolkit | None]) -> None:
        super().__init__(None)
        self._clients = clients

    def list_connected(self) -> list[str]:
        return list(self._clients)

    def get_client(self, name):
        return self._clients.get(name)


def _deps(
    project_dir: Path,
    stubs: _Stubs,
    *,
    settings: Settings | None = None,
    cloud: CloudCredentials | None = None,
    mcp_manager: MCPClientManager | None = None,
    lsp_manager: LspServerManager | None = None,
    monitor_manager: MonitorManager | None = None,
    group_tools_dir: Path | None = None,
    reasoning: Toolkit | None = None,
    session_toolkits: SessionToolkits | None = None,
    tool_names: list[str] | None = None,
) -> ToolBuildDeps:
    """The builder's dependency bag, as the coordinator builds it."""

    def _resolve(registry: ToolRegistry) -> list[str]:
        stubs.resolved_with.append(registry)
        return list(tool_names) if tool_names is not None else ["Bash"]

    def _reasoning(bag_settings: Settings) -> Toolkit | None:
        stubs.reasoning_settings.append(bag_settings)
        return reasoning

    return ToolBuildDeps(
        registry=ToolRegistryDeps(
            project_dir=project_dir,
            settings=settings if settings is not None else Settings(),
            permissions_cls=_stub_permissions_class(stubs),
            registry_cls=_stub_registry_class(stubs),
            cloud=cloud if cloud is not None else CloudCredentials(),
            broadcast=_broadcast,
            supervisor=ProcessSupervisor(),
        ),
        sources=ToolSourceDeps(
            plugin_loader=_StubPluginLoader(stubs),
            disabled_plugins={"off-plugin"},
            group_tools_dir=group_tools_dir,
        ),
        orchestration=OrchestrationDeps(
            pool=AgentPool(),
            hook_executor=HookExecutor({}),
            session_id="session-1",
            spawn_budget=SpawnBudget(4),
            sub_agent_hitl=SubAgentHITLCoordinator(),
        ),
        mcp_manager=mcp_manager if mcp_manager is not None else _StubMcpManager({}),
        lsp_manager=lsp_manager,
        monitor_manager=monitor_manager,
        reasoning_factory=_reasoning,
        resolve_tool_names=_resolve,
        session_toolkits=(
            session_toolkits
            if session_toolkits is not None
            else SessionToolkits.for_session(SimpleNamespace())
        ),
    )


class _FakeGroupPolicy:
    """A group policy that knows one tools directory."""

    def __init__(self, tools_dir: Path) -> None:
        self._tools_dir = tools_dir

    def dir_for(self, kind: str) -> Path:
        assert kind == "tools", kind
        return self._tools_dir


class _FakeSession:
    """The session surface the coordinator reads when it fills the bag."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.settings = Settings(permissions=PermissionsConfig(web_search="deny", web_fetch="deny"))
        self.cloud_auth = SimpleNamespace(
            access_token="tok-42",
            server_url="https://cloud.example",
        )
        self.broadcast = _broadcast
        # The supervisor the session's shell toolkit runs on.
        self.supervisor = ProcessSupervisor()
        self.plugin_loader = _StubPluginLoader()
        self.disabled_plugins = {"off"}
        self.group_policy = _FakeGroupPolicy(project_dir / "group-tools")
        self.pool = AgentPool()
        self.hook_executor = HookExecutor({})
        self.session_id = "session-42"
        self.spawn_budget = SpawnBudget(4)
        self.sub_agent_hitl = SubAgentHITLCoordinator()
        self.mcp_manager = _StubMcpManager({})
        self.lsp_manager = None
        self.monitor_manager = None
        # The loop toolkit asks the session's loop controller.
        self.loop = SimpleNamespace(loop_status_from_tool=self.loop_status_from_tool)

    def loop_status_from_tool(self):
        """What the loop toolkit asks the session for."""
        return SimpleNamespace(render=lambda: "loop-status-of-this-session")


def _coordinator(session: _FakeSession) -> MainAgentBuilder:
    """A coordinator wired to ``session`` with the collaborators the test needs."""
    return MainAgentBuilder(
        session,
        agent_cls=object,
        registry_cls=ToolRegistry,
        permissions_cls=ToolPermissions,
        compression_cls=object,
        model_registry_cls=object,
        reasoning_factory=lambda settings: _REASONING,
        guardrails_factory=lambda settings: [],
        prompt_loader=PromptLoader(PromptFragments()),
    )


class TestTheBuilderTakesOneBag:
    def test_the_constructor_takes_the_bag_and_nothing_else(self):
        """Twenty keyword parameters became one typed bag."""
        signature = inspect.signature(ToolsBuilder.__init__, eval_str=True)

        assert list(signature.parameters) == ["self", "deps"]
        assert signature.parameters["deps"].annotation is ToolBuildDeps

    def test_no_field_of_the_bag_holds_the_session(self):
        """The one session-shaped field holds *toolkits*, not the session."""
        for bag in (
            ToolBuildDeps,
            ToolRegistryDeps,
            ToolSourceDeps,
            OrchestrationDeps,
            SessionToolkits,
        ):
            for name, hint in get_type_hints(bag).items():
                assert hint is not Session, f"{bag.__name__}.{name} holds the session"

    def test_the_builder_module_does_not_import_the_session(self):
        """A module that can import the session will, one refactor later,
        take the session again."""
        source = Path(inspect.getfile(ToolsBuilder)).read_text()

        assert "from igni_core.core.session" not in source
        assert "Session(" not in source


class TestTheAssembly:
    def test_the_families_come_in_the_canonical_order(self, tmp_path):
        """The order carried meaning: custom tools last, so they can
        shadow built-ins; MCP after the core toolkits, so their names
        don't collide with same-named built-ins."""
        builder = ToolsBuilder(
            _deps(
                tmp_path,
                _Stubs(),
                reasoning=_REASONING,
                mcp_manager=_StubMcpManager({"stub": _MCP_CLIENT}),
                lsp_manager=LspServerManager(
                    {"stub": LspServerConfig(name="stub", command="stub-lsp")}, tmp_path
                ),
                monitor_manager=MonitorManager(
                    {"stub": MonitorConfig(name="stub", command="stub-monitor")}, tmp_path
                ),
            )
        )

        assert [tool.name for tool in builder.assemble()] == [
            "core-marker",
            "ember_orchestrate",
            "reasoning-marker",
            "ember_loop",
            "ember_loop_progress",
            "ember_todo",
            "ember_plan",
            "ember_slash_command",
            "ember_lsp",
            "ember_monitors",
            "mcp-marker",
            "custom-marker",
        ]

    def test_a_session_facing_toolkit_is_the_one_the_session_handed_over(self, tmp_path):
        """The builder never builds these — the coordinator does, against
        the session, and hands them over finished."""
        toolkits = SessionToolkits.for_session(SimpleNamespace())

        tools = ToolsBuilder(_deps(tmp_path, _Stubs(), session_toolkits=toolkits)).assemble()

        assert toolkits.loop in tools
        assert toolkits.loop_progress in tools
        assert toolkits.todo in tools
        assert toolkits.plan in tools
        assert toolkits.slash in tools

    def test_the_lsp_and_monitor_toolkits_appear_only_when_configured(self, tmp_path):
        """Both are conditional: a session with no language server and no
        monitor gets neither toolkit."""
        bare = ToolsBuilder(_deps(tmp_path, _Stubs())).assemble()
        assert [tool.name for tool in bare].count("ember_lsp") == 0
        assert [tool.name for tool in bare].count("ember_monitors") == 0

        configured = ToolsBuilder(
            _deps(
                tmp_path,
                _Stubs(),
                lsp_manager=LspServerManager(
                    {"stub": LspServerConfig(name="stub", command="stub-lsp")}, tmp_path
                ),
                monitor_manager=MonitorManager(
                    {"stub": MonitorConfig(name="stub", command="stub-monitor")}, tmp_path
                ),
            )
        ).assemble()

        names = [tool.name for tool in configured]
        assert names.index("ember_lsp") < names.index("ember_monitors")
        assert names.index("ember_monitors") < names.index("custom-marker")

    def test_the_same_client_connected_under_two_names_is_attached_once(self, tmp_path):
        """One client behind two connected names is still one client: it is
        attached once, not once per name."""
        builder = ToolsBuilder(
            _deps(
                tmp_path,
                _Stubs(),
                mcp_manager=_StubMcpManager({"first": _MCP_CLIENT, "second": _MCP_CLIENT}),
            )
        )

        tools = builder.assemble()

        assert [tool.name for tool in tools].count("mcp-marker") == 1

    def test_an_mcp_client_that_is_already_a_core_tool_is_not_attached_again(self, tmp_path):
        """A client that was already pulled in with the core toolkits is not
        attached a second time."""
        stubs = _Stubs()
        stubs.core_tools = [_MCP_CLIENT]
        builder = ToolsBuilder(
            _deps(tmp_path, stubs, mcp_manager=_StubMcpManager({"stub": _MCP_CLIENT}))
        )

        tools = builder.assemble()

        assert [tool.name for tool in tools].count("mcp-marker") == 1

    def test_a_connected_server_without_a_client_is_skipped(self, tmp_path):
        """A name with no client behind it contributes nothing."""
        builder = ToolsBuilder(
            _deps(tmp_path, _Stubs(), mcp_manager=_StubMcpManager({"stub": None}))
        )

        tools = builder.assemble()

        assert [tool.name for tool in tools].count("mcp-marker") == 0

    def test_the_custom_toolkits_are_looked_for_where_the_bag_says(self, tmp_path):
        """The organisation's tools directory travels in the bag; the
        builder has no session to read it off."""
        stubs = _Stubs()
        group_tools = tmp_path / "group-tools"

        builder = ToolsBuilder(
            _deps(tmp_path, stubs, group_tools_dir=group_tools),
        )
        builder.assemble()

        assert stubs.group_tools_dirs == [group_tools]

    def test_the_custom_toolkits_are_looked_for_in_the_plugin_directories(self, tmp_path):
        """The plugin tool directories, and the plugins the session has
        switched off, travel in the bag too."""
        stubs = _Stubs()
        builder = ToolsBuilder(_deps(tmp_path, stubs))

        builder.assemble()

        assert stubs.plugin_tool_dirs == [
            [PluginToolDir(plugin_name="alpha", tools_dir=Path("/plugins/alpha"))]
        ]
        assert stubs.disabled_seen == [{"off-plugin"}]

    def test_the_delegation_surface_is_built_from_the_bag(self, tmp_path):
        """The orchestration toolkit is built from the bag's collaborators."""
        stubs = _Stubs()
        deps = _deps(tmp_path, stubs)
        builder = ToolsBuilder(deps)

        tools = builder.orchestrate()

        assert len(tools) == 1
        toolkit = tools[0]
        assert toolkit.pool is deps.orchestration.pool
        assert toolkit.settings is deps.registry.settings
        # The rest of the delegation surface travels in the toolkit's
        # two bundles; reading it here pins the wiring instead of
        # assuming it.
        assert toolkit.wiring.session_id == deps.orchestration.session_id
        assert toolkit.scope.project_dir == deps.registry.project_dir
        assert toolkit.wiring.hook_executor is deps.orchestration.hook_executor
        assert toolkit.scope.budget is deps.orchestration.spawn_budget
        assert toolkit.wiring.sinks.hitl_coordinator is deps.orchestration.sub_agent_hitl

    def test_the_registry_is_built_once_for_the_whole_assembly(self, tmp_path):
        """Core tools and custom tools share one registry."""
        stubs = _Stubs()

        ToolsBuilder(_deps(tmp_path, stubs)).assemble()

        assert len(stubs.registries) == 1


class TestTheRegistryIsBuiltFromTheBag:
    def test_the_registry_is_built_with_the_cloud_credentials_from_the_bag(self, tmp_path):
        """The cloud account the cloud-routed tools talk to travels in the
        bag, and the registry is built with it."""
        stubs = _Stubs()
        builder = ToolsBuilder(
            _deps(
                tmp_path,
                stubs,
                cloud=CloudCredentials(token="tok-1", server_url="https://cloud.example"),
            )
        )

        builder.assemble()

        assert stubs.registry_kwargs[0]["cloud_token"] == "tok-1"
        assert stubs.registry_kwargs[0]["cloud_server_url"] == "https://cloud.example"

    def test_the_registry_is_built_with_the_settings_permissions_from_the_bag(self, tmp_path):
        """The permissions the registry enforces are the ones the bag's
        settings say, not a fresh default."""
        stubs = _Stubs()
        settings = Settings(permissions=PermissionsConfig(web_search="deny", web_fetch="deny"))
        builder = ToolsBuilder(_deps(tmp_path, stubs, settings=settings))

        builder.assemble()

        assert stubs.permission_kwargs[0]["settings_permissions"] is settings.permissions

    def test_the_registry_is_built_with_the_project_directory_from_the_bag(self, tmp_path):
        """The project directory the registry serves is the bag's."""
        stubs = _Stubs()
        builder = ToolsBuilder(_deps(tmp_path, stubs))

        builder.assemble()

        assert stubs.registry_kwargs[0]["base_dir"] == str(tmp_path)
        assert stubs.permission_kwargs[0]["project_dir"] == tmp_path

    def test_the_registry_is_built_with_the_broadcast_sink_from_the_bag(self, tmp_path):
        """The sink the registry pushes structured payloads through is the
        bag's, not a fresh default."""
        stubs = _Stubs()
        builder = ToolsBuilder(_deps(tmp_path, stubs))

        builder.assemble()

        assert stubs.registry_kwargs[0]["broadcast"] is _broadcast

    def test_the_tool_names_are_resolved_against_the_registry_the_builder_built(self, tmp_path):
        """The resolver is handed the built registry — the thing whose
        permissions decide which tools exist — not the bag it came from."""
        stubs = _Stubs()
        builder = ToolsBuilder(_deps(tmp_path, stubs))

        builder.assemble()

        assert stubs.resolved_with == [stubs.registries[0]]

    def test_the_reasoning_factory_is_asked_about_the_settings_from_the_bag(self, tmp_path):
        """The reasoning factory is handed the session's settings — the
        ones that say whether reasoning is on."""
        stubs = _Stubs()
        settings = Settings()
        builder = ToolsBuilder(_deps(tmp_path, stubs, settings=settings, reasoning=_REASONING))

        builder.assemble()

        assert stubs.reasoning_settings[0] is settings


class TestTheCoordinatorFillsTheBag:
    def test_the_coordinator_reads_the_group_tools_directory_off_the_session(self, tmp_path):
        """The organisation's tools directory is the session's, not a
        default."""
        session = _FakeSession(tmp_path)

        deps = _coordinator(session).tool_deps()

        assert deps.sources.group_tools_dir == tmp_path / "group-tools"

    def test_the_coordinator_reads_the_cloud_credentials_off_the_session(self, tmp_path):
        """The cloud account is the session's."""
        session = _FakeSession(tmp_path)

        deps = _coordinator(session).tool_deps()

        assert deps.registry.cloud.token == "tok-42"
        assert deps.registry.cloud.server_url == "https://cloud.example"

    def test_the_coordinator_reads_the_broadcast_sink_off_the_session(self, tmp_path):
        """The sink the registry pushes structured payloads through is the
        session's."""
        session = _FakeSession(tmp_path)

        deps = _coordinator(session).tool_deps()

        assert deps.registry.broadcast is session.broadcast

    def test_the_coordinator_reads_the_session_id_off_the_session(self, tmp_path):
        """The delegation surface is keyed by the session's own id."""
        session = _FakeSession(tmp_path)

        deps = _coordinator(session).tool_deps()

        assert deps.orchestration.session_id == "session-42"

    def test_the_coordinator_hands_over_the_rest_of_the_session(self, tmp_path):
        """Every collaborator in the bag is the session's own."""
        session = _FakeSession(tmp_path)

        deps = _coordinator(session).tool_deps()

        assert deps.registry.project_dir is session.project_dir
        assert deps.registry.settings is session.settings
        assert deps.registry.permissions_cls is ToolPermissions
        assert deps.registry.registry_cls is ToolRegistry
        assert deps.sources.plugin_loader is session.plugin_loader
        assert deps.sources.disabled_plugins == {"off"}
        assert deps.orchestration.pool is session.pool
        assert deps.orchestration.hook_executor is session.hook_executor
        assert deps.orchestration.spawn_budget is session.spawn_budget
        assert deps.orchestration.sub_agent_hitl is session.sub_agent_hitl
        assert deps.mcp_manager is session.mcp_manager
        assert deps.lsp_manager is session.lsp_manager
        assert deps.monitor_manager is session.monitor_manager
        assert deps.reasoning_factory(session.settings) is _REASONING

    def test_the_coordinator_resolves_the_tool_names_from_the_sessions_permissions(self, tmp_path):
        """The built-in names follow the session's web permissions — a
        session that denies the web does not get the web tools."""
        session = _FakeSession(tmp_path)
        registry = ToolRegistry()

        deps = _coordinator(session).tool_deps()

        assert deps.resolve_tool_names(registry) == list(MainToolSet.CORE)
        assert deps.resolve_tool_names(registry) == MainToolSet(session.settings.permissions).names(
            registry
        )

    async def test_the_coordinator_hands_over_toolkits_built_for_that_session(self, tmp_path):
        """The session-facing toolkits were built against this session, so
        they answer for it."""
        session = _FakeSession(tmp_path)

        deps = _coordinator(session).tool_deps()

        assert await deps.session_toolkits.loop.loop_status() == "loop-status-of-this-session"
