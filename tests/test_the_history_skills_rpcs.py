"""The history and skills RPCs, and the declarations the workflow and trust RPCs carry.

``get_chat_history``, ``search_chat``, ``truncate_history``,
``count_context_tokens`` and ``get_context_breakdown`` hand a session's id
(and their other arguments) to the backend and return what it answers; the
skill RPCs read the session's loaded skills. The skills come from a real
:class:`SkillPool` over real ``SKILL.md`` files; the backend is the
collaborator the history handler forwards to, so it is a stub that records
what it was asked. The registration of each method (name, duration, scope,
streams, correlation) is what the generated SDKs read.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from igni_core.backend.rpc_binding import Correlation, NoArgs, RpcArgsError, RpcBinding
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.rpc_handlers.session_args import (
    SearchChatArgs,
    SessionIdArgs,
    TruncateHistoryArgs,
)
from igni_core.backend.rpc_handlers.session_history import SessionHistoryRpcHandler
from igni_core.backend.rpc_handlers.skills import SkillsRpcHandler
from igni_core.backend.rpc_handlers.workflows import WorkflowsRpcHandler
from igni_core.backend.rpc_handlers.workspace_trust import (
    TrustDecisionArgs,
    WorkspaceTrustRpcHandler,
)
from igni_core.core.skills.loader import SkillPool, SkillPriority
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class _Backend:
    """Records every history call and answers with canned values."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def get_chat_history(self, session_id: str) -> list[dict]:
        self.calls.append(("get_chat_history", (session_id,)))
        return [{"role": "user", "content": "hi"}]

    async def search_chat(self, session_id: str, query: str, limit: int) -> list[dict]:
        self.calls.append(("search_chat", (session_id, query, limit)))
        return []

    async def truncate_history(self, session_id: str, run_id: str) -> dict:
        self.calls.append(("truncate_history", (session_id, run_id)))
        return {"removed": 2, "error": ""}

    async def count_context_tokens(self) -> int:
        self.calls.append(("count_context_tokens", ()))
        return 0

    async def context_breakdown(self) -> dict:
        self.calls.append(("context_breakdown", ()))
        return {"total": 0, "runs": 0, "floor": 0}


def _history() -> tuple[SessionHistoryRpcHandler, _Backend]:
    backend = _Backend()
    ctx: Any = SimpleNamespace(backend=backend)
    return SessionHistoryRpcHandler(ctx), backend


def _binding(handler: type[RpcHandler], method: RpcMethod) -> RpcBinding:
    return RpcBinding.on(handler)[method][1]


async def test_history_is_read_for_the_named_session_and_returned_as_is() -> None:
    handler, backend = _history()
    out = await handler.methods()[RpcMethod.GET_CHAT_HISTORY]({"session_id": "s1"})
    assert out == [{"role": "user", "content": "hi"}]
    assert backend.calls == [("get_chat_history", ("s1",))]


async def test_search_forwards_session_query_and_limit_with_a_default_of_fifty() -> None:
    handler, backend = _history()
    search = handler.methods()[RpcMethod.SEARCH_CHAT]
    await search({"session_id": "s1", "query": "Ünï"})
    await search({"session_id": "s2", "query": "x", "limit": 0})
    await search({"session_id": "s3", "query": "", "limit": -1})
    assert backend.calls == [
        ("search_chat", ("s1", "Ünï", 50)),
        ("search_chat", ("s2", "x", 0)),
        ("search_chat", ("s3", "", -1)),
    ]


async def test_truncate_passes_session_then_run_and_returns_the_result_unchanged() -> None:
    handler, backend = _history()
    out = await handler.methods()[RpcMethod.TRUNCATE_HISTORY]({"session_id": "s1", "run_id": "r9"})
    assert out == {"removed": 2, "error": ""}
    assert backend.calls == [("truncate_history", ("s1", "r9"))]


