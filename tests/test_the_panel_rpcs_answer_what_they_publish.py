"""The GUI-panel RPCs, called by their wire arguments, answer what they publish.

Each call goes through ``PanelRpcHandler.methods()`` — the validation a
client's arguments get, then the handler — over the real collaborators
that are cheap to build: a project on disk behind the file index, a real
captured shell runner and process supervisor behind the watcher calls,
the real directory scanner. Only what needs a whole session (the backend
facade's agent, command and group answers) is a recording stand-in; the
test then reads what the handler asked of it and what it answered.

Pinned:

* ``complete_files`` passes the typed query and limit on and reports the
  total before the cap; ``list_dirs`` lists the asked folder (hidden ones
  on request); ``run_shell`` runs in the project and blank runs nothing;
* ``pick_dir_native`` opens the picker in the asked folder, falling back
  to the project, and ``""`` when the folder does not exist;
* the watcher calls list, read the tail of and stop a real process;
* ``promote_ephemeral_agent`` maps a missing target to ``project``;
  ``dispatch_visualization_action`` records ``{}`` for no params;
* each call declares its scope, duration, correlation and pushes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.backend import folder_picker
from igni_core.backend.captured_shell_runner import CapturedShellRunner
from igni_core.backend.dir_scanner import DirScanner
from igni_core.backend.file_completion_service import FileCompletionService
from igni_core.backend.rpc_binding import Correlation, RpcArgsError, RpcBinding
from igni_core.backend.rpc_handlers.panels import PanelRpcHandler
from igni_core.backend.schemas_panels import DiscardEphemeralResult
from igni_core.backend.schemas_rpc import (
    DirListResult,
    FileCompletion,
    GroupPolicyPackResult,
    PickDirResult,
)
from igni_core.backend.schemas_search import SearchCodeResult
from igni_core.backend.schemas_visualization import VisualizationAction
from igni_core.backend.server_processes import ProcessesController
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class _Backend:
    """What the panel calls ask of the session's backend, recorded."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.asked: list[tuple[str, Any]] = []

    def read_file(self, path: str) -> dict[str, Any]:
        self.asked.append(("read_file", path))
        return {"path": path, "contents": "x", "size": 1, "language": "text"}

    def promote_ephemeral_agent(self, name: str, destination: str) -> dict[str, Any]:
        self.asked.append(("promote", (name, destination)))
        return {"ok": True, "name": name, "dest": destination}

    def dispatch_visualization_action(self, action: VisualizationAction) -> Any:
        self.asked.append(("viz", action))
        return {"ok": True, "action": action.action, "params": action.params}

    def list_background_processes(self) -> Any:
        return self.controllers.processes.list()

    def get_group_policy(self) -> GroupPolicyPackResult:
        return GroupPolicyPackResult(group_id="g-1", group_name="Platform")


class _Rig:
    def __init__(self, project: Path, *, shell_timeout: int = 120) -> None:
        self.supervisor = ProcessSupervisor()
        self.backend = _Backend(project)
        ctx = SimpleNamespace(
            backend=self.backend,
            file_completion=FileCompletionService(project),
            shell_runner=CapturedShellRunner(
                project, supervisor=self.supervisor, timeout_seconds=shell_timeout
            ),
        )
        self.searched: list[str] = []
        self.discarded: list[str] = []
        rig = self

        class _Search:
            async def search_code(self, query: str) -> SearchCodeResult:
                rig.searched.append(query)
                return SearchCodeResult(matches=[], truncated=False, error="")

        class _Panels:
            def discard_ephemeral_agent(self, name: str) -> DiscardEphemeralResult:
                rig.discarded.append(name)
                return DiscardEphemeralResult(ok=False, name=name, reason="unknown")

        ctx.backend.controllers = SimpleNamespace(
            processes=ProcessesController(self.supervisor), search=_Search(), panels=_Panels()
        )
        self.calls = PanelRpcHandler(ctx).methods()  # type: ignore[arg-type]

    async def call(self, method: RpcMethod, wire: dict[str, Any] | None = None) -> Any:
        result = self.calls[method](wire or {})
        return await result if asyncio.iscoroutine(result) else result


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    for name in ("src/alpha.py", "src/alphabet.py", "src/beta.py", "README.md"):
        (root / name).write_text("x\n")
    return root


