"""A running backend for the host object model's tests.

Not a test module: no ``test_`` prefix, so pytest collects nothing.

:class:`HostRig` is the backend the way :class:`~igni_core.backend.app.BackendApp`
assembles it — a real :class:`SessionOrchestrator` over a real session
pool, real RPC tables built by the real :class:`RpcRouter`, the root
transport wrapped by the real :class:`ObservedTransport`, the host model
wired by the same :class:`HostModelWiring` the app uses, and the real
loopback :class:`HostChannel`. Only the sessions are stand-ins
(:class:`FakeSession`): a session is an agent and a model, and these
tests are about what reaches them, not what they answer.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from igni_core.backend.activity import ActivityState, SessionActivity
from igni_core.backend.command_result import CommandResult
from igni_core.backend.host_model_wiring import HostModelHubs, HostModelWiring
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.observed_transport import ObservedTransport
from igni_core.backend.plugin_schemas import PluginContents
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.rpc_router import RpcRouter
from igni_core.backend.schemas_history import UserTurn
from igni_core.backend.schemas_loop import LoopStatusSnapshot
from igni_core.backend.schemas_mcp import (
    MCPServerSnapshot,
    MCPServerSummary,
    MCPToolToggleResult,
)
from igni_core.backend.schemas_panels import (
    BuiltinSlashCommand,
    DiscardEphemeralResult,
    HookEntryView,
    OutputStyleInfo,
    OutputStylesResult,
    PromoteEphemeralResult,
    SlashCommandEntry,
)
from igni_core.backend.schemas_plan import LatestPlanResult
from igni_core.backend.schemas_pool import ProjectDirs
from igni_core.backend.schemas_processes import ProcessTailResult, StopProcessResult
from igni_core.backend.schemas_rpc import GroupPolicyPackResult
from igni_core.backend.schemas_visualization import (
    VisualizationAction,
    VisualizationActionResult,
)
from igni_core.backend.server_processes import ProcessRow
from igni_core.backend.session_orchestrator import SessionOrchestrator
from igni_core.backend.session_pool import SessionRuntime
from igni_core.backend.session_runtime_factory import BootRuntime, SessionRuntimeFactory
from igni_core.core.activity import ActivityRegistry
from igni_core.core.agents.schemas import AgentInfo
from igni_core.core.approvals.events import approval_events
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.schemas.display import DisplayConfig
from igni_core.core.config.schemas.models import ModelsConfig
from igni_core.core.hooks.extension_gate import extension_hook_gate
from igni_core.core.plugins.models import MarketplaceInfo, PluginInfo
from igni_core.core.scheduler.models import ScheduledTask
from igni_core.core.session.event_log_schema import (
    SessionEvent,
    SessionEventAppendResult,
    SessionEventDraft,
    SessionEventKind,
)
from igni_core.core.session.schemas import (
    ContextBreakdown,
    LoopAdvance,
    McpInitResult,
    McpServerStatus,
    PersistResult,
    PlanDecisionResult,
)
from igni_core.core.skills.parser import SkillInfo
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.process_supervisor_locator import SupervisorLocator
from igni_core.core.tools.todo import TodoItemWire
from igni_core.extensions.host.caller import Caller
from igni_core.extensions.host.channel import HostChannel
from igni_core.extensions.host.hooks import ExtensionHooks
from igni_core.extensions.host.model import HostModel
from igni_core.extensions.host.processes import ExtensionProcesses
from igni_core.extensions.host.wire import Op, Request
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.store import ExtensionStore
from igni_core.protocol import messages as msg
from igni_core.transport.base import ListeningTransport
from tests.shared_tables import SharedTables
from tests.siblings import IGNI_SDK

REPO = Path(__file__).resolve().parent.parent
SDK_SRC = IGNI_SDK / "packages" / "igni-extension" / "src"


class CapturingTransport(ListeningTransport):
    """The windows, as a list: every message igni sent them."""

    def __init__(self) -> None:
        self.sent: list[msg.Message] = []
        self._closed = False

    async def send(self, message: msg.Message) -> None:
        self.sent.append(message)

    async def receive(self) -> AsyncIterator[msg.Message]:
        return
        yield

    async def close(self) -> None:
        self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        return None

    async def wait_for_connection(self, timeout: float | None = 30.0) -> None:
        return None

    @property
    def client_count(self) -> int:
        return 0

    def owns_client(self, client_id: str) -> bool:
        """No window ever identifies itself here, so none is owned."""
        return False

    def of(self, kind: type[msg.Message]) -> list[Any]:
        return [m for m in self.sent if isinstance(m, kind)]

    def pushes(self, channel: str) -> list[msg.PushNotification]:
        return [
            m for m in self.sent if isinstance(m, msg.PushNotification) and m.channel == channel
        ]


def fake_models() -> ModelsConfig:
    """Two configured models, ``small`` the default.

    ``large`` carries a key, the way a configured model does: the
    extension must never see it, only the variable's name.
    """
    return ModelsConfig(
        default="small",
        max_context_window=100_000,
        registry={
            "small": {"model_id": "s"},
            "large": {"model_id": "l", "api_key": "sk-secret", "api_key_env": "LARGE_KEY"},
        },
    )


class FakePermissions:
    """The permission rules igni keeps, as the HITL controller holds them."""

    def __init__(self) -> None:
        self.level = "allow"
        self.rules: list[tuple[str, str]] = []

    def check(self, tool_name: str, func_name: str, tool_args: Any) -> str:
        return self.level

    def save_rule(self, rule: str, level: str) -> None:
        self.rules.append((rule, level))


class FakeScheduler:
    """The scheduler, as the loop controller holds it."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.cancelled: list[str] = []
        self.stopped = False

    async def execute(self, description: str) -> str:
        self.executed.append(description)
        return f"ran {description}"

    async def cancel(self, task_id: str) -> str:
        self.cancelled.append(task_id)
        return f"cancelled {task_id}"

    async def stop(self) -> None:
        self.stopped = True


