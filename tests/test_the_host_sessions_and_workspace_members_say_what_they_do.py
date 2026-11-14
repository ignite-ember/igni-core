"""``host.sessions`` and ``host.workspace``: what each member sends igni,
which window it moves, what it answers and what it refuses.

Driven over a scripted host: the gateway attaches and answers RPCs from a
table, the windows record what is written to them and in which order, and
the workspace is a real directory seen through a real grant.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.members.call import Arguments, Call
from igni_core.extensions.host.members.sessions import SessionMembers, SessionSwitched
from igni_core.extensions.host.members.workspace import WorkspaceMembers
from igni_core.extensions.host.session_target import SessionTarget
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.values import HeadInfo, SessionInfo
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import manifest
from tests.test_the_host_members_translate_what_an_extension_asks import Gateway


class Attaching(Gateway):
    """A gateway that also attaches sessions, refusing the ones it is told to."""

    def __init__(self, answers: dict[RpcMethod, Any] | None = None) -> None:
        super().__init__(answers or {})
        self.attached: list[tuple[str, str | None]] = []
        self.refuse: dict[str, Exception] = {}

    async def attach(self, session_id: str, project_dir: str | None) -> Any:
        self.attached.append((session_id, project_dir))
        if session_id in self.refuse:
            raise self.refuse[session_id]
        return SimpleNamespace(session_id=session_id or "new-1", project_dir=project_dir or "/proj")


class Windows:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, Any]] = []

    def write(self, window: str, field: str, value: Any) -> None:
        self.writes.append((window, field, value))


class Backend:
    def __init__(self, project: Path | None = None) -> None:
        self.project_dir = project or Path("/proj")
        self.titles: list[tuple[str, str]] = []
        self.title_answer = SimpleNamespace(ok=True, error="")

    async def title(self, *, session_id: str, title: str) -> Any:
        self.titles.append((session_id, title))
        return self.title_answer

    def project_dir_of(self, session_id: str) -> Path:
        return self.project_dir


class Observed:
    def __init__(self) -> None:
        self.switched: list[tuple[str, str | None]] = []

    def session_switched(self, session_id: str, window: str | None) -> None:
        self.switched.append((session_id, window))


def host_of(gateway: Attaching, backend: Backend | None = None) -> Any:
    return SimpleNamespace(
        gateway=gateway,
        backend=backend or Backend(),
        windows=Windows(),
        observations=Observed(),
        bus=SimpleNamespace(emit=lambda *a, **k: None),
    )


def a_call(
    *, window: str | None = None, value: Any = None, grants: HostGrants | None = None, **params: Any
) -> Call:
    return Call(
        extension_id="acme.reviewer",
        grants=grants or HostGrants(manifest("acme.reviewer")),
        window=window,
        target=SessionTarget(session_id="s1", on_screen=True),
        arguments=Arguments(params=params, value=value),
    )


def refusal(caught: pytest.ExceptionInfo[HostModelError]) -> tuple[str, str]:
    return str(caught.value.code), caught.value.message


ENTRY = {
    "session_id": "a",
    "name": "Alpha",
    "created_at": 5,
    "updated_at": 6,
    "run_count": 2,
    "summary": "sum",
    "agent_name": "igni",
}


class TestTheSessionList:
    async def test_a_dict_answer_is_its_sessions(self) -> None:
        gateway = Attaching({RpcMethod.LIST_SESSIONS: {"sessions": [ENTRY, {"session_id": "b"}]}})
        got = await SessionMembers(host_of(gateway)).session_list(a_call())
        assert got == [
            SessionInfo.from_entry(ENTRY),
            SessionInfo(
                session_id="b",
                name="",
                created_at=0,
                updated_at=0,
                run_count=0,
                summary="",
                agent_name="",
            ),
        ]
        assert gateway.calls == [(RpcMethod.LIST_SESSIONS, {}, "s1")]

    async def test_a_bare_list_answer_is_the_sessions_too(self) -> None:
        gateway = Attaching({RpcMethod.LIST_SESSIONS: [ENTRY]})
        got = await SessionMembers(host_of(gateway)).session_list(a_call())
        assert [s.session_id for s in got] == ["a"]

    @pytest.mark.parametrize("answer", [None, [], {}, {"sessions": None}, {"sessions": []}])
    async def test_nothing_is_no_sessions(self, answer: Any) -> None:
        gateway = Attaching({RpcMethod.LIST_SESSIONS: answer})
        assert await SessionMembers(host_of(gateway)).session_list(a_call()) == []


class TestSwitching:
    async def test_with_no_window_the_boot_session_is_switched_and_the_switch_observed(
        self,
    ) -> None:
        gateway = Attaching({RpcMethod.SWITCH_SESSION: {"type": "info"}})
        host = host_of(gateway)
        got = await SessionMembers(host).open(a_call(session_id="other"))
        assert got == SessionSwitched(session_id="other", window=None)
        assert gateway.calls == [(RpcMethod.SWITCH_SESSION, {"session_id": "other"}, "s1")]
        assert host.observations.switched == [("other", None)]
        assert gateway.attached == []
        assert host.windows.writes == []

    async def test_a_refusal_of_the_switch_is_refused_and_not_observed(self) -> None:
        gateway = Attaching(
            {RpcMethod.SWITCH_SESSION: {"type": "error", "text": "no such session"}}
        )
        host = host_of(gateway)
        with pytest.raises(HostModelError) as caught:
            await SessionMembers(host).open(a_call(session_id="gone"))
        assert refusal(caught) == (ErrorCode.REFUSED, "no such session")
        assert host.observations.switched == []

    async def test_with_a_window_the_session_is_attached_then_written_to_the_window(self) -> None:
        gateway = Attaching()
        host = host_of(gateway)
        got = await SessionMembers(host).open(a_call(window="w-1", session_id="other"))
        assert got == SessionSwitched(session_id="other", window="w-1")
        assert gateway.attached == [("other", None)]
        assert host.windows.writes == [("w-1", "session", "other")]
        assert gateway.calls == []
        assert host.observations.switched == []

    async def test_a_session_the_gateway_refuses_to_attach_moves_no_window(self) -> None:
        gateway = Attaching()
        gateway.refuse["theirs"] = HostModelError(ErrorCode.REFUSED, "another project's session")
        host = host_of(gateway)
        with pytest.raises(HostModelError) as caught:
            await SessionMembers(host).open(a_call(window="w-1", session_id="theirs"))
        assert refusal(caught) == (ErrorCode.REFUSED, "another project's session")
        assert host.windows.writes == []

    async def test_an_empty_session_id_is_invalid_and_touches_nothing(self) -> None:
        gateway = Attaching()
        host = host_of(gateway)
        with pytest.raises(HostModelError) as caught:
            await SessionMembers(host).open(a_call(window="w-1", session_id=""))
        assert refusal(caught) == (ErrorCode.INVALID, "a session id is required")
        assert gateway.calls == [] and gateway.attached == [] and host.windows.writes == []

    async def test_a_session_id_that_is_not_a_string_is_its_text(self) -> None:
        gateway = Attaching()
        got = await SessionMembers(host_of(gateway)).open(a_call(window="w", session_id=12))
        assert got.session_id == "12"

    async def test_opening_without_a_session_id_is_a_missing_parameter(self) -> None:
        with pytest.raises(KeyError, match="session_id"):
            await SessionMembers(host_of(Attaching())).open(a_call())

    async def test_setting_the_current_session_switches_to_the_value(self) -> None:
        gateway = Attaching()
        host = host_of(gateway)
        assert await SessionMembers(host).set_current(a_call(window="w-1", value="s2")) is None
        assert host.windows.writes == [("w-1", "session", "s2")]

    @pytest.mark.parametrize("value", [None, ""])
    async def test_setting_the_current_session_to_nothing_is_invalid(self, value: Any) -> None:
        host = host_of(Attaching())
        with pytest.raises(HostModelError) as caught:
            await SessionMembers(host).set_current(a_call(window="w-1", value=value))
        assert refusal(caught) == (ErrorCode.INVALID, "a session id is required")
        assert host.windows.writes == []

    async def test_the_current_session_is_the_calls_own(self) -> None:
        assert await SessionMembers(host_of(Attaching())).current(a_call()) == "s1"


class TestCreatingAndAttaching:
    async def test_a_session_is_created_by_attaching_nothing_in_the_backends_project(
        self,
    ) -> None:
        gateway = Attaching()
        host = host_of(gateway, Backend(Path("/the/project")))
        got = await SessionMembers(host).create(a_call())
        assert (got.session_id, got.project_dir) == ("new-1", "/the/project")
        assert gateway.attached == [("", "/the/project")]
        assert host.backend.titles == []
        assert host.windows.writes == []

    async def test_a_session_is_created_in_the_project_asked_for(self) -> None:
        gateway = Attaching()
        await SessionMembers(host_of(gateway)).create(a_call(project_dir="/elsewhere"))
        assert gateway.attached == [("", "/elsewhere")]

    async def test_a_title_is_set_before_anything_runs(self) -> None:
        host = host_of(Attaching())
        await SessionMembers(host).create(a_call(title="Review"))
        assert host.backend.titles == [("new-1", "Review")]

    @pytest.mark.parametrize("title", ["", "   ", "\n\t"])
    async def test_an_empty_title_is_invalid_and_creates_nothing(self, title: str) -> None:
        gateway = Attaching()
        with pytest.raises(HostModelError) as caught:
            await SessionMembers(host_of(gateway)).create(a_call(title=title))
        assert refusal(caught) == (ErrorCode.INVALID, "sessions.create(title): an empty title")
        assert gateway.attached == []

    async def test_a_title_the_backend_refuses_says_which_session_was_created_untitled(
        self,
    ) -> None:
        host = host_of(Attaching())
        host.backend.title_answer = SimpleNamespace(ok=False, error="read only")
        with pytest.raises(HostModelError) as caught:
            await SessionMembers(host).create(a_call(title="Review", window="w-1", focus=True))
        assert refusal(caught) == (
            ErrorCode.FAILED,
            "session new-1 was created but not titled: read only",
        )
        assert host.windows.writes == []

    async def test_focus_moves_the_window_to_the_folder_first_and_then_the_session(self) -> None:
        host = host_of(Attaching())
        await SessionMembers(host).create(a_call(window="w-1", focus=True, project_dir="/p2"))
        assert host.windows.writes == [
            ("w-1", "project_dir", "/p2"),
            ("w-1", "session", "new-1"),
        ]

    async def test_focus_with_no_window_moves_nothing(self) -> None:
        host = host_of(Attaching())
        await SessionMembers(host).create(a_call(focus=True))
        assert host.windows.writes == []

    async def test_a_window_is_left_alone_unless_focus_is_asked_for(self) -> None:
        host = host_of(Attaching())
        await SessionMembers(host).create(a_call(window="w-1"))
        assert host.windows.writes == []

    async def test_attach_takes_the_session_and_an_optional_project(self) -> None:
        gateway = Attaching()
        members = SessionMembers(host_of(gateway))
        got = await members.attach(a_call(session_id="abc"))
        assert got.session_id == "abc"
        await members.attach(a_call(session_id=7, project_dir="/p"))
        assert gateway.attached == [("abc", None), ("7", "/p")]

    async def test_attach_without_a_session_is_a_missing_parameter(self) -> None:
        with pytest.raises(KeyError, match="session_id"):
            await SessionMembers(host_of(Attaching())).attach(a_call())


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def grants(**permissions: Any) -> HostGrants:
    return HostGrants(manifest("acme.reviewer", **permissions))


class TestTheWorkspace:
    def members(self, root: Path, gateway: Gateway) -> WorkspaceMembers:
        host = host_of(Attaching(gateway.answers), Backend(root))
        host.gateway = gateway
        return WorkspaceMembers(host)

    async def test_completions_ask_for_fifty_and_keep_only_the_granted_paths(
        self, tmp_path: Path
    ) -> None:
        answer = {"matches": ["docs/a.md", "src/secret.py", "docs/b.md"], "total": 3}
        gateway = Gateway({RpcMethod.COMPLETE_FILES: answer})
        got = await self.members(tmp_path, gateway).complete(
            a_call(prefix="d", grants=grants(workspace="read", workspacePaths=["docs/*"]))
        )
        assert got == {"matches": ["docs/a.md", "docs/b.md"], "total": 2}
        assert gateway.calls == [(RpcMethod.COMPLETE_FILES, {"query": "d", "limit": 50}, "s1")]

    async def test_a_completion_with_no_workspace_grant_shows_nothing(self, tmp_path: Path) -> None:
        gateway = Gateway({RpcMethod.COMPLETE_FILES: {"matches": ["a.py"], "total": 1}})
        got = await self.members(tmp_path, gateway).complete(a_call(prefix="a"))
        assert got == {"matches": [], "total": 0}

    async def test_a_completion_without_a_prefix_is_a_missing_parameter(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(KeyError, match="prefix"):
            await self.members(tmp_path, Gateway({})).complete(a_call())

    async def test_a_search_keeps_only_the_hits_in_granted_files(self, tmp_path: Path) -> None:
        hits = [{"path": "docs/a.md", "line": 1}, {"path": "src/x.py", "line": 2}, {"line": 3}]
        gateway = Gateway({RpcMethod.SEARCH_CODE: {"matches": hits, "other": "kept"}})
        got = await self.members(tmp_path, gateway).search(
            a_call(query="todo", grants=grants(workspace="read", workspacePaths=["docs/*"]))
        )
        assert got == {"matches": [{"path": "docs/a.md", "line": 1}], "other": "kept"}
        assert gateway.calls == [(RpcMethod.SEARCH_CODE, {"query": "todo"}, "s1")]

    async def test_a_search_without_a_query_is_a_missing_parameter(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError, match="query"):
            await self.members(tmp_path, Gateway({})).search(a_call())

    async def test_the_native_picker_answers_what_igni_answers(self, tmp_path: Path) -> None:
        gateway = Gateway({RpcMethod.PICK_DIR_NATIVE: {"path": "/chosen"}})
        got = await self.members(tmp_path, gateway).pick_dir(a_call())
        assert got == {"path": "/chosen"}
        assert gateway.calls == [(RpcMethod.PICK_DIR_NATIVE, {}, "s1")]

    async def test_the_head_outside_a_repository_is_nothing(self, tmp_path: Path) -> None:
        got = await self.members(tmp_path, Gateway({})).head(a_call())
        assert got is None

    async def test_the_head_inside_a_repository_is_its_commit_and_branch(
        self, tmp_path: Path
    ) -> None:
        git(tmp_path, "init", "-q", "-b", "trunk")
        (tmp_path / "f").write_text("x")
        git(tmp_path, "add", "f")
        git(tmp_path, "commit", "-q", "-m", "one")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
        ).stdout.strip()
        got = await self.members(tmp_path, Gateway({})).head(a_call())
        assert got == HeadInfo(commit_sha=sha, branch="trunk")

    async def test_the_root_is_the_sessions_project(self, tmp_path: Path) -> None:
        assert await self.members(tmp_path, Gateway({})).root_value(a_call()) == str(tmp_path)

    async def test_the_scope_is_the_projects_root_under_this_extensions_grant(
        self, tmp_path: Path
    ) -> None:
        scope = self.members(tmp_path, Gateway({})).scope(a_call())
        assert scope.root == tmp_path

    async def test_a_listing_is_cut_down_to_the_granted_directories(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        gateway = Gateway({RpcMethod.LIST_DIRS: {"dirs": ["guide", "api"]}})
        got = await self.members(tmp_path, gateway).list_dir(
            a_call(dir="docs", grants=grants(workspace="read", workspacePaths=["docs/guide/*"]))
        )
        assert got == {"dirs": ["guide"]}
        assert gateway.calls == [(RpcMethod.LIST_DIRS, {"path": str(tmp_path / "docs")}, "s1")]

    async def test_an_empty_directory_is_the_project_root(self, tmp_path: Path) -> None:
        gateway = Gateway({RpcMethod.LIST_DIRS: {"dirs": ["a"]}})
        await self.members(tmp_path, gateway).list_dir(
            a_call(dir="", grants=grants(workspace="read"))
        )
        assert gateway.calls == [(RpcMethod.LIST_DIRS, {"path": str(tmp_path)}, "s1")]

    async def test_a_listing_with_no_directory_named_is_the_project_root(
        self, tmp_path: Path
    ) -> None:
        gateway = Gateway({RpcMethod.LIST_DIRS: {"dirs": []}})
        await self.members(tmp_path, gateway).list_dir(a_call(grants=grants(workspace="read")))
        assert gateway.calls == [(RpcMethod.LIST_DIRS, {"path": str(tmp_path)}, "s1")]

    async def test_a_directory_outside_the_project_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(HostModelError) as caught:
            await self.members(tmp_path, Gateway({})).list_dir(
                a_call(dir="../elsewhere", grants=grants(workspace="read"))
            )
        assert refusal(caught) == (ErrorCode.REFUSED, "'../elsewhere' is outside the project")

    async def test_a_read_asks_for_the_path_relative_to_the_project(self, tmp_path: Path) -> None:
        gateway = Gateway({RpcMethod.READ_FILE: {"content": "hi"}})
        got = await self.members(tmp_path, gateway).read(
            a_call(path=str(tmp_path / "docs" / "a.md"), grants=grants(workspace="read"))
        )
        assert got == {"content": "hi"}
        assert gateway.calls == [(RpcMethod.READ_FILE, {"path": "docs/a.md"}, "s1")]

    async def test_a_read_outside_the_workspace_paths_is_not_granted_and_asks_nothing(
        self, tmp_path: Path
    ) -> None:
        gateway = Gateway({})
        with pytest.raises(HostModelError) as caught:
            await self.members(tmp_path, gateway).read(
                a_call(path="src/x.py", grants=grants(workspace="read", workspacePaths=["docs/*"]))
            )
        assert refusal(caught) == (
            ErrorCode.NOT_GRANTED,
            "'src/x.py' is outside 'acme.reviewer''s workspacePaths",
        )
        assert gateway.calls == []