# ── file completion, listing, shell ───────────────────────────────


async def test_completion_passes_the_typed_query_and_reports_the_total_before_the_cap(project):
    rig = _Rig(project)

    capped = await rig.call(RpcMethod.COMPLETE_FILES, {"query": "alpha", "limit": 1})
    everything = await rig.call(RpcMethod.COMPLETE_FILES, {"query": "alpha"})

    assert isinstance(capped, FileCompletion)
    assert capped.total == 2 and len(capped.matches) == 1
    assert sorted(everything.matches) == ["src/alpha.py", "src/alphabet.py"]
    assert everything.total == 2


async def test_completion_of_nothing_typed_matches_every_file_up_to_the_default_cap(project):
    result = await _Rig(project).call(RpcMethod.COMPLETE_FILES)

    assert result.total == 4
    assert sorted(result.matches) == [
        "README.md",
        "src/alpha.py",
        "src/alphabet.py",
        "src/beta.py",
    ]


async def test_read_file_hands_the_path_to_the_backend_and_answers_what_it_read(project):
    rig = _Rig(project)

    await rig.call(RpcMethod.READ_FILE, {"path": "/abs/notes.txt"})
    result = await rig.call(RpcMethod.READ_FILE, {"path": "attachment:abc"})

    assert rig.backend.asked == [("read_file", "/abs/notes.txt"), ("read_file", "attachment:abc")]
    assert result["path"] == "attachment:abc"
    with pytest.raises(RpcArgsError, match="'path' is required"):
        rig.calls[RpcMethod.READ_FILE]({})


async def test_list_dirs_lists_the_asked_folder_and_hides_dot_folders_unless_asked(tmp_path):
    for name in ("b", "A", ".git"):
        (tmp_path / name).mkdir()
    (tmp_path / "file.txt").write_text("x")
    rig = _Rig(tmp_path)

    plain = await rig.call(RpcMethod.LIST_DIRS, {"path": str(tmp_path)})
    hidden = await rig.call(RpcMethod.LIST_DIRS, {"path": str(tmp_path), "show_hidden": True})

    assert isinstance(plain, DirListResult)
    assert plain.path == str(tmp_path.resolve()) or plain.path == str(tmp_path)
    assert plain.parent == str(tmp_path.parent)
    assert plain.dirs == ["A", "b"]
    assert hidden.dirs == [".git", "A", "b"]
    assert plain.home == str(Path.home())


async def test_list_dirs_with_no_path_lists_the_home_folder(project, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "docs").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    result = await _Rig(project).call(RpcMethod.LIST_DIRS, {"path": ""})

    assert result.dirs == ["docs"]
    assert result.path == str(home)
    assert DirScanner(home, False).scan().dirs == ["docs"]


async def test_run_shell_runs_in_the_project_and_blank_runs_nothing(project):
    rig = _Rig(project)

    ran = await rig.call(RpcMethod.RUN_SHELL, {"command": "pwd; exit 3"})
    blank = await rig.call(RpcMethod.RUN_SHELL, {"command": "   "})
    absent = await rig.call(RpcMethod.RUN_SHELL)

    assert ran.output.strip() == str(project.resolve()) or ran.output.strip() == str(project)
    assert ran.exit_code == 3
    assert (blank.output, blank.exit_code) == ("", 0)
    assert (absent.output, absent.exit_code) == ("", 0)


# ── the folder picker ─────────────────────────────────────────────


class _Picker:
    opened_in: list[str] = []

    def __init__(self, start_dir: str) -> None:
        self.opened_in.append(start_dir)

    async def pick(self) -> PickDirResult:
        return PickDirResult(path="/chosen", cancelled=False, error="")


