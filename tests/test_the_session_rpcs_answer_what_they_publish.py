"""The session RPCs, called by their wire arguments, over a real backend.

The backend is built the way a boot builds it — real settings, project
folder and session — and every call goes through the real dispatch table,
so what is asserted is what a window gets back: the values, the typed
results and the refusals of arguments.

Pinned:

* the status calls answer the session's own values, and a switch to an
  unknown id starts an empty session under it;
* an upload is stored and read back, a clash gets a ``-N`` suffix and bad
  base64 is told in ``error``;
* interrupted runs are listed from the store and discarded only for the
  session and run named; an empty ``message_id`` is refused in the answer;
* ``cancel_run`` and ``cancel_agent_run`` say what they did;
* ``shutdown`` winds the session down once, however often it is asked;
  ``fire_session_start_hook`` runs every time and a failed revalidation
  does not stop it;
* each call declares its scope, duration and the stream a cancel ends.
"""

from __future__ import annotations

import base64
import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend import server_context
from igni_core.backend.rpc_binding import Correlation, RpcArgsError, RpcBinding
from igni_core.backend.rpc_handlers.session import SessionRpcHandler
from igni_core.backend.schemas_run import CancelAgentRunResult, CancelRunResult
from igni_core.backend.server import BackendServer
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from tests.rpc_tables import rpc_table_for


@pytest.fixture
def backend(tmp_path) -> BackendServer:
    return BackendServer(SettingsLoader.load(project_dir=tmp_path), project_dir=tmp_path)


@pytest.fixture
def table(backend):
    return rpc_table_for(backend, MagicMock())


async def _ask(table: dict, method: RpcMethod, wire: dict[str, Any] | None = None) -> Any:
    result = table[method](wire or {})
    if hasattr(result, "__await__"):
        return await result
    return result


# ── what the session says about itself ────────────────────────────


async def test_the_status_calls_answer_the_sessions_own_values(backend, table, tmp_path):
    assert await _ask(table, RpcMethod.GET_SESSION_ID) == backend.session_id
    assert await _ask(table, RpcMethod.GET_PROJECT_DIR) == str(tmp_path)
    assert await _ask(table, RpcMethod.GET_PROCESSING) is False
    assert await _ask(table, RpcMethod.GET_RUN_TIMEOUT) == backend.settings.models.max_run_timeout
    status = await _ask(table, RpcMethod.GET_STATUS)
    assert isinstance(status, msg.StatusUpdate)
    assert status.model == backend.settings.models.default
    listed = await _ask(table, RpcMethod.LIST_SESSIONS)
    assert isinstance(listed, msg.SessionListResult)


async def test_switching_to_an_unknown_session_starts_an_empty_one_under_that_id(backend, table):
    info = await _ask(table, RpcMethod.SWITCH_SESSION, {"session_id": "not-stored-anywhere"})

    assert isinstance(info, msg.Info)
    assert "not-stored-anywhere" in info.text
    assert await _ask(table, RpcMethod.GET_SESSION_ID) == "not-stored-anywhere"
    assert (
        await _ask(table, RpcMethod.GET_INTERRUPTED_RUNS, {"session_id": "not-stored-anywhere"})
        == []
    )


async def test_a_call_naming_a_session_without_its_id_is_refused(table):
    with pytest.raises(RpcArgsError, match="switch_session: 'session_id' is required"):
        table[RpcMethod.SWITCH_SESSION]({})
    with pytest.raises(RpcArgsError, match="get_pending_messages: 'session_id' is required"):
        table[RpcMethod.GET_PENDING_MESSAGES]({"sessionId": "s"})


async def test_a_key_a_newer_client_adds_is_ignored(table, backend):
    answer = await _ask(table, RpcMethod.GET_PENDING_MESSAGES, {"session_id": "s", "newer": 1})

    assert answer == []


async def test_a_message_just_received_is_not_yet_pending(backend, table):
    backend.controllers.context._pending_store.record_received("s-1", "hello")

    assert await _ask(table, RpcMethod.GET_PENDING_MESSAGES, {"session_id": "s-1"}) == []


async def test_a_message_left_unanswered_for_over_a_minute_is_pending_oldest_first(
    backend, table, monkeypatch
):
    store = backend.controllers.context._pending_store
    first = store.record_received("s-1", "first")
    store.record_received("s-2", "someone else's")
    later = time.time() + 120
    monkeypatch.setattr(server_context.time, "time", lambda: later)

    pending = await _ask(table, RpcMethod.GET_PENDING_MESSAGES, {"session_id": "s-1"})

    assert [(p.message_id, p.content) for p in pending] == [(first, "first")]


# ── uploads ───────────────────────────────────────────────────────


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def test_an_upload_is_stored_with_its_size_and_a_clash_gets_a_suffix(backend, table):
    first = await _ask(
        table,
        RpcMethod.UPLOAD_ATTACHMENT,
        {"filename": "note.txt", "content_base64": _b64(b"hello")},
    )
    second = await _ask(
        table,
        RpcMethod.UPLOAD_ATTACHMENT,
        {"filename": "note.txt", "content_base64": _b64(b"other!")},
    )

    assert first.error == "" and first.size == 5
    assert second.error == "" and second.size == 6
    assert first.path != second.path
    assert "note" in first.path and "note" in second.path
    assert "NOTE" not in first.path
    assert backend.read_file(first.path).contents == "hello"
    assert backend.read_file(second.path).contents == "other!"


async def test_an_upload_that_is_not_base64_is_told_in_the_answer(table):
    answer = await _ask(
        table, RpcMethod.UPLOAD_ATTACHMENT, {"filename": "x.txt", "content_base64": "!!!not base64"}
    )

    assert answer.path == "" and answer.size == 0
    assert answer.error.startswith("invalid base64")


