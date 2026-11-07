"""Tests for the workflow RPC handlers.

Covers :class:`WorkflowsRpcHandler`, behind the ``list_workflows``,
``run_workflow`` and ``cancel_workflow`` RPCs the FE calls from the
``/workflows`` slash command. The handler reads a request model,
delegates to its :class:`WorkflowRunner` and returns a response
model, so the runner is the one seam mocked here; the context is a
real :class:`RpcHandlerContext` and the backend a stub carrying the
session the call was routed to.

Wire shapes are asserted through the dispatcher's ``_serialize``, the
path production takes, so the ``whenToUse`` alias is checked where it
matters.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.message_dispatcher import _serialize
from igni_core.backend.rpc_handlers.base import RpcHandlerContext
from igni_core.backend.rpc_handlers.workflows import WorkflowsRpcHandler
from igni_core.backend.schemas_workflows import (
    WorkflowCancelOutcome,
    WorkflowMeta,
    WorkflowMetaEnvelope,
    WorkflowPhase,
    WorkflowRunIdentity,
    WorkflowRunRequest,
    WorkflowStartResult,
)
from igni_core.backend.workflow_runner import WorkflowRunner
from igni_core.core.paths import CONFIG_DIR
from igni_core.protocol.rpc import RpcMethod


def _envelope(
    name: str = "smoke", path: str = ".claude/workflows/smoke.mjs"
) -> WorkflowMetaEnvelope:
    """A canned discovery result the runner would emit."""
    return WorkflowMetaEnvelope(
        meta=WorkflowMeta(
            name=name,
            description=f"smoke {name}",
            whenToUse="tests",
            phases=[WorkflowPhase(title="plan")],
        ),
        path=path,
    )


def _context(runner: WorkflowRunner, session: object) -> RpcHandlerContext:
    return RpcHandlerContext(
        backend=SimpleNamespace(session=session),
        transport=None,
        login=MagicMock(),
        push=MagicMock(),
        workflows=runner,
    )


def _started(workflow_run_id: str, name: str) -> WorkflowStartResult:
    return WorkflowStartResult.started(
        WorkflowRunIdentity(workflow_run_id=workflow_run_id, name=name)
    )


def _make_handler(session: object | None = None) -> tuple[WorkflowsRpcHandler, AsyncMock]:
    runner = AsyncMock(spec=WorkflowRunner)
    handler = WorkflowsRpcHandler(_context(runner, session or object()))
    return handler, runner


async def test_list_workflows_returns_discovery_output() -> None:
    """Each discovered workflow becomes one flat row on the wire."""
    handler, runner = _make_handler()
    runner.list_workflows.return_value = [
        _envelope("alpha", ".claude/workflows/alpha.mjs"),
        _envelope("beta", f"{CONFIG_DIR}/workflows/beta.mjs"),
    ]

    out = _serialize(await handler.list_workflows({}))

    assert out == [
        {
            "name": "alpha",
            "description": "smoke alpha",
            "whenToUse": "tests",
            "phases": [{"title": "plan", "detail": ""}],
            "path": ".claude/workflows/alpha.mjs",
        },
        {
            "name": "beta",
            "description": "smoke beta",
            "whenToUse": "tests",
            "phases": [{"title": "plan", "detail": ""}],
            "path": f"{CONFIG_DIR}/workflows/beta.mjs",
        },
    ]


async def test_list_workflows_ignores_the_routing_session_id() -> None:
    handler, runner = _make_handler()
    runner.list_workflows.return_value = [_envelope("alpha")]

    out = await handler.list_workflows({"session_id": "s"})

    assert [item.name for item in out] == ["alpha"]


async def test_run_workflow_returns_workflow_run_id_without_blocking() -> None:
    """``run_workflow`` returns the id the runner emitted; the run
    itself streams events later."""
    session = object()
    handler, runner = _make_handler(session)
    runner.run.return_value = _started("wf_abc123def456", "refactor-to-standards")

    out = await handler.run_workflow(
        {
            "name": "refactor-to-standards",
            "args": {"file": "src/example.py"},
            "session_id": "sess_xyz",
        }
    )

    assert _serialize(out) == {
        "workflow_run_id": "wf_abc123def456",
        "name": "refactor-to-standards",
    }
    runner.run.assert_awaited_once_with(
        request=WorkflowRunRequest(name="refactor-to-standards", args={"file": "src/example.py"}),
        session=session,
    )


async def test_run_workflow_runs_in_the_routed_session_whatever_the_session_id() -> None:
    """The call was routed to its session before the handler ran, so
    the run belongs to ``ctx.backend.session``. Looking the id up
    again used to call a ``find`` that ``SessionsController`` doesn't
    have, so every call with a session id failed."""
    session = object()
    handler, runner = _make_handler(session)
    runner.run.return_value = _started("wf_1", "smoke")

    for session_id in ("", "sess_known", "sess_unknown"):
        await handler.run_workflow({"name": "smoke", "session_id": session_id})
        assert runner.run.await_args.kwargs["session"] is session


async def test_run_workflow_defaults_args_to_empty_dict() -> None:
    handler, runner = _make_handler()
    runner.run.return_value = _started("wf_q", "smoke")

    await handler.run_workflow({"name": "smoke", "args": None})

    assert runner.run.await_args.kwargs["request"].args == {}


async def test_run_workflow_surfaces_file_not_found() -> None:
    """An unknown workflow name propagates as an RPC error."""
    handler, runner = _make_handler()
    runner.run.return_value = WorkflowStartResult.not_found("workflow 'missing' not found")

    with pytest.raises(FileNotFoundError, match="missing"):
        await handler.run_workflow({"name": "missing"})


async def test_run_workflow_surfaces_runtime_error() -> None:
    handler, runner = _make_handler()
    runner.run.return_value = WorkflowStartResult.runtime_missing(
        "workflow runtime missing at /tmp/x.mjs"
    )

    with pytest.raises(RuntimeError, match="workflow runtime missing"):
        await handler.run_workflow({"name": "smoke"})


@pytest.mark.parametrize("args", [{}, {"name": ""}, {"session_id": "sess_x"}])
async def test_run_workflow_rejects_a_missing_name(args: dict) -> None:
    """A missing name is a caller bug: refused before the runner is
    touched, in the wording callers have always seen."""
    handler, runner = _make_handler()

    with pytest.raises(ValueError) as caught:
        await handler.run_workflow(args)

    assert str(caught.value) == "run_workflow: 'name' is required"
    runner.run.assert_not_called()


async def test_run_workflow_rejects_non_dict_args() -> None:
    handler, runner = _make_handler()

    with pytest.raises(ValueError) as caught:
        await handler.run_workflow({"name": "smoke", "args": "oops"})

    assert str(caught.value) == "run_workflow: 'args' must be a dict, got str"
    runner.run.assert_not_called()


async def test_run_workflow_takes_a_numeric_name_as_text() -> None:
    handler, runner = _make_handler()
    runner.run.return_value = _started("wf_n", "7")

    out = await handler.run_workflow({"name": 7})

    assert out.name == "7"


async def test_cancel_workflow_returns_cancelled_true_when_run_exists() -> None:
    handler, runner = _make_handler()
    runner.cancel.return_value = WorkflowCancelOutcome.CANCELLED

    out = await handler.cancel_workflow({"workflow_run_id": "wf_abc", "session_id": "s"})

    assert _serialize(out) == {"cancelled": True, "workflow_run_id": "wf_abc"}
    runner.cancel.assert_awaited_once_with("wf_abc")


async def test_cancel_workflow_returns_cancelled_false_when_no_run() -> None:
    """Already finished, cancelled or unknown: the caller's UI shows
    "already stopped"."""
    handler, runner = _make_handler()
    runner.cancel.return_value = WorkflowCancelOutcome.NOT_LIVE

    out = await handler.cancel_workflow({"workflow_run_id": "wf_unknown"})

    assert _serialize(out) == {"cancelled": False, "workflow_run_id": "wf_unknown"}


async def test_cancel_workflow_surfaces_runtime_error() -> None:
    handler, runner = _make_handler()
    runner.cancel.side_effect = RuntimeError("subprocess gone")

    with pytest.raises(RuntimeError, match="subprocess gone"):
        await handler.cancel_workflow({"workflow_run_id": "wf_x"})


@pytest.mark.parametrize("args", [{}, {"workflow_run_id": ""}])
async def test_cancel_workflow_rejects_a_missing_id(args: dict) -> None:
    handler, runner = _make_handler()

    with pytest.raises(ValueError) as caught:
        await handler.cancel_workflow(args)

    assert str(caught.value) == "cancel_workflow: 'workflow_run_id' is required"
    runner.cancel.assert_not_called()


def test_a_started_result_answers_with_the_runs_id_and_name() -> None:
    out = _started("wf_ok", "smoke").to_response()

    assert _serialize(out) == {"workflow_run_id": "wf_ok", "name": "smoke"}


def test_a_missing_workflow_result_raises_file_not_found() -> None:
    """``FileNotFoundError`` is what the extension host maps to REFUSED."""
    with pytest.raises(FileNotFoundError, match="nope"):
        WorkflowStartResult.not_found("nope").to_response()


def test_a_missing_runtime_result_raises_runtime_error() -> None:
    with pytest.raises(RuntimeError, match="no runtime"):
        WorkflowStartResult.runtime_missing("no runtime").to_response()


def test_rpc_method_enum_values_match_wire_string() -> None:
    """The wire strings the FE calls match the enum values."""
    assert RpcMethod.LIST_WORKFLOWS.value == "list_workflows"
    assert RpcMethod.RUN_WORKFLOW.value == "run_workflow"
    assert RpcMethod.CANCEL_WORKFLOW.value == "cancel_workflow"