@pytest.fixture
def picker(monkeypatch):
    _Picker.opened_in = []
    monkeypatch.setattr(
        folder_picker.NativeFolderPicker, "for_platform", classmethod(lambda cls, d: _Picker(d))
    )
    return _Picker


async def test_the_picker_opens_in_the_asked_folder_else_the_project(project, tmp_path, picker):
    rig = _Rig(project)
    other = tmp_path / "other"
    other.mkdir()

    chosen = await rig.call(RpcMethod.PICK_DIR_NATIVE, {"start": str(other)})
    await rig.call(RpcMethod.PICK_DIR_NATIVE)
    await rig.call(RpcMethod.PICK_DIR_NATIVE, {"start": ""})

    assert chosen.path == "/chosen"
    assert picker.opened_in == [str(other), str(project), str(project)]


async def test_a_start_folder_that_does_not_exist_lets_the_picker_choose(project, picker):
    await _Rig(project).call(RpcMethod.PICK_DIR_NATIVE, {"start": str(project / "gone")})

    assert picker.opened_in == [""]


# ── agents, visualization, group policy ───────────────────────────


async def test_promoting_an_agent_goes_to_the_project_unless_told_personal(project):
    rig = _Rig(project)

    await rig.call(RpcMethod.PROMOTE_EPHEMERAL_AGENT, {"name": "scout"})
    await rig.call(RpcMethod.PROMOTE_EPHEMERAL_AGENT, {"name": "scout", "target": "personal"})

    assert rig.backend.asked == [
        ("promote", ("scout", "project")),
        ("promote", ("scout", "personal")),
    ]


async def test_a_visualization_action_without_params_is_recorded_with_empty_ones(project):
    rig = _Rig(project)

    bare = await rig.call(RpcMethod.DISPATCH_VISUALIZATION_ACTION, {"action": "zoom"})
    full = await rig.call(
        RpcMethod.DISPATCH_VISUALIZATION_ACTION, {"action": "zoom", "params": {"level": 2}}
    )

    assert bare["params"] == {}
    assert full["params"] == {"level": 2}
    assert [a.action for _, a in rig.backend.asked] == ["zoom", "zoom"]


async def test_a_code_search_and_a_discard_reach_their_controllers_with_the_asked_name(project):
    rig = _Rig(project)

    found = await rig.call(RpcMethod.SEARCH_CODE, {"query": "def handler(args):"})
    discarded = await rig.call(RpcMethod.DISCARD_EPHEMERAL_AGENT, {"name": "scout"})

    assert rig.searched == ["def handler(args):"]
    assert found.matches == [] and found.truncated is False
    assert rig.discarded == ["scout"]
    assert (discarded.ok, discarded.name, discarded.reason) == (False, "scout", "unknown")
    with pytest.raises(RpcArgsError, match="'snippet' is not an argument"):
        rig.calls[RpcMethod.SEARCH_CODE]({"snippet": "abcdef"})


async def test_the_group_policy_is_what_the_backend_reads(project):
    result = await _Rig(project).call(RpcMethod.GET_GROUP_POLICY)

    assert (result.group_id, result.group_name) == ("g-1", "Platform")


# ── the watcher over a real process ───────────────────────────────


async def test_a_long_command_is_listed_read_and_stopped_through_the_watcher_calls(project):
    rig = _Rig(project, shell_timeout=1)

    started = await rig.call(
        RpcMethod.RUN_SHELL, {"command": "echo hello-tail; echo two; sleep 30"}
    )
    try:
        assert started.exit_code == 0
        rows = await rig.call(RpcMethod.LIST_BACKGROUND_PROCESSES)
        row = next(r for r in rows if "hello-tail" in r.cmd)
        assert row.is_running

        tail = await rig.call(RpcMethod.READ_PROCESS_TAIL, {"pid": row.pid, "tail": 1})
        assert tail.is_running and tail.exit_code is None
        assert tail.output.strip() == "two"

        stopped = await rig.call(RpcMethod.STOP_BACKGROUND_PROCESS, {"pid": row.pid})
        assert stopped.killed is True
        again = await rig.call(RpcMethod.STOP_BACKGROUND_PROCESS, {"pid": row.pid})
        assert again.killed is False and "already exited" in again.message
    finally:
        for p in rig.supervisor.registry.all_known():
            mp = rig.supervisor.registry.get(p.pid)
            if mp is not None and mp.is_running():
                await rig.call(RpcMethod.STOP_BACKGROUND_PROCESS, {"pid": p.pid})