class FakeLoop:
    """The loop controller, as the backend holds it."""

    def __init__(self) -> None:
        self.scheduler = FakeScheduler()
        #: The iteration waiting, if any.
        self.pending: LoopAdvance | None = None
        self.cancelled = False
        self.paused = False
        self.resumed = ""

    async def pop_pending_iteration(self) -> LoopAdvance | None:
        found, self.pending = self.pending, None
        return found

    async def cancel_pending(self) -> bool:
        self.cancelled = True
        return True

    async def pause(self) -> bool:
        self.paused = True
        return True

    async def resume(self) -> str:
        self.resumed = "resumed"
        return self.resumed


class FakePlugins:
    """The plugin controller, as the backend holds it."""

    def __init__(self) -> None:
        self.enabled: list[tuple[str, bool]] = []
        self.installed: list[str] = []
        self.removed: list[str] = []
        self.updated: list[str] = []

    def contents(self, name: str) -> PluginContents:
        return PluginContents(name=name, root_path="", readme="")

    async def preview(self, source: str, branch: str | None, subdir: str | None) -> PluginContents:
        return PluginContents(name="previewed", root_path="", readme="")

    def set_enabled(self, name: str, enabled: bool) -> Any:
        self.enabled.append((name, enabled))
        return SimpleNamespace(text=f"{name} {'on' if enabled else 'off'}")

    def install(self, ref: str, install_ref: str | None) -> Any:
        self.installed.append(ref)
        return SimpleNamespace(text=f"installed {ref}")

    def update(self, name: str, install_ref: str | None) -> Any:
        self.updated.append(name)
        return SimpleNamespace(text=f"updated {name}")

    def remove(self, name: str) -> Any:
        self.removed.append(name)
        return SimpleNamespace(text=f"removed {name}")