async def test_an_upload_names_its_file_and_its_bytes(table):
    with pytest.raises(RpcArgsError, match="'content_base64' is required"):
        table[RpcMethod.UPLOAD_ATTACHMENT]({"filename": "x"})
    with pytest.raises(RpcArgsError, match="'filename' is required"):
        table[RpcMethod.UPLOAD_ATTACHMENT]({"content_base64": ""})


# ── interrupted runs ──────────────────────────────────────────────


async def test_an_interrupted_run_is_listed_and_discarded_only_for_the_session_and_run_named(
    backend, table
):
    store = backend.controllers.context._pending_store
    mine = store.record_received("s-a", "build it")
    store.mark_interrupted(mine, "cancelled")
    theirs = store.record_received("s-b", "theirs")
    store.mark_interrupted(theirs, "cancelled")

    (row,) = await _ask(table, RpcMethod.GET_INTERRUPTED_RUNS, {"session_id": "s-a"})
    assert (row.message_id, row.content, row.reason) == (mine, "build it", "cancelled")

    wrong_session = await _ask(
        table, RpcMethod.DISCARD_INTERRUPTED_RUN, {"session_id": "s-a", "message_id": theirs}
    )
    assert wrong_session.ok is True
    assert len(await _ask(table, RpcMethod.GET_INTERRUPTED_RUNS, {"session_id": "s-b"})) == 1
    assert len(await _ask(table, RpcMethod.GET_INTERRUPTED_RUNS, {"session_id": "s-a"})) == 1

    done = await _ask(
        table, RpcMethod.DISCARD_INTERRUPTED_RUN, {"session_id": "s-a", "message_id": mine}
    )
    again = await _ask(
        table, RpcMethod.DISCARD_INTERRUPTED_RUN, {"session_id": "s-a", "message_id": mine}
    )
    assert (done.ok, again.ok) == (True, True)
    assert await _ask(table, RpcMethod.GET_INTERRUPTED_RUNS, {"session_id": "s-a"}) == []


async def test_discarding_with_an_empty_message_id_is_refused_in_the_answer(table):
    answer = await _ask(
        table, RpcMethod.DISCARD_INTERRUPTED_RUN, {"session_id": "s-a", "message_id": ""}
    )

    assert answer.ok is False
    assert answer.error == "message_id is required"
    with pytest.raises(RpcArgsError, match="'message_id' is required"):
        table[RpcMethod.DISCARD_INTERRUPTED_RUN]({"session_id": "s-a"})


# ── cancelling ────────────────────────────────────────────────────


async def test_cancelling_with_no_turn_running_says_nothing_was_cancelled(table):
    answer = await _ask(table, RpcMethod.CANCEL_RUN)

    assert answer == CancelRunResult(cancelled=False)


@pytest.mark.parametrize(
    "wire", [{}, {"run_id": ""}, {"run_id": "run-never-started-by-the-wire-test"}]
)
async def test_cancelling_a_sub_agent_run_that_is_not_running_is_not_ok(table, wire):
    answer = await _ask(table, RpcMethod.CANCEL_AGENT_RUN, wire)

    assert isinstance(answer, CancelAgentRunResult)
    assert answer.ok is False
    assert answer.error


# ── winding down ──────────────────────────────────────────────────


async def test_a_session_is_wound_down_once_however_often_it_is_asked(backend, table):
    execute = AsyncMock()
    backend._session.hook_executor.execute = execute

    first = await _ask(table, RpcMethod.SHUTDOWN)
    again = await _ask(table, RpcMethod.SHUTDOWN)

    assert first is None and again is None
    assert execute.await_count == 1


async def test_the_start_hook_runs_every_time_and_a_failed_revalidation_does_not_stop_it(
    backend, table, caplog
):
    fired = AsyncMock()
    backend._session.fire_session_start_hook = fired
    backend.controllers.group_policy.revalidate_for_new_dialogue = AsyncMock(
        return_value=SimpleNamespace(error="pack unreachable")
    )

    with caplog.at_level(logging.WARNING):
        assert await _ask(table, RpcMethod.FIRE_SESSION_START_HOOK) is None
        assert await _ask(table, RpcMethod.FIRE_SESSION_START_HOOK) is None

    assert fired.await_count == 2
    assert "pack unreachable" in caplog.text


# ── what each call declares ───────────────────────────────────────


def test_each_call_declares_how_long_it_takes_and_what_it_acts_on():
    bindings = RpcBinding.on(SessionRpcHandler)
    io = {
        RpcMethod.SHUTDOWN,
        RpcMethod.UPLOAD_ATTACHMENT,
        RpcMethod.GET_PENDING_MESSAGES,
        RpcMethod.GET_INTERRUPTED_RUNS,
        RpcMethod.DISCARD_INTERRUPTED_RUN,
        RpcMethod.LIST_SESSIONS,
        RpcMethod.SWITCH_SESSION,
    }
    for method, (_, binding) in bindings.items():
        expected = (
            RpcDuration.LONG
            if method is RpcMethod.FIRE_SESSION_START_HOOK
            else RpcDuration.IO
            if method in io
            else RpcDuration.INSTANT
        )
        assert binding.duration is expected, method
    assert bindings[RpcMethod.CANCEL_AGENT_RUN][1].scope is RpcScope.IGNI
    assert bindings[RpcMethod.CANCEL_RUN][1].scope is RpcScope.SESSION


def test_cancelling_a_run_declares_the_stream_it_ends_and_how_to_tell_it():
    binding = RpcBinding.on(SessionRpcHandler)[RpcMethod.CANCEL_RUN][1]

    assert binding.correlate == Correlation(by="session_id", source="request.session_id")
    assert binding.streams == (msg.Info, msg.StreamEnd)
