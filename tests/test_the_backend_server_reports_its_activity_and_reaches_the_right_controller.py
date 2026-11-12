"""What the server facade itself decides: its activity, the order of the
session start, the settings after a trust change, the events it filters, the
objects it shares with its controllers — and that each of its one-line
methods reaches the controller it is meant to."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend import server as server_module
from igni_core.backend.activity import ActivityState
from igni_core.backend.server import BackendServer
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.tools.process_supervisor_locator import SupervisorLocator


class _Spy:
    """Answers any call with the dotted path it was reached by and its arguments."""

    def __init__(self, path: str = "") -> None:
        self._path = path

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return _Spy(f"{self._path}.{name}" if self._path else name)

    def __call__(self, *args: object) -> Any:
        result = (self._path, *args)

        class _Both(tuple):  # type: ignore[type-arg]
            def __await__(self_inner):  # noqa: N805
                async def _r() -> tuple:  # type: ignore[type-arg]
                    return tuple(self_inner)

                return _r().__await__()

        return _Both(result)


def _server(
    *,
    controllers: Any = None,
    session: Any = None,
    settings: Any = None,
) -> BackendServer:
    session = session if session is not None else SimpleNamespace(project_dir=Path("."))
    if not hasattr(session, "project_dir"):
        session.project_dir = Path(".")
    if controllers is None:
        controllers = _Spy()
    return BackendServer.from_parts(
        session=session,
        settings=settings if settings is not None else SimpleNamespace(),
        controllers=controllers,
        message_queue=None,
        supervisors=SupervisorLocator(),
        gate=RunGate(),
    )


def _controllers(
    *,
    processing: bool = False,
    waiting: list[int] | None = None,
    subagent_runs: int = 0,
    revalidate: Any = None,
) -> SimpleNamespace:
    async def ok() -> SimpleNamespace:
        return SimpleNamespace(error="")

    return SimpleNamespace(
        runs=SimpleNamespace(is_processing=lambda: processing),
        hitl=SimpleNamespace(
            waiting_messages=lambda: [SimpleNamespace(requirements=[0] * n) for n in waiting or []]
        ),
        team_wiring=SimpleNamespace(active_subagent_runs=lambda: subagent_runs),
        group_policy=SimpleNamespace(revalidate_for_new_dialogue=revalidate or ok),
    )


# ── activity ────────────────────────────────────────────────────────


def test_a_session_with_nothing_going_on_is_idle() -> None:
    session = SimpleNamespace(session_id="s1", sub_agent_hitl=SimpleNamespace(waiting_count=0))

    activity = _server(controllers=_controllers(), session=session).activity()

    assert (activity.session_id, activity.state, activity.waiting_approvals) == (
        "s1",
        ActivityState.IDLE,
        0,
    )


def test_a_run_in_progress_is_running() -> None:
    session = SimpleNamespace(session_id="s1", sub_agent_hitl=SimpleNamespace(waiting_count=0))

    activity = _server(controllers=_controllers(processing=True), session=session).activity()

    assert activity.state is ActivityState.RUNNING


def test_approvals_waiting_on_a_person_add_up_across_messages() -> None:
    session = SimpleNamespace(session_id="s1", sub_agent_hitl=SimpleNamespace(waiting_count=0))

    activity = _server(controllers=_controllers(waiting=[2, 3]), session=session).activity()

    assert (activity.state, activity.waiting_approvals) == (ActivityState.WAITING, 5)


def test_a_run_whose_sub_agents_are_all_blocked_is_waiting() -> None:
    session = SimpleNamespace(session_id="s1", sub_agent_hitl=SimpleNamespace(waiting_count=2))
    controllers = _controllers(processing=True, subagent_runs=2)

    assert _server(controllers=controllers, session=session).activity().state is (
        ActivityState.WAITING
    )


def test_a_run_with_a_sub_agent_still_working_is_running() -> None:
    session = SimpleNamespace(session_id="s1", sub_agent_hitl=SimpleNamespace(waiting_count=1))
    controllers = _controllers(processing=True, subagent_runs=2)

    assert _server(controllers=controllers, session=session).activity().state is (
        ActivityState.RUNNING
    )


# ── session start ───────────────────────────────────────────────────


async def test_the_pack_is_revalidated_before_the_start_hook_fires() -> None:
    order: list[str] = []

    async def revalidate() -> SimpleNamespace:
        order.append("revalidate")
        return SimpleNamespace(error="")

    async def fire() -> None:
        order.append("hook")

    session = SimpleNamespace(fire_session_start_hook=fire)

    await _server(
        controllers=_controllers(revalidate=revalidate), session=session
    ).fire_session_start_hook()

    assert order == ["revalidate", "hook"]


async def test_a_failed_revalidation_is_logged_and_the_session_still_starts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fired: list[bool] = []

    async def revalidate() -> SimpleNamespace:
        return SimpleNamespace(error="portal unreachable")

    async def fire() -> None:
        fired.append(True)

    session = SimpleNamespace(fire_session_start_hook=fire)
    server = _server(controllers=_controllers(revalidate=revalidate), session=session)

    with caplog.at_level(logging.WARNING, logger=server_module.logger.name):
        await server.fire_session_start_hook()

    assert fired == [True]
    assert "portal unreachable" in caplog.text


# ── what the server shares and adopts ───────────────────────────────


def test_a_new_dialogue_revalidates_the_group_policy() -> None:
    async def revalidate() -> SimpleNamespace:
        return SimpleNamespace(error="")

    controllers = _controllers(revalidate=revalidate)
    session = SimpleNamespace(project_dir=Path("."))

    _server(controllers=controllers, session=session)

    assert session.on_new_dialogue is revalidate  # type: ignore[attr-defined]


def test_a_server_has_no_runtime_until_one_is_attached() -> None:
    server = _server()
    runtime = object()

    assert server.runtime is None
    server.attach_runtime(runtime)  # type: ignore[arg-type]
    assert server.runtime is runtime


def test_a_server_assembled_from_parts_answers_with_the_supervisor_and_gate_it_was_given() -> None:
    locator, gate = SupervisorLocator(), RunGate()

    server = BackendServer.from_parts(
        session=SimpleNamespace(project_dir=Path(".")),  # type: ignore[arg-type]
        settings=SimpleNamespace(),  # type: ignore[arg-type]
        controllers=_controllers(),  # type: ignore[arg-type]
        message_queue=None,
        supervisors=locator,
        gate=gate,
    )

    assert server.supervisors is locator
    assert server.supervisor is locator.supervisor
    assert server.gate is gate


def test_a_server_built_on_its_own_takes_the_locator_and_gate_it_is_handed(tmp_path: Path) -> None:
    from igni_core.core.config.settings_loader import SettingsLoader

    locator, gate = SupervisorLocator(), RunGate()

    server = BackendServer(
        SettingsLoader.load(project_dir=tmp_path),
        project_dir=tmp_path,
        supervisors=locator,
        gate=gate,
    )

    assert server.supervisors is locator
    assert server.gate is gate
    assert server.controllers.processes is not None


def test_a_server_built_on_its_own_has_a_locator_and_an_open_gate_of_its_own(
    tmp_path: Path,
) -> None:
    from igni_core.core.config.settings_loader import SettingsLoader

    server = BackendServer(SettingsLoader.load(project_dir=tmp_path), project_dir=tmp_path)

    assert isinstance(server.supervisors, SupervisorLocator)
    assert server.supervisor is server.supervisors.supervisor
    assert isinstance(server.gate, RunGate)
    assert server.gate.phase == "open"


# ── session identity and the logged events ──────────────────────────


def test_the_server_reports_the_session_it_serves_and_the_timeout_it_runs_under() -> None:
    session = SimpleNamespace(session_id="abc12345", project_dir=Path("/proj"))
    settings = SimpleNamespace(models=SimpleNamespace(max_run_timeout=77))

    server = _server(session=session, settings=settings, controllers=_controllers(processing=True))

    assert server.session_id == "abc12345"
    assert server.session is session
    assert server.settings is settings
    assert server.project_dir == Path("/proj")
    assert server.run_timeout == 77
    assert server.processing is True


def test_the_events_of_one_kind_come_back_oldest_first() -> None:
    log = [
        SimpleNamespace(type="visualization_delta", n=1),
        SimpleNamespace(type="workflow_event", n=2),
        SimpleNamespace(type="visualization_delta", n=3),
    ]
    server = _server(session=SimpleNamespace(event_log=log))

    assert [e.n for e in server.session_events("visualization_delta")] == [1, 3]  # type: ignore[attr-defined]
    assert [e.n for e in server.session_events("workflow_event")] == [2]  # type: ignore[attr-defined]


def test_an_empty_log_has_no_events_of_any_kind() -> None:
    assert _server(session=SimpleNamespace(event_log=[])).session_events("workflow_event") == []


# ── workspace trust ─────────────────────────────────────────────────


async def test_a_trust_change_reloads_the_settings_for_the_folder_and_rebuilds_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = SimpleNamespace(models=SimpleNamespace(max_run_timeout=5))
    asked: list[Path] = []
    applied: list[object] = []

    def reload(project_dir: Path) -> object:
        asked.append(project_dir)
        return fresh

    async def apply(settings: object) -> None:
        applied.append(settings)

    monkeypatch.setattr(server_module, "reload_settings", reload)
    session = SimpleNamespace(project_dir=Path("/proj"), apply_workspace_trust=apply)
    server = _server(session=session)

    await server.apply_workspace_trust()

    assert asked == [Path("/proj")]
    assert applied == [fresh]
    assert server.settings is fresh
    assert server.run_timeout == 5


# ── each delegate reaches its controller ────────────────────────────


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda s: s.cancel_run(), ("runs.cancel_run",)),
        (lambda s: s.get_status(), ("context.get_status",)),
        (lambda s: s.switch_model("m"), ("model_switcher.switch", "m")),
        (lambda s: s.get_mcp_status(), ("mcp.status",)),
        (lambda s: s.get_mcp_servers(), ("mcp.servers",)),
        (lambda s: s.read_file("a.txt"), ("files.read_file", "a.txt")),
        (lambda s: s.get_latest_plan(), ("plan_snapshots.latest",)),
        (lambda s: s.get_todos(), ("plan_snapshots.todos",)),
        (lambda s: s.dispatch_visualization_action("e"), ("viz_actions.dispatch", "e")),
        (lambda s: s.list_background_processes(), ("processes.list",)),
        (lambda s: s.get_agent_details(), ("panels.agent_details",)),
        (lambda s: s.get_hooks_details(), ("panels.hooks_details",)),
        (lambda s: s.get_skill_details(), ("panels.skill_details",)),
        (lambda s: s.get_output_styles(), ("panels.output_styles",)),
        (lambda s: s.get_group_policy(), ("panels.group_policy",)),
        (lambda s: s.get_slash_commands(), ("panels.slash_commands",)),
        (lambda s: s.get_plugin_details(), ("plugins.list_installed",)),
        (lambda s: s.get_marketplaces(), ("marketplaces.list_registered",)),
        (lambda s: s.skill_names, ("panels.skill_names",)),
        (
            lambda s: s.promote_ephemeral_agent("a"),
            ("panels.promote_ephemeral_agent", "a", "project"),
        ),
    ],
)
def test_a_one_line_method_reaches_the_controller_it_is_meant_to(
    call: Any, expected: tuple
) -> None:  # type: ignore[type-arg]
    assert tuple(call(_server())) == expected


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda s: s.list_sessions(), ("sessions.list_sessions",)),
        (lambda s: s.title_session("n"), ("sessions.title", "n")),
        (lambda s: s.switch_session("x"), ("sessions.switch_session", "x")),
        (lambda s: s.search_chat("x", "q"), ("sessions.search_chat", "x", "q", 50)),
        (lambda s: s.get_chat_history("x"), ("chat_history.rebuild", "x")),
        (lambda s: s.count_context_tokens(), ("context.count_context_tokens",)),
        (lambda s: s.context_breakdown(), ("context.context_breakdown",)),
        (lambda s: s.get_pending_messages("x"), ("context.get_pending_messages", "x")),
        (lambda s: s.get_interrupted_runs("x"), ("context.get_interrupted_runs", "x")),
        (lambda s: s.truncate_history("x", "r"), ("context.truncate_history", "x", "r")),
        (lambda s: s.toggle_mcp("n", True), ("mcp.toggle", "n", True)),
        (lambda s: s.get_mcp_server_details(), ("mcp.server_details",)),
        (lambda s: s.mcp_connect("n"), ("mcp.connect", "n")),
        (lambda s: s.loop_pause(), ("loop.pause",)),
        (lambda s: s.loop_status(), ("loop.status",)),
    ],
)
async def test_an_async_one_line_method_reaches_the_controller_it_is_meant_to(
    call: Any,
    expected: tuple,  # type: ignore[type-arg]
) -> None:
    assert tuple(await call(_server())) == expected