async def test_the_context_calls_answer_what_the_backend_measured() -> None:
    handler, backend = _history()
    assert await handler.methods()[RpcMethod.COUNT_CONTEXT_TOKENS]({}) == 0
    assert await handler.methods()[RpcMethod.GET_CONTEXT_BREAKDOWN]({}) == {
        "total": 0,
        "runs": 0,
        "floor": 0,
    }
    assert [name for name, _ in backend.calls] == ["count_context_tokens", "context_breakdown"]


@pytest.mark.parametrize(
    ("method", "wire", "named"),
    [
        (RpcMethod.GET_CHAT_HISTORY, {}, "session_id"),
        (RpcMethod.SEARCH_CHAT, {"session_id": "s"}, "query"),
        (RpcMethod.SEARCH_CHAT, {"query": "q"}, "session_id"),
        (RpcMethod.TRUNCATE_HISTORY, {"session_id": "s"}, "run_id"),
        (RpcMethod.COUNT_CONTEXT_TOKENS, {"extra": 1}, "extra"),
        (RpcMethod.GET_CONTEXT_BREAKDOWN, {"extra": 1}, "extra"),
    ],
)
async def test_a_missing_or_unexpected_argument_is_refused_naming_the_method(
    method: RpcMethod, wire: dict, named: str
) -> None:
    handler, backend = _history()
    with pytest.raises(RpcArgsError, match=f"{method.value}.*{named}"):
        await handler.methods()[method](wire)
    assert backend.calls == []


@pytest.mark.parametrize(
    ("method", "args", "duration"),
    [
        (RpcMethod.GET_CHAT_HISTORY, SessionIdArgs, RpcDuration.IO),
        (RpcMethod.SEARCH_CHAT, SearchChatArgs, RpcDuration.IO),
        (RpcMethod.TRUNCATE_HISTORY, TruncateHistoryArgs, RpcDuration.IO),
        (RpcMethod.COUNT_CONTEXT_TOKENS, NoArgs, RpcDuration.IO),
        (RpcMethod.GET_CONTEXT_BREAKDOWN, NoArgs, RpcDuration.IO),
    ],
)
def test_each_history_rpc_is_registered_with_its_arguments_duration_and_scope(
    method: RpcMethod, args: type, duration: RpcDuration
) -> None:
    binding = _binding(SessionHistoryRpcHandler, method)
    assert binding.args is args
    assert binding.duration is duration
    assert binding.scope is RpcScope.SESSION
    assert binding.streams == ()
    assert set(SessionHistoryRpcHandler(MagicMock()).methods()) == {
        RpcMethod.GET_CHAT_HISTORY,
        RpcMethod.SEARCH_CHAT,
        RpcMethod.TRUNCATE_HISTORY,
        RpcMethod.COUNT_CONTEXT_TOKENS,
        RpcMethod.GET_CONTEXT_BREAKDOWN,
    }


# -- skills ---------------------------------------------------------------


def _skill(root: Path, name: str, description: str, body: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{description}"\n---\n{body}', encoding="utf-8"
    )


class _SkillBackend:
    def __init__(self, pool: SkillPool) -> None:
        self._pool = pool
        self.skill_names = [s.name for s in pool.list_skills()]

    def get_skill_pool(self) -> SkillPool:
        return self._pool

    def get_skill_details(self) -> list[str]:
        return ["details"]


def _skills(tmp_path: Path, *skills: tuple[str, str, str]) -> SkillsRpcHandler:
    pool = SkillPool()
    for name, description, body in skills:
        _skill(tmp_path, name, description, body)
    pool.load_directory(tmp_path, priority=SkillPriority.PROJECT_EMBER)
    ctx: Any = SimpleNamespace(backend=_SkillBackend(pool))
    return SkillsRpcHandler(ctx)