class FakeMarketplaces:
    """The marketplace controller, as the backend holds it."""

    def __init__(self) -> None:
        self.added: list[str] = []
        self.removed: list[str] = []
        self.refreshed: list[str | None] = []

    def add(self, url: str) -> Any:
        self.added.append(url)
        return SimpleNamespace(text=f"added {url}")

    def remove(self, name: str) -> Any:
        self.removed.append(name)
        return SimpleNamespace(text=f"removed {name}")

    def refresh(self, name: str | None) -> Any:
        self.refreshed.append(name)
        return SimpleNamespace(text="refreshed")


class FakeSession:
    """One session's backend, standing in for :class:`BackendServer`.

    Implements the methods the RPC handlers and the message dispatcher
    call, answering with values of the shapes the real ones return, and
    records what it was asked.
    """

    def __init__(self, session_id: str, project_dir: Path) -> None:
        self.session_id = session_id
        self.project_dir = project_dir
        self.processing = False
        self.run_timeout = 600
        # The supervisor this backend process shares. The real
        # ``BackendServer`` carries one; the fake needs one too,
        # because the RPC router reads it to build the shell runner.
        self.supervisor = ProcessSupervisor()
        # Whether this backend process may run agent sessions. The real
        # ``BackendServer`` carries one; the RPC router reads it to build
        # the workflow runner. This one is open, so a run starts.
        self.gate = RunGate()
        self.settings = SimpleNamespace(display=DisplayConfig(), models=fake_models())
        self.runtime: Any = None
        self.ran: list[tuple[str, dict[str, str]]] = []
        self.commands: list[str] = []
        self.events: list[SessionEvent] = []
        self.answers: list[tuple[str, str, str]] = []
        self.switched_to: list[str] = []
        self.output_style = ""
        self.skill_names = ["review"]
        self.run_release: asyncio.Event | None = None
        self.titles: list[str] = []
        self.promoted: list[tuple[str, str]] = []
        self.discarded: list[str] = []
        self.plan_decisions: list[tuple[str, str]] = []
        self.session_start_hooks = 0
        #: The permission rules igni keeps, and the level a check answers.
        self.permissions = FakePermissions()
        #: The loop, as the backend holds it — and the scheduler in it.
        self.loop = FakeLoop()
        #: The plugins and the marketplaces, as the backend holds them.
        self.plugins = FakePlugins()
        self.marketplaces = FakeMarketplaces()
        # The approvals still waiting, as a backend's HITL controller holds them.
        self.controllers = SimpleNamespace(
            hitl=SimpleNamespace(
                waiting_messages=lambda: [],
                permissions=self.permissions,
            ),
            loop=self.loop,
            panels=SimpleNamespace(
                reload_hooks=lambda: None,
                discard_ephemeral_agent=self.discard_ephemeral_agent,
            ),
            mcp=SimpleNamespace(
                ensure=self.ensure_mcp,
                disconnect=self.mcp_disconnect,
                set_tool_enabled=self.set_mcp_tool_enabled,
            ),
            processes=SimpleNamespace(
                read_tail=self.read_process_tail,
                stop=self.stop_process,
            ),
            plugins=self.plugins,
            marketplaces=self.marketplaces,
        )
        #: The session the backend drives: the plan decisions go through it.
        self.session = SimpleNamespace(
            plan=SimpleNamespace(
                approve=self.approve_plan,
                dismiss=self.dismiss_plan,
            ),
        )

    # ── Lifecycle the pool and dispatcher touch ───────────────────

    def attach_runtime(self, runtime: Any) -> None:
        self.runtime = runtime

    async def shutdown(self) -> None:
        return None

    def consume_plan_research_flag(self) -> bool:
        return False

    async def maybe_auto_name_session(self) -> Any:
        return SimpleNamespace(ok=False, name="")

    # ── Runs ──────────────────────────────────────────────────────

    async def run_message(
        self, text: str, media: dict[str, str] | None = None
    ) -> AsyncIterator[msg.Message]:
        self.ran.append((text, dict(media or {})))
        run_id = f"run-{len(self.ran)}"
        self.processing = True
        yield msg.RunStarted(run_id=run_id, agent_name="main", model="small")
        yield msg.ContentDelta(text="On it.")
        if self.run_release is not None:
            await self.run_release.wait()
        self.processing = False
        yield msg.RunCompleted(run_id=run_id, input_tokens=3, output_tokens=2, duration=0.5)

    async def resolve_hitl(
        self, requirement_id: str, action: str, choice: str
    ) -> AsyncIterator[msg.Message]:
        self.answers.append((requirement_id, action, choice))
        yield msg.Info(text=f"resolved {requirement_id}")

    async def resolve_hitl_batch(self, decisions: list[Any]) -> AsyncIterator[msg.Message]:
        for d in decisions:
            self.answers.append((d.requirement_id, d.action, d.choice))
        yield msg.Info(text="resolved")

    async def handle_command(self, text: str) -> msg.CommandResult:
        self.commands.append(text)
        if text.startswith("/output-style "):
            self.output_style = text.split(" ", 1)[1]
        return CommandResult.info(f"ran {text}")

    def cancel_run(self) -> None:
        return None

    # ── Reads ─────────────────────────────────────────────────────

    async def fire_session_start_hook(self) -> None:
        self.session_start_hooks += 1

    def get_status(self) -> msg.StatusUpdate:
        return msg.StatusUpdate(
            model=self.settings.models.default,
            max_context=self.settings.models.max_context_window,
            cloud_connected=True,
            cloud_email="ada@acme.test",
            cloud_name="Ada",
            cloud_org="Acme",
        )

    async def list_sessions(self) -> msg.SessionListResult:
        return msg.SessionListResult(
            sessions=[msg.SessionListEntry(session_id=self.session_id, name="First", run_count=1)]
        )

    async def title_session(self, name: str) -> PersistResult:
        self.titles.append(name)
        return PersistResult(ok=True)

    async def switch_session(self, session_id: str) -> msg.Info:
        self.switched_to.append(session_id)
        return msg.Info(text=f"Switched to session: {session_id}")

    async def get_chat_history(self, session_id: str) -> list[UserTurn]:
        return [UserTurn(content=text) for text, _ in self.ran]

    async def search_chat(self, session_id: str, query: str, limit: int = 50) -> list[dict]:
        return [{"text": t} for t, _ in self.ran if query in t][:limit]

    async def get_pending_messages(self, session_id: str) -> list[dict]:
        return []

    async def get_interrupted_runs(self, session_id: str) -> list[dict]:
        return []

    async def count_context_tokens(self) -> int:
        return 1234

    async def context_breakdown(self) -> ContextBreakdown:
        return ContextBreakdown.from_totals(total=1234, runs=1000)

    def get_latest_plan(self) -> LatestPlanResult:
        return LatestPlanResult(latest="", history=[], tasks=[], state="")

    def get_todos(self) -> list[TodoItemWire]:
        return [TodoItemWire(content="write tests", status="pending", activeForm="Writing tests")]

    async def loop_status(self) -> LoopStatusSnapshot:
        return LoopStatusSnapshot(
            active=False,
            paused=False,
            prompt="",
            iteration_index=0,
            iterations_remaining=0,
            cap_explicit=False,
            announced_total=None,
        )

    def get_slash_commands(self) -> list[SlashCommandEntry]:
        return [BuiltinSlashCommand(name="/ctx", description="context")]

    def get_skill_pool(self) -> Any:
        return SimpleNamespace(
            list_skills=lambda: [
                SimpleNamespace(name="review", description="Reviews", body="Review it.")
            ]
        )

    def get_skill_details(self) -> list[SkillInfo]:
        return [SkillInfo(name="review", description="Reviews")]

    def get_agent_details(self) -> list[AgentInfo]:
        return [AgentInfo(name="main", description="The main agent")]

    def promote_ephemeral_agent(self, name: str, target: str = "project") -> PromoteEphemeralResult:
        self.promoted.append((name, target))
        subdir = "agents" if target == "project" else "agents.local"
        return PromoteEphemeralResult(ok=True, name=name, dest=f".igni/{subdir}/{name}.md")

    def discard_ephemeral_agent(self, name: str) -> DiscardEphemeralResult:
        self.discarded.append(name)
        return DiscardEphemeralResult(ok=True, name=name)

    async def get_scheduled_tasks(self, include_done: bool = True) -> list[ScheduledTask]:
        return [
            ScheduledTask(
                id="t1",
                description="nightly",
                scheduled_at=datetime(2026, 1, 1, 3, 0),
            )
        ]

    def list_background_processes(self) -> list[ProcessRow]:
        return []

    async def read_process_tail(self, pid: int, tail: int = 200) -> ProcessTailResult:
        return ProcessTailResult(pid=pid, output="", is_running=False, exit_code=0)

    async def stop_process(self, pid: int) -> StopProcessResult:
        return StopProcessResult(pid=pid, killed=True, message=f"stopped {pid}")

    def get_mcp_servers(self) -> list[MCPServerSummary]:
        return [MCPServerSummary(name="github", connected=False)]

    def get_mcp_status(self) -> list[McpServerStatus]:
        return [McpServerStatus(name="github", connected=False)]

    async def get_mcp_server_details(self) -> list[MCPServerSnapshot]:
        return [MCPServerSnapshot(name="github", connected=False, transport="stdio")]

    async def ensure_mcp(self) -> McpInitResult:
        return McpInitResult(connected=[], failed={}, rebuilt=False, skipped_reason=None)

    async def mcp_disconnect(self, server_name: str) -> Any:
        return SimpleNamespace(text=f"disconnected {server_name}")

    def set_mcp_tool_enabled(self, server: str, tool: str, enabled: bool) -> MCPToolToggleResult:
        return MCPToolToggleResult(server=server, tool=tool, enabled=enabled)

    def get_plugin_details(self) -> list[PluginInfo]:
        return []

    def get_marketplaces(self) -> list[MarketplaceInfo]:
        return []

    def get_hooks_details(self) -> list[HookEntryView]:
        return []

    async def approve_plan(self, run_id: str) -> PlanDecisionResult:
        self.plan_decisions.append(("approve", run_id))
        return PlanDecisionResult(
            run_id=run_id,
            decision="approved",
            mode_status="mode → default",
            ok=True,
            error=None,
        )

    async def dismiss_plan(self, run_id: str) -> PlanDecisionResult:
        self.plan_decisions.append(("dismiss", run_id))
        return PlanDecisionResult(
            run_id=run_id,
            decision="dismissed",
            mode_status="",
            ok=True,
            error=None,
        )

    def get_group_policy(self) -> GroupPolicyPackResult:
        return GroupPolicyPackResult()

    def get_output_styles(self) -> OutputStylesResult:
        return OutputStylesResult(
            active=self.output_style,
            styles=[OutputStyleInfo(name="terse", description="Short")],
        )

    def read_file(self, path: str) -> dict:
        return {
            "path": path,
            "contents": (self.project_dir / path).read_text(),
            "language": "",
            "error": "",
        }

    # ── Writes ────────────────────────────────────────────────────

    def toggle_verbose(self) -> bool:
        return self.settings.display.toggle_show_routing()

    def switch_model(self, model_name: str) -> msg.Info | msg.Error:
        # Refuses a name the registry doesn't know, as the real switcher does.
        if model_name not in self.settings.models.registry:
            return msg.Error(text=f"Unknown model: '{model_name}'.")
        self.settings.models.default = model_name
        return msg.Info(text=f"Switched to {model_name}")

    async def mcp_connect(self, server_name: str) -> msg.Info:
        return msg.Info(text=f"connected {server_name}")

    async def loop_pause(self) -> bool:
        return True

    def dispatch_visualization_action(
        self, entry: VisualizationAction
    ) -> VisualizationActionResult:
        return VisualizationActionResult.echo(entry)

    async def truncate_history(self, session_id: str, run_id: str) -> dict:
        return {"ok": True, "run_id": run_id}

    def activity(self) -> SessionActivity:
        return SessionActivity(session_id=self.session_id, state=ActivityState.IDLE)

    # ── The event log ─────────────────────────────────────────────

    async def record_event(self, event: SessionEventDraft) -> SessionEventAppendResult:
        entry = event.to_event(len(self.events) + 1)
        self.events.append(entry)
        return SessionEventAppendResult(persisted=False)

    def session_events(self, event_type: SessionEventKind) -> list[SessionEvent]:
        return [e for e in self.events if e.type == event_type]


