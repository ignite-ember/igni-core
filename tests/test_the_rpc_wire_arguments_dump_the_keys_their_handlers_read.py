"""The host's RPC argument models: the wire keys each handler reads.

A member hands the backend's handler ``args.model_dump()``; the handler
reads those keys by name. These pin the keys, that an optional left empty
is still sent as ``None``, that a missing required field is refused, and
that the argument objects cannot be changed once built.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.extensions.host.rpc_wire.base import NO_ARGS, NoArgs, RpcArgs
from igni_core.extensions.host.rpc_wire.chat import (
    CompactArgs,
    DiscardInterruptedArgs,
    DispatchViewArgs,
    LearningsArgs,
    RunIdArgs,
    SearchChatArgs,
    SessionArgs,
    TruncateArgs,
    UploadArgs,
)
from igni_core.extensions.host.rpc_wire.sessions import SwitchSessionArgs
from igni_core.extensions.host.rpc_wire.settings import (
    CheckPermissionArgs,
    InstallPluginArgs,
    PluginEnabledArgs,
    PluginNameArgs,
    PreviewPluginArgs,
    RefreshMarketplacesArgs,
    SaveRuleArgs,
    ServerNameArgs,
    SwitchModelArgs,
    ToolEnabledArgs,
    UpdatePluginArgs,
    UrlArgs,
)
from igni_core.extensions.host.rpc_wire.work import (
    AgentNameArgs,
    CancelWorkflowArgs,
    ExecuteTaskArgs,
    PidArgs,
    PromoteArgs,
    RunWorkflowArgs,
    ScheduledTaskArgs,
    ScheduledTasksArgs,
    ShellArgs,
    TailArgs,
)
from igni_core.extensions.host.rpc_wire.workspace import CompleteArgs, PathArgs, QueryArgs

DUMPS: list[tuple[RpcArgs, dict[str, Any]]] = [
    (SessionArgs(session_id="s"), {"session_id": "s"}),
    (
        SearchChatArgs(session_id="s", query="q", limit=5),
        {"session_id": "s", "query": "q", "limit": 5},
    ),
    (RunIdArgs(run_id="r"), {"run_id": "r"}),
    (TruncateArgs(session_id="s", run_id="r"), {"session_id": "s", "run_id": "r"}),
    (CompactArgs(ctx_tokens=10, max_ctx=100), {"ctx_tokens": 10, "max_ctx": 100}),
    (
        LearningsArgs(user_msg="u", assistant_msg="a"),
        {"user_msg": "u", "assistant_msg": "a"},
    ),
    (
        DiscardInterruptedArgs(session_id="s", message_id="m"),
        {"session_id": "s", "message_id": "m"},
    ),
    (
        UploadArgs(filename="f.txt", content_base64="aGk="),
        {"filename": "f.txt", "content_base64": "aGk="},
    ),
    (SwitchSessionArgs(session_id="s"), {"session_id": "s"}),
    (SwitchModelArgs(model_name="m"), {"model_name": "m"}),
    (ServerNameArgs(server_name="srv"), {"server_name": "srv"}),
    (
        ToolEnabledArgs(server="srv", tool="t", enabled=False),
        {"server": "srv", "tool": "t", "enabled": False},
    ),
    (PluginNameArgs(name="p"), {"name": "p"}),
    (RefreshMarketplacesArgs(name=None), {"name": None}),
    (RefreshMarketplacesArgs(name="mk"), {"name": "mk"}),
    (
        PreviewPluginArgs(source="src", branch=None, subdir=None),
        {"source": "src", "branch": None, "subdir": None},
    ),
    (
        PreviewPluginArgs(source="src", branch="b", subdir="d"),
        {"source": "src", "branch": "b", "subdir": "d"},
    ),
    (InstallPluginArgs(ref="r", install_ref=None), {"ref": "r", "install_ref": None}),
    (InstallPluginArgs(ref="r", install_ref="i"), {"ref": "r", "install_ref": "i"}),
    (UpdatePluginArgs(name="p", install_ref=None), {"name": "p", "install_ref": None}),
    (UpdatePluginArgs(name="p", install_ref="i"), {"name": "p", "install_ref": "i"}),
    (PluginEnabledArgs(name="p", enabled=True), {"name": "p", "enabled": True}),
    (PluginEnabledArgs(name="p", enabled=False), {"name": "p", "enabled": False}),
    (UrlArgs(url="https://x"), {"url": "https://x"}),
    (
        CheckPermissionArgs(tool_name="t", func_name="f", tool_args={}),
        {"tool_name": "t", "func_name": "f", "tool_args": {}},
    ),
    (SaveRuleArgs(rule="r", level="project"), {"rule": "r", "level": "project"}),
    (PromoteArgs(name="n", target="user"), {"name": "n", "target": "user"}),
    (AgentNameArgs(name="n"), {"name": "n"}),
    (
        RunWorkflowArgs(name="w", args={"a": 1}, session_id="s"),
        {"name": "w", "args": {"a": 1}, "session_id": "s"},
    ),
    (CancelWorkflowArgs(workflow_run_id="w1"), {"workflow_run_id": "w1"}),
    (ScheduledTasksArgs(include_done=True), {"include_done": True}),
    (ScheduledTasksArgs(include_done=False), {"include_done": False}),
    (ScheduledTaskArgs(task_id="t"), {"task_id": "t"}),
    (ExecuteTaskArgs(description="déjà vu ✓"), {"description": "déjà vu ✓"}),
    (TailArgs(pid=0, tail=0), {"pid": 0, "tail": 0}),
    (TailArgs(pid=-3, tail=50), {"pid": -3, "tail": 50}),
    (PidArgs(pid=42), {"pid": 42}),
    (ShellArgs(command=""), {"command": ""}),
    (PathArgs(path="ünï/ö.txt"), {"path": "ünï/ö.txt"}),
    (CompleteArgs(query="", limit=0), {"query": "", "limit": 0}),
    (CompleteArgs(query="a", limit=10**9), {"query": "a", "limit": 10**9}),
    (QueryArgs(query="q"), {"query": "q"}),
]


@pytest.mark.parametrize(
    ("args", "wire"), DUMPS, ids=[f"{type(a).__name__}-{i}" for i, (a, _) in enumerate(DUMPS)]
)
def test_an_argument_model_dumps_exactly_the_keys_its_handler_reads(
    args: RpcArgs, wire: dict[str, Any]
) -> None:
    assert args.model_dump() == wire


REQUIRED: list[tuple[type[RpcArgs], dict[str, Any]]] = [
    (SessionArgs, {}),
    (SearchChatArgs, {"session_id": "s", "query": "q"}),
    (SwitchSessionArgs, {}),
    (ToolEnabledArgs, {"server": "s", "tool": "t"}),
    (RefreshMarketplacesArgs, {}),
    (PreviewPluginArgs, {"source": "x", "branch": None}),
    (InstallPluginArgs, {"ref": "r"}),
    (RunWorkflowArgs, {"name": "w", "args": {}}),
    (TailArgs, {"pid": 1}),
    (ShellArgs, {}),
    (PathArgs, {}),
    (CompleteArgs, {"query": "q"}),
]


@pytest.mark.parametrize("model", [m for m, _ in REQUIRED], ids=[m.__name__ for m, _ in REQUIRED])
def test_a_missing_required_field_is_refused_not_defaulted(model: type[RpcArgs]) -> None:
    given = dict(REQUIRED)[model]
    with pytest.raises(ValidationError):
        model(**given)


def test_a_view_action_carries_its_view_id_inside_params_only() -> None:
    args = DispatchViewArgs(action="open", params={"x": 1}, view_id="v1")
    assert args.model_dump() == {"action": "open", "params": {"x": 1, "view_id": "v1"}}


def test_the_named_view_wins_over_a_view_id_the_caller_put_in_params() -> None:
    args = DispatchViewArgs(action="a", params={"view_id": "other", "k": "v"}, view_id="mine")
    assert args.model_dump()["params"] == {"view_id": "mine", "k": "v"}


def test_a_view_action_with_no_params_still_names_its_view() -> None:
    assert DispatchViewArgs(action="a", params={}, view_id="v").model_dump() == {
        "action": "a",
        "params": {"view_id": "v"},
    }


def test_dumping_a_view_action_leaves_the_callers_params_alone() -> None:
    params: dict[str, Any] = {"view_id": "other", "k": 1}
    args = DispatchViewArgs(action="a", params=params, view_id="mine")
    args.model_dump()
    assert params == {"view_id": "other", "k": 1}
    assert args.params == {"view_id": "other", "k": 1}


def test_a_view_action_needs_a_text_view_id() -> None:
    with pytest.raises(ValidationError):
        DispatchViewArgs(action="a", params={}, view_id=3)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        DispatchViewArgs(action="a", params={})  # type: ignore[call-arg]


def test_a_handler_without_arguments_is_handed_nothing_to_read() -> None:
    assert NO_ARGS.model_dump() == {}
    assert isinstance(NO_ARGS, NoArgs)


def test_arguments_cannot_be_changed_once_built() -> None:
    args = SessionArgs(session_id="s")
    with pytest.raises(ValidationError):
        args.session_id = "other"  # type: ignore[misc]
    assert args.model_dump() == {"session_id": "s"}
