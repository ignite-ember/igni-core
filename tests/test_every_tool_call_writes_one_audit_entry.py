"""Every tool call writes one audit entry, however it was decided and however it ended.

docs/SECURITY.md §6: the audit log records every tool execution. Each path
a call can take is driven through the real code that decides it — the
main agent's tool hook and its permission pipeline, a sub-agent's audit
hook, and the places a paused call is refused — with a real
:class:`AuditLogger` writing to a file, and the line read back is held to
the fields the document promises. No argument value or error message is
ever in the line.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement
from agno.tools.function import Function, ToolResult

from igni_core.backend.hitl_controller import HitlController
from igni_core.backend.pause_handler import PauseHandler
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.backend.schemas_pause import PendingRequirement
from igni_core.core.agents.builder import AgentBuilder
from igni_core.core.agents.schemas import AgentBuildContext, AgentDefinition
from igni_core.core.config.permission_eval import PermissionEvaluator
from igni_core.core.config.settings import Settings
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.extension_gate import ExtensionHookVerdict, extension_hook_gate
from igni_core.core.hooks.schemas import HookResult, PermissionDecision
from igni_core.core.hooks.tool_audit_hook import ToolAuditHook
from igni_core.core.hooks.tool_hook import ToolEventHook
from igni_core.core.hooks.untrusted import UntrustedOutputHook
from igni_core.core.session.message_outcome import OutcomeKind, TurnOutcome
from igni_core.core.session.message_pause_policy import PausePolicyResolver
from igni_core.core.session.tool_approval import GuardRequirement, GuardToolExecution
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.utils.audit import AuditLogger
from igni_core.core.utils.tool_audit import ToolAudit
from igni_core.protocol import messages as msg
from tests.session_factory import build_session

#: A value no audit line may ever contain: it rides in arguments and errors.
SECRET = "hunter2-SECRET-TOKEN"


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.log"


@pytest.fixture
def audit(log_path: Path) -> ToolAudit:
    settings = Settings()
    settings.storage.audit_log = str(log_path)
    return ToolAudit(AuditLogger(settings), lambda: "sess-42")


def _lines(log_path: Path) -> list[dict[str, Any]]:
    """Every entry written, each checked to carry no secret."""
    if not log_path.exists():
        return []
    text = log_path.read_text(encoding="utf-8")
    assert SECRET not in text
    return [json.loads(line) for line in text.splitlines()]


def _only(log_path: Path) -> dict[str, Any]:
    lines = _lines(log_path)
    assert len(lines) == 1, lines
    return lines[0]


def _base(entry: dict[str, Any], *, agent: str, tool: str, status: str) -> dict[str, Any]:
    """Hold the entry's envelope; return its details."""
    assert list(entry) == ["timestamp", "session_id", "agent", "tool", "status", "details"]
    assert entry["timestamp"].endswith("+00:00")
    assert entry["session_id"] == "sess-42"
    assert entry["agent"] == agent
    assert entry["tool"] == tool
    assert entry["status"] == status
    return entry["details"]


class _HookAnswer(HookExecutor):
    """A configured ``PreToolUse`` hook answering with ``result``."""

    def __init__(self, result: HookResult) -> None:
        super().__init__({})
        self._result = result

    async def execute(self, event: str, payload: Any, target: str = "") -> HookResult:
        return self._result if event == HookEvent.PRE_TOOL_USE.value else HookResult()


def _hook(
    audit: ToolAudit,
    evaluator: PermissionEvaluator | None = None,
    executor: HookExecutor | None = None,
    **kwargs: Any,
) -> ToolEventHook:
    return ToolEventHook(
        executor or HookExecutor({}),
        session_id="sess-42",
        permission_evaluator=evaluator,
        audit=audit,
        **kwargs,
    )


def _main_agent(*, paused: bool) -> SimpleNamespace:
    """The main agent, its shell tool built to pause for approval or not."""
    return SimpleNamespace(
        name="igni",
        tools=[Function(name="run_shell_command", requires_confirmation=paused)],
    )


class _Ran:
    """A tool that records that it ran."""

    def __init__(self, result: Any = "done") -> None:
        self.calls = 0
        self.result = result

    def __call__(self, **_: Any) -> Any:
        self.calls += 1
        return self.result