class RigRuntimeFactory(SessionRuntimeFactory):
    """The real factory, making stand-in sessions instead of real ones."""

    async def create(self, session_id: str) -> SessionRuntime:
        backend = FakeSession(session_id, self._dirs.project_dir)
        stamped = self._wiring.stamped(backend)
        router = self._wiring.router_for(
            backend, self._wiring.push_bridge.for_transport(stamped, queue=[])
        )
        return self.bind(
            backend=backend,
            rpc_table=router.build_table(),
            queue=[],
            transport=stamped,
            router=router,
        )


def manifest(extension_id: str = "acme.reviewer", **permissions: Any) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "name": extension_id,
            "trust": "unsigned",
            "dev": {"command": ["python", "server.py"]},
            "permissions": permissions,
            "contributes": {
                "pages": [{"kind": f"{extension_id}.home", "label": "Home"}],
                "settings": [{"key": "serviceUrl", "type": "string", "label": "Service"}],
            },
        }
    )


class HostRig:
    """A backend with the host model, as the app builds it."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.project = tmp_path / "project"
        self.project.mkdir(parents=True, exist_ok=True)
        self.windows = CapturingTransport()
        self.transport = ObservedTransport(self.windows)
        self.registry = ExtensionRegistry()
        # The extensions' hooks: the rig's own, so a test can ask through them.
        self.hooks = ExtensionHooks(registry=self.registry)
        # The default store, handed to the RPC handlers; ``conftest``
        # points the home directory it lives under at a temp dir.
        self.store = ExtensionStore()
        self.session = FakeSession("boot", self.project)
        self.model: HostModel | None = None
        self.orchestrator: SessionOrchestrator | None = None
        self.channel: HostChannel | None = None
        self._requests = 0

    async def start(self, *, strict: bool = True) -> HostRig:
        loop = asyncio.get_running_loop()
        queue: list[str] = []
        push = PushNotificationBridge(transport=self.transport, loop=loop, queue=queue)
        login = LoginCoordinator(backend=self.session, push_bridge=push)
        extensions = ExtensionRpc(self.registry)
        router = RpcRouter(
            backend=self.session,
            login=login,
            push=push,
            extensions=extensions,
            extension_store=self.store,
        )
        settings = SimpleNamespace(
            storage=SimpleNamespace(data_dir=str(self.tmp / "data"), database_url=None)
        )
        self.settings = settings
        tables = SharedTables(extensions=extensions)
        self.orchestrator = SessionOrchestrator.start(
            boot=BootRuntime(
                backend=self.session, rpc_table=router.build_table(), router=router, queue=queue
            ),
            wiring=PoolWiring(
                transport=self.transport,
                push_bridge=push,
                login=login,
                supervisors=SupervisorLocator(self.session.supervisor),
                gate=self.session.gate,
                contributions=tables.contributions,
                prompt_loader=tables.prompt_loader,
                tool_hints=tables.tool_hints,
                extensions=tables.extensions,
                extension_store=self.store,
            ),
            settings=settings,
            dirs=ProjectDirs(project_dir=self.project),
            factory=RigRuntimeFactory,
        )
        self.model = HostModelWiring.build(
            orchestrator=self.orchestrator,
            transport=self.transport,
            registry=self.registry,
            hooks=self.hooks,
            project_dir=self.project,
            hubs=HostModelHubs(
                approvals=approval_events,
                activities=ActivityRegistry.shared(),
                install_hooks=extension_hook_gate.install,
            ),
            store=self.store,
            strict=strict,
        )
        self.channel = HostChannel(self.model, hooks=self.hooks, processes=ExtensionProcesses())
        return self

    async def stop(self) -> None:
        if self.channel is not None:
            await self.channel.stop()
        if self.model is not None:
            await self.model.services.work.drain()

    def install(self, extension_id: str = "acme.reviewer", **permissions: Any) -> str:
        self.registry.install(manifest(extension_id, **permissions), state=ExtensionState.READY)
        return extension_id

    async def ask(
        self,
        extension_id: str,
        op: str,
        member: str,
        *,
        window: str | None = None,
        home_window: str | None = None,
        session: str | None = None,
        **params: Any,
    ) -> Any:
        """One request, as a channel would make it. Returns the reply.

        ``to=`` is the value of a ``set``; ``session=`` addresses a session
        (``host.sessions.chat(id)``); everything else is a parameter.
        """
        assert self.model is not None
        value = params.pop("to", None)
        request = Request(
            id=1,
            op=Op(op),
            path=member,
            window=window,
            session=session,
            params=params,
            value=value,
        )
        return await self.model.handle(
            request, Caller(extension_id=extension_id, home_window=home_window)
        )

    async def window(
        self, window_id: str = "w-main", *, connection: str = "ws-1", session: str = ""
    ) -> str:
        """A window connects and hydrates its model, as ``clientState.ts`` does."""
        assert self.orchestrator is not None
        if session:
            written = self.orchestrator.client_state.set_value(window_id, "session-id", session)
            assert written.ok, written.error
        await self.window_model(window_id, "hydrate", connection_id=connection, kind="tauri")
        return window_id

    async def window_writes(
        self, window_id: str, field: str, value: Any, *, token: str = ""
    ) -> Any:
        """The window's own views write one field of its model."""
        return await self.window_model(window_id, "write", field=field, value=value, token=token)

    async def window_model(self, window_id: str, op: str, **args: Any) -> Any:
        """One ``window_model`` request from a window; its answer, or
        the error string when the model refused it."""
        assert self.orchestrator is not None
        self._requests += 1
        request_id = f"wm-{op}-{self._requests}"
        await self.orchestrator.dispatch(
            msg.RPCRequest(
                id=request_id, method="window_model", args={"op": op, "window": window_id, **args}
            )
        )
        [answer] = [m for m in self.windows.of(msg.RPCResponse) if m.id == request_id]
        return answer.error or answer.result

    async def window_pushes(self, window_id: str | None = None) -> list[dict[str, Any]]:
        """Every ``window_model_changed`` push so far, optionally for one
        window. Pushes are broadcast on the loop, so it settles first."""
        await self.settle()
        return [
            p.payload
            for p in self.windows.pushes("window_model_changed")
            if window_id is None or p.payload["window"] == window_id
        ]

    async def settle(self) -> None:
        """Let background dispatches and signal deliveries run."""
        for _ in range(20):
            await asyncio.sleep(0)
        assert self.orchestrator is not None and self.model is not None
        tasks = self.orchestrator.in_flight.pending()
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        await self.model.services.work.drain()
        for _ in range(5):
            await asyncio.sleep(0)


class Sink:
    """A subscriber's signals, as a list."""

    def __init__(self) -> None:
        self.got: list[tuple[str, Any]] = []

    def deliver(self, message: Any) -> None:
        self.got.append((message.signal, message.value))

    def values(self, wire: str) -> list[Any]:
        return [value for signal, value in self.got if signal == wire]


def extension_process_env(env: dict[str, str]) -> dict[str, str]:
    """What igni starts a process with, plus the SDK on its path."""
    merged = dict(os.environ)
    merged.update(env)
    merged["PYTHONPATH"] = os.pathsep.join([str(SDK_SRC), merged.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    return merged


PYTHON = sys.executable