async def test_a_skill_is_listed_with_its_name_description_and_body_as_the_prompt(
    tmp_path: Path,
) -> None:
    handler = _skills(
        tmp_path,
        ("deploy", "Ship it", "Run the deploy.\n"),
        ("bare", "", ""),
        ("grüße", "Ünïcode", "Hallo 🌍"),
    )
    out = await handler.methods()[RpcMethod.GET_SKILL_DEFINITIONS]({})
    by_name = {d.name: d for d in out}
    assert set(by_name) == {"deploy", "bare", "grüße"}
    assert (by_name["deploy"].description, by_name["deploy"].prompt) == (
        "Ship it",
        "Run the deploy.",
    )
    assert (by_name["bare"].description, by_name["bare"].prompt) == ("", "")
    assert (by_name["grüße"].description, by_name["grüße"].prompt) == ("Ünïcode", "Hallo 🌍")
    assert set(out[0].model_dump()) == {"name", "description", "prompt"}


async def test_a_session_with_no_skills_lists_none(tmp_path: Path) -> None:
    handler = _skills(tmp_path)
    assert await handler.methods()[RpcMethod.GET_SKILL_DEFINITIONS]({}) == []
    assert handler.methods()[RpcMethod.GET_SKILL_NAMES]({}) == []


def test_names_and_details_are_what_the_backend_loaded(tmp_path: Path) -> None:
    handler = _skills(tmp_path, ("a", "A", "x"), ("b", "B", "y"))
    assert handler.methods()[RpcMethod.GET_SKILL_NAMES]({}) == ["a", "b"]
    assert handler.methods()[RpcMethod.GET_SKILL_DETAILS]({}) == ["details"]


def test_the_skill_rpcs_take_no_arguments(tmp_path: Path) -> None:
    handler = _skills(tmp_path)
    for method in (
        RpcMethod.GET_SKILL_NAMES,
        RpcMethod.GET_SKILL_DEFINITIONS,
        RpcMethod.GET_SKILL_DETAILS,
    ):
        with pytest.raises(RpcArgsError, match=method.value):
            handler.methods()[method]({"extra": 1})
        binding = _binding(SkillsRpcHandler, method)
        assert binding.args is NoArgs
        assert binding.scope is RpcScope.SESSION


# -- the declarations of the workflow and trust RPCs ----------------------


def test_run_and_cancel_workflow_say_how_their_pushes_are_told_apart() -> None:
    run = _binding(WorkflowsRpcHandler, RpcMethod.RUN_WORKFLOW)
    cancel = _binding(WorkflowsRpcHandler, RpcMethod.CANCEL_WORKFLOW)
    assert run.correlate == Correlation(
        by="payload.workflow_run_id", source="result.workflow_run_id"
    )
    assert cancel.correlate == Correlation(
        by="payload.workflow_run_id", source="args.workflow_run_id"
    )
    assert run.streams == cancel.streams == (PushChannel.WORKFLOW_EVENT,)
    listed = _binding(WorkflowsRpcHandler, RpcMethod.LIST_WORKFLOWS)
    assert listed.streams == () and listed.correlate is None
    for binding in (run, cancel, listed):
        assert binding.scope is RpcScope.SESSION
        assert binding.duration is RpcDuration.IO


def test_the_trust_rpcs_are_declared_with_their_stream_and_correlation() -> None:
    get = _binding(WorkspaceTrustRpcHandler, RpcMethod.GET_WORKSPACE_TRUST)
    set_ = _binding(WorkspaceTrustRpcHandler, RpcMethod.SET_WORKSPACE_TRUST)
    assert get.args is NoArgs and get.streams == () and get.correlate is None
    assert set_.args is TrustDecisionArgs
    assert set_.streams == (PushChannel.WORKSPACE_TRUST_REQUIRED,)
    assert set_.correlate == Correlation(by="payload.path", source="result.path")
    assert get.scope is set_.scope is RpcScope.SESSION
    assert get.duration is set_.duration is RpcDuration.IO