# ── the main agent: allowed ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_call_an_allow_rule_lets_through_is_written_as_run_by_the_rule(
    audit: ToolAudit, log_path: Path
) -> None:
    evaluator = PermissionEvaluator.from_strings(allow=["Bash(ls *)"])
    tool = _Ran()
    result = await _hook(audit, evaluator)(
        name="run_shell_command", func=tool, args={"command": f"ls {SECRET}"}
    )
    assert tool.calls == 1 and "done" in str(result)
    details = _base(_only(log_path), agent="igni", tool="run_shell_command", status="success")
    assert details["decision"] == "allow"
    assert details["decided_by"] == "rule"
    assert details["mode"] == "default"
    assert details["reason"] == "allow rule 'Bash(ls *)' matched"
    assert details["arg_names"] == ["command"]
    assert isinstance(details["duration_ms"], float) and details["duration_ms"] >= 0
    assert "error_type" not in details


@pytest.mark.asyncio
async def test_a_call_with_no_evaluator_is_written_as_run_by_the_policy(
    audit: ToolAudit, log_path: Path
) -> None:
    await _hook(audit)(name="list_todos", func=_Ran(), args={})
    details = _base(_only(log_path), agent="igni", tool="list_todos", status="success")
    assert details["decided_by"] == "policy"
    assert "mode" not in details and "reason" not in details
    assert details["arg_names"] == []


@pytest.mark.asyncio
async def test_a_hooks_allow_is_written_as_decided_by_the_hook(
    audit: ToolAudit, log_path: Path
) -> None:
    evaluator = PermissionEvaluator.from_strings(deny=["Bash(rm *)"])
    executor = _HookAnswer(HookResult(permission_decision=PermissionDecision.ALLOW))
    await _hook(audit, evaluator, executor)(
        name="run_shell_command", func=_Ran(), args={"command": "rm x"}
    )
    details = _base(_only(log_path), agent="igni", tool="run_shell_command", status="success")
    assert details["decided_by"] == "hook"


# ── the main agent: refused ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_call_a_deny_rule_refuses_is_written_as_blocked_by_the_rule(
    audit: ToolAudit, log_path: Path
) -> None:
    evaluator = PermissionEvaluator.from_strings(deny=["Bash(rm *)"])
    tool = _Ran()
    result = await _hook(audit, evaluator)(
        name="run_shell_command", func=tool, args={"command": f"rm -rf {SECRET}"}
    )
    assert tool.calls == 0 and str(result).startswith("Blocked")
    details = _base(_only(log_path), agent="igni", tool="run_shell_command", status="blocked")
    assert details == {
        "decision": "deny",
        "decided_by": "rule",
        "mode": "default",
        "reason": "deny rule 'Bash(rm *)' matched",
        "arg_names": ["command"],
    }


@pytest.mark.asyncio
async def test_a_call_the_mode_refuses_is_written_as_blocked_by_the_mode(
    audit: ToolAudit, log_path: Path
) -> None:
    evaluator = PermissionEvaluator.from_strings(mode="plan")
    tool = _Ran()
    await _hook(audit, evaluator)(
        name="edit_file",
        func=tool,
        args={"file_path": "src/app.py", "old_string": SECRET, "new_string": "x"},
    )
    assert tool.calls == 0
    details = _base(_only(log_path), agent="igni", tool="edit_file", status="blocked")
    assert details["decision"] == "deny"
    assert details["decided_by"] == "mode"
    assert details["mode"] == "plan"
    assert details["arg_names"] == ["file_path", "new_string", "old_string"]
    assert "duration_ms" not in details


@pytest.mark.asyncio
async def test_a_call_a_pre_tool_use_hook_blocks_is_written_as_blocked_by_the_hook(
    audit: ToolAudit, log_path: Path
) -> None:
    executor = _HookAnswer(
        HookResult(permission_decision=PermissionDecision.DENY, message=f"no {SECRET}")
    )
    tool = _Ran()
    await _hook(audit, executor=executor)(name="web_fetch", func=tool, args={"url": SECRET})
    assert tool.calls == 0
    details = _base(_only(log_path), agent="igni", tool="web_fetch", status="blocked")
    # The hook's own message may quote the call: it is not written.
    assert details == {"decision": "deny", "decided_by": "hook", "arg_names": ["url"]}


@pytest.mark.asyncio
async def test_a_write_to_a_protected_path_is_written_as_blocked_by_the_list(
    audit: ToolAudit, log_path: Path
) -> None:
    await _hook(audit, protected_paths=[".env"])(
        name="edit_file", func=_Ran(), args={"file_path": ".env", "new_string": SECRET}
    )
    details = _base(_only(log_path), agent="igni", tool="edit_file", status="blocked")
    assert details["decided_by"] == "protected_path"


