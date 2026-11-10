"""Message-handling pipeline for :class:`Session`.

The headless message path, in order:

1. Ensure MCP servers are connected (deferred to the caller).
2. Fire ``UserPromptSubmit`` → maybe block the turn.
3. Run guardrails → maybe prepend a warning prefix.
4. Assemble the effective message (reminders + datetime).
5. Dispatch to the team; settle the pauses the policy decided.
6. Read the run's outcome → a reply, or a typed failure.
7. Fire ``Stop``, retrying up to 3× on block.
8. Auto-compact if the run pushed us over the context window.

The pipeline's collaborators arrive as a :class:`MessageHandlerDeps`
model — nothing the session can change under the pipeline is copied:
``team_ref`` and ``session_id_ref`` are callables (the team is
rebuilt, ``/clear`` and ``/fork`` rotate the id), the hook executor is
read from the stable :class:`SessionHooks` on every call (``/hooks
reload`` and plugin reloads replace it), and the context window is
read from the stable :class:`ContextBudget` (``/model`` resizes it).

The expected failures of a turn — a pause nobody can answer, a run
the provider failed — are values
(:class:`~igni_core.core.session.message_outcome.TurnFailure`), not
exceptions raised on the way out and caught again on the way in.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from igni_core.core.config.permission_eval import PermissionEvaluator
from igni_core.core.guardrails.runner import GuardrailRunner
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.extension_gate import extension_hook_gate
from igni_core.core.hooks.extension_values import StopValue, UserPromptSubmitValue
from igni_core.core.paths import MAIN_AGENT_NAME
from igni_core.core.session.compaction.context_budget import ContextBudget
from igni_core.core.session.message_outcome import (
    OutcomeKind,
    TurnFailure,
    TurnOutcome,
    TurnResult,
)
from igni_core.core.session.message_pause_policy import PausePolicyResolver
from igni_core.core.session.message_team import AnyRunOutput, MessageTeam
from igni_core.core.session.schemas import (
    McpInitResult,
    MessageMedia,
    StopFailureHookPayload,
    StopHookPayload,
    UserPromptSubmitHookPayload,
)
from igni_core.core.utils import response as response_text
from igni_core.core.utils.audit import AuditEntry, AuditLogger
from igni_core.core.utils.display import DisplayManager

logger = logging.getLogger(__name__)


class HookExecutorSource(Protocol):
    """Where the current :class:`HookExecutor` is read from (:class:`SessionHooks`)."""

    @property
    def executor(self) -> HookExecutor: ...


@dataclass(frozen=True)
class MessageHandlerDeps:
    """What the message pipeline reads.

    Every collaborator is either a stable owner or a callable the
    pipeline calls live — never a copy of state the session replaces
    underneath it.
    """

    #: Where the current hook executor is read from.
    hooks: HookExecutorSource
    #: Where the turn is recorded.
    audit: AuditLogger
    #: Where the turn is reported to the person.
    display: DisplayManager
    #: What runs the guardrail checks.
    guardrail_runner: GuardrailRunner
    #: The team the turn runs on — rebuilt by ``/model`` and the
    #: plugin reloads, so it is read on every turn.
    team_ref: Callable[[], MessageTeam]
    #: The queued reminders, drained one-shot per turn.
    pending_reminders_drain: Callable[[], list[str]]
    #: Called with the turn's input tokens and the current window.
    compact_hook: Callable[[int, int], Awaitable[bool]]
    #: Connects the MCP servers before the first turn.
    ensure_mcp: Callable[[], Awaitable[McpInitResult]]
    #: The session's id — rotated by ``/clear`` and ``/fork``.
    session_id_ref: Callable[[], str]
    #: The window the compaction threshold is measured against.
    budget: ContextBudget
    #: How the reply is read off the run output; the default reads the
    #: run's own content.
    extract_response_text: Callable[[AnyRunOutput], str] | None = None
    #: The permission evaluator — rebuilt with the team, so it is read
    #: on every turn.
    permission_evaluator_ref: Callable[[], PermissionEvaluator | None] | None = None


class SessionMessageHandler:
    """Owns the message pipeline for one Session instance."""

    _STOP_HOOK_MAX_RETRIES = 3
    _STOP_HOOK_RESPONSE_PREVIEW = 500

    def __init__(self, deps: MessageHandlerDeps) -> None:
        self._deps = deps
        self._pause_policy = PausePolicyResolver(
            evaluator_ref=deps.permission_evaluator_ref or _no_evaluator,
            session_id_ref=deps.session_id_ref,
        )
        #: Why the last :meth:`handle` failed, or ``""`` when it did not —
        #: what a headless run's exit code is made from.
        self.last_failure = ""

    @property
    def _hook_executor(self) -> HookExecutor:
        return self._deps.hooks.executor

    @property
    def _team(self) -> MessageTeam:
        return self._deps.team_ref()

    def _extract_response_text(self, response: AnyRunOutput) -> str:
        extract = self._deps.extract_response_text or response_text.extract_response_text
        return extract(response)

    async def handle(self, message: str, media: MessageMedia | None = None) -> str:
        """Handle a single user message and return the response."""
        self.last_failure = ""
        await self._deps.ensure_mcp()

        verdict = await extension_hook_gate.ask(
            UserPromptSubmitValue(session_id=self._deps.session_id_ref(), message=message)
        )
        if not verdict.allowed:
            return self._blocked(verdict.reason)
        message = verdict.value.message

        blocked = await self._check_user_prompt_hook(message)
        if blocked is not None:
            return blocked

        guardrail_prefix = await self._guardrail_prefix(message)
        effective_message = self._build_effective_message(message, guardrail_prefix)

        try:
            result = await self._run_turn(effective_message, media)
        except Exception as exc:  # noqa: BLE001 — the turn failed; it is reported, not swallowed
            result = TurnResult.failed(TurnFailure.from_exception(exc))
        if result.failure is not None:
            return await self._report_failure(result.failure)
        return result.reply

    async def _run_turn(self, message: str, media: MessageMedia | None) -> TurnResult:
        """Run the turn and say what came of it."""
        team = self._team
        media_kwargs = media.to_kwargs() if media is not None else {}
        response = await team.arun(message, stream=False, **media_kwargs)
        response = await self._pause_policy.settle(team, response)

        outcome = TurnOutcome.from_run(response)
        if outcome.kind is OutcomeKind.PAUSED:
            return TurnResult.failed(TurnFailure.paused(outcome))
        if outcome.kind is OutcomeKind.FAILED:
            return TurnResult.failed(TurnFailure.failed(outcome))

        reply = self._extract_response_text(response)
        self._deps.audit.log(
            AuditEntry.success(
                session_id=self._deps.session_id_ref(),
                agent_name=MAIN_AGENT_NAME,
                tool_name="main_team",
            )
        )

        reply, final_run = await self._retry_on_stop_hook_block(reply, response)
        await self._compact_if_over_the_window(final_run)
        return TurnResult.completed(reply)

    async def _compact_if_over_the_window(self, final_run: AnyRunOutput) -> None:
        """Latch the turn's input tokens and compact when they are near the limit.

        The count is read off the value ``arun`` returned, not off the
        team: agno's ``Team``/``Agent`` have no ``run_response``
        attribute (the name exists only as a keyword parameter), so a
        handler that asked the team measured nothing and neither the
        token latch nor auto-compaction ever ran on this path.
        ``final_run`` is the last RunOutput of the turn — a Stop-hook
        retry re-runs the agent, and it is that run's input count the
        next threshold check needs.
        """
        metrics = final_run.metrics
        if not metrics:
            return
        input_tokens = metrics.input_tokens or 0
        self._deps.budget.latch(input_tokens)
        await self._deps.compact_hook(input_tokens, self._deps.budget.window)

    async def _check_user_prompt_hook(self, message: str) -> str | None:
        """Fire the ``UserPromptSubmit`` hook. Returns the blocked
        message when the hook denies, ``None`` when the turn should
        proceed. Blocked turns emit an audit entry so a policy denial
        is traceable — otherwise the user sees "blocked" with no
        record of why."""
        payload = UserPromptSubmitHookPayload(
            message=message,
            session_id=self._deps.session_id_ref(),
        )
        hook_result = await self._hook_executor.execute(
            event=HookEvent.USER_PROMPT_SUBMIT.value,
            payload=payload,
        )
        if hook_result.should_continue:
            return None
        return self._blocked(hook_result.message or "Blocked by UserPromptSubmit hook.")

    def _blocked(self, blocked_msg: str) -> str:
        """A message a UserPromptSubmit hook stopped: audited, and returned as the reply."""
        self._deps.audit.log(
            AuditEntry.blocked(
                session_id=self._deps.session_id_ref(),
                agent_name="session",
                tool_name="user_prompt",
                reason=blocked_msg,
            )
        )
        return blocked_msg

    async def _guardrail_prefix(self, message: str) -> str:
        """Run guardrail checks and produce a warning prefix (empty
        when disabled or all clean). Guardrails inform, don't block —
        the prefix is prepended to the effective message so the model
        sees the caveat before the user text.
        """
        return await self._deps.guardrail_runner.warning_prefix(message)

    def _build_effective_message(self, message: str, guardrail_prefix: str) -> str:
        """Assemble the message the model actually sees: any queued
        ``asyncRewake`` reminders (drained one-shot), a
        ``<system-context>`` datetime hint, and the guardrail prefix
        if any."""
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        reminders = self._deps.pending_reminders_drain()
        reminders_block = ""
        if reminders:
            joined = "\n".join(reminders)
            reminders_block = f"<system-reminder>{joined}</system-reminder>\n"
        effective = (
            f"{reminders_block}"
            f"<system-context>Current datetime: {timestamp}</system-context>\n{message}"
        )
        if guardrail_prefix:
            effective = guardrail_prefix + effective
        return effective

    async def _retry_on_stop_hook_block(
        self, response_text: str, run_output: AnyRunOutput
    ) -> tuple[str, AnyRunOutput]:
        """Fire the ``Stop`` hook up to 3 times; feed rejection
        messages back to the agent to re-generate the response.

        A Stop hook that returns ``should_continue=False`` treats
        the agent's response as unacceptable — its ``message``
        becomes a critique the agent should address. We re-run
        the agent with that critique as a system message, then
        fire the hook again on the new response. Bounded at 3
        attempts so a persistently-rejecting hook doesn't loop
        forever. On the third failure we accept the response
        (the hook can still deny at a later stage if it's a
        hard-block invariant).

        Returns the accepted text *and* the RunOutput that produced it.
        A retry re-runs the agent with the whole history plus a critique,
        so its input-token count is the larger one and the only one worth
        thresholding compaction on; returning text alone left the caller
        measuring a run that had been superseded.
        """
        for _stop_attempt in range(self._STOP_HOOK_MAX_RETRIES):
            payload = StopHookPayload(
                session_id=self._deps.session_id_ref(),
                response=response_text[: self._STOP_HOOK_RESPONSE_PREVIEW],
            )
            feedback = await self._stop_feedback(payload, response_text)
            if feedback is None:
                break
            system_msg = (
                f"[SYSTEM] Your previous response was rejected by a Stop hook: "
                f"{feedback}\nPlease revise your response to address this issue."
            )
            run_output = await self._team.arun(system_msg, stream=False)
            response_text = self._extract_response_text(run_output)
        return response_text, run_output

    async def _stop_feedback(self, payload: StopHookPayload, response_text: str) -> str | None:
        """Why the agent may not stop yet, or ``None`` when it may.

        The extensions that hook ``Stop`` are asked first, then the
        configured hooks; either one's block sends the agent back.
        """
        verdict = await extension_hook_gate.ask(
            StopValue(session_id=payload.session_id, response=response_text)
        )
        if not verdict.allowed:
            return verdict.reason
        stop_result = await self._hook_executor.execute(
            event=HookEvent.STOP.value,
            payload=payload,
        )
        if stop_result.should_continue:
            return None
        return stop_result.message or "Response blocked by Stop hook."

    async def _report_failure(self, failure: TurnFailure) -> str:
        """The turn produced no reply: say so, record it, tell the plugins.

        The StopFailure hook mirrors the Stop hook on the happy path —
        same payload shape family, same non-blocking semantics — so
        plugins can observe both success and failure with a single
        subscription pair.
        """
        error_msg = f"Error handling message: {failure.message}"
        self.last_failure = error_msg
        self._deps.display.print_error(error_msg)

        self._deps.audit.log(
            AuditEntry.error(
                session_id=self._deps.session_id_ref(),
                agent_name="session",
                tool_name="main_team",
                error=failure.message,
                error_type=failure.error_type,
            )
        )

        await self._tell_stop_failure(failure)
        return error_msg

    async def _tell_stop_failure(self, failure: TurnFailure) -> None:
        """Fire the StopFailure hook — observation only.

        A hook that fails here must not mask the failure it is
        reporting, so its own error is logged and the turn's failure
        stands.
        """
        payload = StopFailureHookPayload(
            session_id=self._deps.session_id_ref(),
            error=failure.message,
            error_type=failure.error_type,
        )
        try:
            await self._hook_executor.execute(
                event=HookEvent.STOP_FAILURE.value,
                payload=payload,
            )
        except Exception:  # noqa: BLE001 — the failure being reported is the interesting one
            logger.warning("the StopFailure hook failed", exc_info=True)


def _no_evaluator() -> PermissionEvaluator | None:
    """The evaluator a pipeline without one reads — nobody decides."""
    return None


__all__ = ["HookExecutorSource", "MessageHandlerDeps", "SessionMessageHandler"]