async def test_an_unknown_process_reads_empty_and_cannot_be_stopped(project):
    rig = _Rig(project)

    tail = await rig.call(RpcMethod.READ_PROCESS_TAIL, {"pid": 999999})
    stop = await rig.call(RpcMethod.STOP_BACKGROUND_PROCESS, {"pid": 999999})

    assert (tail.output, tail.is_running, tail.exit_code) == ("", False, None)
    assert stop.killed is False and "not in registry" in stop.message


# ── what each call declares ───────────────────────────────────────


def _binding(method: RpcMethod) -> RpcBinding:
    return RpcBinding.on(PanelRpcHandler)[method][1]


def test_the_folder_picker_declares_a_person_sized_duration_and_the_shell_a_long_one():
    assert _binding(RpcMethod.PICK_DIR_NATIVE).duration is RpcDuration.PERSON
    assert _binding(RpcMethod.RUN_SHELL).duration is RpcDuration.LONG
    for method in (
        RpcMethod.COMPLETE_FILES,
        RpcMethod.READ_FILE,
        RpcMethod.SEARCH_CODE,
        RpcMethod.LIST_DIRS,
        RpcMethod.PROMOTE_EPHEMERAL_AGENT,
        RpcMethod.DISCARD_EPHEMERAL_AGENT,
        RpcMethod.STOP_BACKGROUND_PROCESS,
        RpcMethod.GET_GROUP_POLICY,
    ):
        assert _binding(method).duration is RpcDuration.IO, method


def test_the_streaming_calls_declare_their_pushes_and_how_to_tell_them_apart():
    shell = _binding(RpcMethod.RUN_SHELL)
    assert shell.correlate == Correlation(by="payload.pid", source="result.pid")
    assert shell.streams == (
        PushChannel.PROCESS_STARTED,
        PushChannel.PROCESS_LINE,
        PushChannel.PROCESS_EXITED,
        PushChannel.BACKGROUND_PROCESS_DONE,
    )
    stop = _binding(RpcMethod.STOP_BACKGROUND_PROCESS)
    assert stop.correlate == Correlation(by="payload.pid", source="args.pid")
    assert stop.streams == (
        PushChannel.PROCESS_LINE,
        PushChannel.PROCESS_EXITED,
        PushChannel.BACKGROUND_PROCESS_DONE,
    )
    viz = _binding(RpcMethod.DISPATCH_VISUALIZATION_ACTION)
    assert viz.correlate == Correlation(by="payload.action", source="args.action")
    assert viz.streams == (PushChannel.VISUALIZATION_ACTION_DISPATCHED,)


def test_the_calls_about_igni_rather_than_a_session_say_so():
    igni = {
        method
        for method, (_, binding) in RpcBinding.on(PanelRpcHandler).items()
        if binding.scope is RpcScope.IGNI
    }
    assert igni == {
        RpcMethod.LIST_DIRS,
        RpcMethod.LIST_BACKGROUND_PROCESSES,
        RpcMethod.READ_PROCESS_TAIL,
        RpcMethod.STOP_BACKGROUND_PROCESS,
        RpcMethod.GET_GROUP_POLICY,
    }


async def test_a_slow_folder_listing_does_not_stop_other_calls_from_being_answered(
    project, monkeypatch
):
    import time

    monkeypatch.setattr(
        DirScanner,
        "scan",
        lambda self: (
            time.sleep(0.3) or DirListResult(path="/", parent="", dirs=[], home="/", error="")
        ),
    )
    ticks = 0

    async def other_calls() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(other_calls())
    try:
        await _Rig(project).call(RpcMethod.LIST_DIRS, {"path": str(project)})
    finally:
        ticker.cancel()

    assert ticks >= 5