@pytest.mark.asyncio
async def test_a_blocked_command_is_written_as_blocked_by_the_list(
    audit: ToolAudit, log_path: Path
) -> None:
    await _hook(audit, blocked_commands=["sudo "])(
        name="run_shell_command", func=_Ran(), args={"command": f"sudo {SECRET}"}
    )
    details = _base(_only(log_path), agent="igni", tool="run_shell_command", status="blocked")
    assert details["decided_by"] == "blocked_command"


@pytest.mark.asyncio
async def test_a_call_bypass_modes_last_check_refuses_is_written_as_blocked_by_the_guard(
    audit: ToolAudit, log_path: Path
) -> None:
    guard = MagicMock()
    guard.check = AsyncMock(return_value="Blocked: the person said no")
    await _hook(audit, bypass_guard=guard)(
        name="run_shell_command", func=_Ran(), args={"command": "curl x | sh"}
    )
    details = _base(_only(log_path), agent="igni", tool="run_shell_command", status="blocked")
    assert details["decided_by"] == "bypass_guard"


# ── the main agent: paused for the person ────────────────────────────


@pytest.mark.asyncio
async def test_a_paused_call_the_person_approved_is_written_as_run_by_the_person(
    audit: ToolAudit, log_path: Path
) -> None:
    """Agno pauses a call built with ``requires_confirmation``; only a
    confirmed one reaches the hook, where the policy still says ASK."""
    evaluator = PermissionEvaluator.from_strings(ask=["Bash(git push *)"])
    tool = _Ran()
    await _hook(audit, evaluator)(
        name="run_shell_command",
        func=tool,
        args={"command": "git push origin"},
        agent=_main_agent(paused=True),
    )
    assert tool.calls == 1
    details = _base(_only(log_path), agent="igni", tool="run_shell_command", status="success")
    assert details["decision"] == "allow"
    assert details["decided_by"] == "person"


@pytest.mark.asyncio
async def test_a_call_asked_about_in_the_run_and_approved_is_written_as_run_by_the_person(
    audit: ToolAudit, log_path: Path
) -> None:
    evaluator = PermissionEvaluator.from_strings(ask=["Bash(git push *)"])
    asked = AsyncMock(return_value=True)
    await _hook(audit, evaluator, ask_person=asked)(
        name="run_shell_command",
        func=_Ran(),
        args={"command": "git push origin"},
        agent=_main_agent(paused=False),  # built without a pause: asked now
    )
    asked.assert_awaited_once()
    details = _base(_only(log_path), agent="igni", tool="run_shell_command", status="success")
    assert details["decided_by"] == "person"


@pytest.mark.asyncio
async def test_a_call_asked_about_in_the_run_and_refused_is_written_as_blocked_by_the_person(
    audit: ToolAudit, log_path: Path
) -> None:
    evaluator = PermissionEvaluator.from_strings(ask=["Bash(git push *)"])
    tool = _Ran()
    await _hook(audit, evaluator, ask_person=AsyncMock(return_value=False))(
        name="run_shell_command",
        func=tool,
        args={"command": f"git push origin {SECRET}"},
        agent=_main_agent(paused=False),
    )
    assert tool.calls == 0
    details = _base(_only(log_path), agent="igni", tool="run_shell_command", status="blocked")
    assert details["decision"] == "deny"
    assert details["decided_by"] == "person"


@pytest.mark.asyncio
async def test_a_call_nobody_could_be_asked_about_is_written_as_blocked_by_the_rule(
    audit: ToolAudit, log_path: Path
) -> None:
    evaluator = PermissionEvaluator.from_strings(ask=["Bash(git push *)"])
    await _hook(audit, evaluator)(
        name="run_shell_command",
        func=_Ran(),
        args={"command": "git push origin"},
        agent=_main_agent(paused=False),
    )
    details = _base(_only(log_path), agent="igni", tool="run_shell_command", status="blocked")
    assert details["decided_by"] == "rule"


def _requirement(tool_name: str, **args: Any) -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_name=tool_name, tool_args=args, requires_confirmation=True
        )
    )


@pytest.mark.asyncio
async def test_a_paused_call_the_person_refused_in_the_dialog_is_written_as_blocked(
    audit: ToolAudit, log_path: Path
) -> None:
    """The desktop's dialog: the refused call never runs, so no hook sees it."""
    session = MagicMock()
    session.session_id = "sess-42"
    session.tool_audit = audit
    session.permission_evaluator = PermissionEvaluator.from_strings()
    session.sub_agent_hitl = SubAgentHITLCoordinator()
    session.main_team.acontinue_run = MagicMock(return_value=_no_events())
    session.hook_executor.execute = AsyncMock(return_value=HookResult())
    store = PendingRequirementsStore()
    store.register(
        "r1",
        PendingRequirement(req=_requirement("run_shell_command", command=SECRET), run_id="run-1"),
    )
    controller = HitlController(session, store)
    async for _ in controller.resolve_batch(
        [msg.HITLDecision(requirement_id="r1", action="reject", choice="once")]
    ):
        pass
    details = _base(_only(log_path), agent="igni", tool="run_shell_command", status="blocked")
    assert details == {
        "decision": "deny",
        "decided_by": "person",
        "mode": "default",
        "arg_names": ["command"],
    }


async def _no_events():
    if False:
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_a_paused_call_the_person_confirmed_in_the_dialog_writes_nothing_yet(
    audit: ToolAudit, log_path: Path
) -> None:
    """A confirmed call is written when it runs, by the hook — not twice."""
    session = MagicMock()
    session.session_id = "sess-42"
    session.tool_audit = audit
    session.sub_agent_hitl = SubAgentHITLCoordinator()
    session.main_team.acontinue_run = MagicMock(return_value=_no_events())
    session.hook_executor.execute = AsyncMock(return_value=HookResult())
    store = PendingRequirementsStore()
    store.register(
        "r1", PendingRequirement(req=_requirement("run_shell_command", command="ls"), run_id="r")
    )
    async for _ in HitlController(session, store).resolve_batch(
        [msg.HITLDecision(requirement_id="r1", action="confirm", choice="once")]
    ):
        pass
    assert _lines(log_path) == []


def test_a_paused_call_the_policy_refuses_in_the_backend_is_written_as_blocked(
    audit: ToolAudit, log_path: Path
) -> None:
    handler = PauseHandler(
        evaluator=PermissionEvaluator.from_strings(mode="plan"),
        store=PendingRequirementsStore(),
        audit=audit,
    )
    event = SimpleNamespace(
        run_id="run-1",
        active_requirements=[_requirement("edit_file", file_path="a.py", new_string=SECRET)],
    )
    result = handler.handle(event)
    assert len(result.auto_resolved) == 1
    details = _base(_only(log_path), agent="igni", tool="edit_file", status="blocked")
    assert details["decision"] == "deny"
    assert details["decided_by"] == "mode"
    assert details["mode"] == "plan"
    assert details["arg_names"] == ["file_path", "new_string"]


def test_a_paused_call_the_policy_allows_in_the_backend_writes_nothing_yet(
    audit: ToolAudit, log_path: Path
) -> None:
    handler = PauseHandler(
        evaluator=PermissionEvaluator.from_strings(allow=["Bash(ls *)"]),
        store=PendingRequirementsStore(),
        audit=audit,
    )
    handler.handle(
        SimpleNamespace(
            run_id="r", active_requirements=[_requirement("run_shell_command", command="ls")]
        )
    )
    assert _lines(log_path) == []


@pytest.mark.asyncio
async def test_a_paused_call_the_policy_refuses_headless_is_written_as_blocked(
    audit: ToolAudit, log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``igni -m``: the pause is settled from the policy, the run continued."""
    requirement = _requirement("run_shell_command", command=f"rm {SECRET}")
    team = MagicMock()
    team.acontinue_run = AsyncMock(return_value=MagicMock())
    outcomes = iter(
        [
            SimpleNamespace(kind=OutcomeKind.PAUSED, requirements=[requirement]),
            SimpleNamespace(kind=OutcomeKind.COMPLETED, requirements=[]),
        ]
    )
    monkeypatch.setattr(TurnOutcome, "from_run", staticmethod(lambda _run: next(outcomes)))
    resolver = PausePolicyResolver(
        evaluator_ref=lambda: PermissionEvaluator.from_strings(deny=["Bash(rm *)"]),
        session_id_ref=lambda: "sess-42",
        audit=audit,
    )
    await resolver.settle(team, MagicMock())
    team.acontinue_run.assert_awaited_once()
    details = _base(_only(log_path), agent="igni", tool="run_shell_command", status="blocked")
    assert details["decided_by"] == "rule"
    assert details["mode"] == "default"
    assert details["reason"] == "deny rule 'Bash(rm *)' matched"


# ── the main agent: errored ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_call_that_raises_is_written_as_an_error_with_its_type_only(
    audit: ToolAudit, log_path: Path
) -> None:
    def boom(**_: Any) -> str:
        raise PermissionError(f"cannot open {SECRET}")

    with pytest.raises(PermissionError):
        await _hook(audit)(name="read_notes", func=boom, args={"path": SECRET})
    details = _base(_only(log_path), agent="igni", tool="read_notes", status="error")
    assert details["decision"] == "allow"
    assert details["error_type"] == "PermissionError"
    assert isinstance(details["duration_ms"], float)


@pytest.mark.asyncio
async def test_a_call_cancelled_while_it_runs_is_written_as_an_error(
    audit: ToolAudit, log_path: Path
) -> None:
    async def hangs(**_: Any) -> str:
        await asyncio.sleep(3600)
        return "never"

    task = asyncio.ensure_future(_hook(audit)(name="slow_tool", func=hangs, args={}))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    details = _base(_only(log_path), agent="igni", tool="slow_tool", status="error")
    assert details["error_type"] == "CancelledError"


# ── MCP and extension tools ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_mcp_tool_call_is_written_like_any_other(audit: ToolAudit, log_path: Path) -> None:
    tool = _Ran(ToolResult(content="rows: 2"))
    await _hook(audit)(name="query_db", func=tool, args={"sql": SECRET})
    details = _base(_only(log_path), agent="igni", tool="query_db", status="success")
    assert details["arg_names"] == ["sql"]


@pytest.mark.asyncio
async def test_an_mcp_call_its_server_never_answered_is_written_as_an_error(
    audit: ToolAudit, log_path: Path
) -> None:
    tool = _Ran(ToolResult(content=f"Error: timed out talking to {SECRET}"))
    await _hook(audit)(name="query_db", func=tool, args={})
    details = _base(_only(log_path), agent="igni", tool="query_db", status="error")
    assert details["error_type"] == "McpUnanswered"


class _Refuses:
    """An extension whose ``PreToolUse`` hook refuses every call."""

    def hooks(self, event: HookEvent) -> bool:
        return event is HookEvent.PRE_TOOL_USE

    async def ask(self, value: Any) -> ExtensionHookVerdict[Any]:
        return ExtensionHookVerdict(allowed=False, value=value, reason=SECRET, by="ext-1")

    def tell(self, event: HookEvent, payload: Any) -> None: ...

    def tool_called(self, tool_name: str, *, answered: bool, detail: str) -> None: ...


@pytest.mark.asyncio
async def test_a_call_an_extensions_hook_refuses_is_written_as_blocked_by_the_extension(
    audit: ToolAudit, log_path: Path
) -> None:
    extension_hook_gate.install(_Refuses())
    try:
        tool = _Ran()
        await _hook(audit)(name="scan_volume", func=tool, args={"volume": "v1"})
    finally:
        extension_hook_gate.install(None)
    assert tool.calls == 0
    details = _base(_only(log_path), agent="igni", tool="scan_volume", status="blocked")
    assert details == {"decision": "deny", "decided_by": "extension", "arg_names": ["volume"]}


# ── sub-agents ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_sub_agents_call_is_written_under_its_own_name(
    audit: ToolAudit, log_path: Path
) -> None:
    agent = SimpleNamespace(name="explorer", tools=[])
    await ToolAuditHook(audit)(
        name="run_shell_command", func=_Ran(), args={"command": SECRET}, agent=agent
    )
    details = _base(_only(log_path), agent="explorer", tool="run_shell_command", status="success")
    assert details["decision"] == "allow"
    assert details["decided_by"] == "policy"
    assert details["arg_names"] == ["command"]
    assert isinstance(details["duration_ms"], float)


@pytest.mark.asyncio
async def test_a_sub_agents_paused_and_approved_call_is_written_as_run_by_the_person(
    audit: ToolAudit, log_path: Path
) -> None:
    agent = SimpleNamespace(
        name="editor", tools=[Function(name="edit_file", requires_confirmation=True)]
    )
    await ToolAuditHook(audit)(name="edit_file", func=_Ran(), args={}, agent=agent)
    details = _base(_only(log_path), agent="editor", tool="edit_file", status="success")
    assert details["decided_by"] == "person"


@pytest.mark.asyncio
async def test_a_sub_agents_failing_call_is_written_as_an_error(
    audit: ToolAudit, log_path: Path
) -> None:
    async def boom(**_: Any) -> str:
        raise TimeoutError(SECRET)

    with pytest.raises(TimeoutError):
        await ToolAuditHook(audit)(
            name="web_fetch", func=boom, args={}, agent=SimpleNamespace(name="researcher")
        )
    details = _base(_only(log_path), agent="researcher", tool="web_fetch", status="error")
    assert details["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_a_sub_agents_paused_call_the_person_refused_is_written_as_blocked(
    audit: ToolAudit, log_path: Path
) -> None:
    coordinator = SubAgentHITLCoordinator(audit=audit)
    req_id = await coordinator.push_requirement(
        _requirement("run_shell_command", command=SECRET),
        run_id="sub-run",
        agent_path=["igni", "editor"],
    )
    assert coordinator.resolve(req_id, msg.HITLAction.REJECT)
    details = _base(_only(log_path), agent="editor", tool="run_shell_command", status="blocked")
    assert details == {"decision": "deny", "decided_by": "person", "arg_names": ["command"]}


@pytest.mark.asyncio
async def test_a_single_call_question_refused_is_left_to_the_pipeline(
    audit: ToolAudit, log_path: Path
) -> None:
    """The main pipeline's own questions are written by the pipeline: not twice."""
    coordinator = SubAgentHITLCoordinator(audit=audit)
    req_id = await coordinator.push_requirement(
        GuardRequirement(
            tool_execution=GuardToolExecution(tool_name="run_shell_command"), details="?"
        ),
        run_id="bypass-guard-run_shell_command",
        agent_path=["safety check"],
    )
    coordinator.resolve(req_id, msg.HITLAction.REJECT)
    assert _lines(log_path) == []


def test_every_agent_the_pool_builds_carries_the_audit_hook(
    settings: Any, audit: ToolAudit, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Recorder:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(AgentBuilder, "_agent_cls", Recorder)
    context = AgentBuildContext(settings=settings, supervisor=ProcessSupervisor(), tool_audit=audit)
    built = AgentBuilder(context).build(AgentDefinition(name="worker", description="Works"))
    hooks = built.kwargs["tool_hooks"]
    assert [type(h) for h in hooks] == [UntrustedOutputHook, ToolAuditHook]
    assert hooks[1].audit is audit
    # A spawn re-wraps the pool's hooks: the audit hook stays, once.
    assert UntrustedOutputHook.on(hooks) == hooks
    assert ToolAuditHook.on(hooks, audit) == hooks


# ── a failing audit never breaks a call ──────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_write_is_counted_and_the_call_still_runs(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    path.mkdir()  # opening a directory for append fails
    settings = Settings()
    settings.storage.audit_log = str(path)
    audit = ToolAudit(AuditLogger(settings), lambda: "s")
    tool = _Ran()
    result = await _hook(audit)(name="t", func=tool, args={})
    assert tool.calls == 1 and result == "done"
    assert audit.failed == 1


@pytest.mark.asyncio
async def test_an_entry_that_cannot_be_built_is_counted_not_raised(log_path: Path) -> None:
    settings = Settings()
    settings.storage.audit_log = str(log_path)

    def no_session() -> str:
        raise RuntimeError("no session yet")

    audit = ToolAudit(AuditLogger(settings), no_session)
    assert await _hook(audit)(name="t", func=_Ran(), args={}) == "done"
    assert audit.failed == 1


# ── a session wires one audit into every place a call is written ─────


@pytest.mark.asyncio
async def test_a_session_writes_its_main_agents_calls_to_its_audit_log(tmp_path: Path) -> None:
    session = build_session(tmp_path)
    log = Path(session.settings.storage.audit_log).expanduser()
    audit = session.tool_audit
    assert audit is not None
    # Every place a call is written writes to the one audit the session has.
    hook = session.tool_hook_factory.create()
    assert hook.audit is audit
    assert session.sub_agent_hitl.audit is audit
    assert session.pool._context.tool_audit is audit
    assert session.message_handler._pause_policy._audit is audit
    # And a call through the main agent's hook lands in the session's log
    # under the session's id.
    await hook(name="list_todos", func=_Ran(), args={})
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["session_id"] == session.session_id
    assert entry["tool"] == "list_todos"
    assert entry["details"]["decision"] == "allow"
