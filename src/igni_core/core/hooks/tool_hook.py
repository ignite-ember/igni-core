"""Tool event hook — fires PreToolUse/PostToolUse/PostToolUseFailure events.

Agno fires ``tool_hooks`` around every tool call. :class:`ToolEventHook`
is the async facade that Agno's ``aexecute`` chain sees. It
composes four small collaborators, each with one job:

* :class:`~igni_core.core.hooks.permission.pipeline.PermissionPipeline`
  — hooks + safety lists + evaluator, ordered gates that either
  green-light the call or return a block message.
* :class:`~igni_core.core.hooks.tool_call_invoker.ToolCallInvoker`
  — runs the callable (sync or async), fires PostToolUse /
  PostToolUseFailure, re-raises on error.
* :class:`~igni_core.core.hooks.rules_suffixer.RulesSuffixer`
  — appends any newly-discovered subdirectory rules to a string
  result and fires ``InstructionsLoaded``.
* :class:`~igni_core.core.hooks.hook_firer.HookFirer` — the
  uniform wire path every collaborator uses to actually reach
  the :class:`HookExecutor`.
* :class:`~igni_core.core.utils.tool_audit.ToolAudit` — one
  audit-log entry per call: refused (and by what), or run (how
  long, and whether it failed). Every main-agent call passes here,
  so this is where it is written, once.

Module-level ``_safe_args`` / ``_preview`` are compat shims —
``tests/test_tool_hook.py`` imports them by name. Each is a
two-line delegator to :class:`PayloadSanitizer`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agno.tools.function import ToolResult

from igni_core.core.config.permission_eval import PermissionEvaluator
from igni_core.core.hooks.agno_coroutine_marker import AgnoCoroutineMarker
from igni_core.core.hooks.bypass_guard import BypassGuard
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.extension_gate import extension_hook_gate
from igni_core.core.hooks.extension_values import PostToolUseValue, PreToolUseValue
from igni_core.core.hooks.hook_firer import HookFirer
from igni_core.core.hooks.payload_sanitizer import PayloadSanitizer
from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.permission.evaluator_stage import AskPerson
from igni_core.core.hooks.permission.pipeline import PermissionPipeline
from igni_core.core.hooks.rules_suffixer import RulesSuffixer
from igni_core.core.hooks.tool_audit_hook import UNANSWERED, unanswered
from igni_core.core.hooks.tool_call_invoker import ToolCallInvoker
from igni_core.core.observability.runtime import OtelRuntime
from igni_core.core.paths import MAIN_AGENT_NAME
from igni_core.core.utils.rules_index import RulesIndex
from igni_core.core.utils.tool_audit import DecidedBy, ToolAudit

logger = logging.getLogger(__name__)

__all__ = ["ToolEventHook", "_safe_args", "_preview"]


class ToolEventHook:
    """Async Agno ``tool_hook`` — thin facade over the pipeline.

    ``__call__`` runs: :class:`PermissionPipeline` → (if allowed)
    :class:`ToolCallInvoker` → :class:`RulesSuffixer`. The three
    collaborators share a :class:`HookFirer`, so hook events flow
    through a single wire path.

    The constructor signature is preserved verbatim so
    ``ToolEventHookFactory`` (session/tool_hook_factory.py) does
    not have to move.
    """

    def __init__(
        self,
        executor: HookExecutor,
        session_id: str = "",
        protected_paths: list[str] | None = None,
        blocked_commands: list[str] | None = None,
        rules_index: RulesIndex | None = None,
        project_dir: Path | None = None,
        permission_evaluator: PermissionEvaluator | None = None,
        bypass_guard: BypassGuard | None = None,
        ask_person: AskPerson | None = None,
        audit: ToolAudit | None = None,
    ):
        # Agno's ``aexecute`` checks ``inspect.iscoroutinefunction``
        # — mark this callable-instance so the async path is used.
        AgnoCoroutineMarker.mark(self)

        self._session_id = session_id
        self._evaluator = permission_evaluator
        self.audit = audit
        firer = HookFirer(executor, session_id)
        self._pipeline = PermissionPipeline.build(
            firer=firer,
            protected_paths=protected_paths or [],
            blocked_commands=blocked_commands or [],
            permission_evaluator=permission_evaluator,
            bypass_guard=bypass_guard,
            ask_person=ask_person,
        )
        self._invoker = ToolCallInvoker(firer)
        self._suffixer = RulesSuffixer(
            rules_index=rules_index,
            project_dir=project_dir,
            firer=firer,
        )

    async def __call__(
        self,
        name: str = "",
        func: Callable | None = None,
        args: dict[str, Any] | None = None,
        agent: Any = None,
        team: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Agno-owned signature — ``**kwargs`` absorbs any extra
        keys Agno's dispatch may add. Internally we immediately
        pack the call into a typed :class:`ToolCallContext` so
        every collaborator downstream is on the typed side of
        the seam.
        """
        ctx = ToolCallContext(
            name=name,
            func=func,
            args=args or {},
            agent=agent,
            team=team,
        )
        # Extensions' PreToolUse hooks are asked first, with the real
        # arguments, so igni's own checks — the configured PreToolUse
        # hooks among them — run on whatever they leave.
        pre = await extension_hook_gate.ask(
            PreToolUseValue(session_id=self._session_id, tool_name=name, tool_args=ctx.args)
        )
        if not pre.allowed:
            OtelRuntime.note_permission("deny")
            self._refused(ctx, DecidedBy.EXTENSION)
            return f"Blocked: {pre.reason}"
        ctx = ctx.with_args(pre.value.tool_args)
        if args is not None and pre.value.tool_args != args:
            # agno 3.1 calls the entrypoint with the FunctionCall's own
            # arguments, whatever a hook hands ``func``; this hook is the
            # outermost, so the dict it was given is those arguments, and
            # the change reaches the tool only written into it (campaign
            # R114, R287, R361: igni's checks saw the change, the tool ran
            # with the model's arguments).
            args.clear()
            args.update(pre.value.tool_args)
        verdict = await self._pipeline.decide(ctx)
        if verdict.message is not None:
            OtelRuntime.note_permission("deny")
            self._refused(ctx, verdict.by, verdict.reason)
            return verdict.message
        OtelRuntime.note_permission("allow")
        started = time.perf_counter()
        try:
            result = await self._invoker.run(ctx)
        except BaseException as exc:
            self._ran(ctx, verdict.by, verdict.reason, started, type(exc).__name__)
            if isinstance(exc, Exception):
                # An MCP tool that reported an error of its own answered
                # (``core/mcp/tool_errors``); only one that did not is a fault.
                answered = bool(getattr(exc, "answered", False))
                extension_hook_gate.tool_called(name, answered=answered, detail=str(exc))
            raise
        not_answered = self._unanswered(result)
        self._ran(ctx, verdict.by, verdict.reason, started, UNANSWERED if not_answered else None)
        extension_hook_gate.tool_called(
            name, answered=not_answered is None, detail=not_answered or ""
        )
        result = await self._after(ctx, result)
        return await self._suffixer.enrich(ctx, result)

    def _mode(self) -> str | None:
        """The permission mode the call is decided in, when there is an evaluator."""
        if self._evaluator is None:
            return None
        try:
            return str(self._evaluator.mode.value)
        except Exception:  # noqa: BLE001 — the audit must never break a call
            return None

    def _refused(self, ctx: ToolCallContext, by: DecidedBy, reason: str = "") -> None:
        if self.audit is None:
            return
        self.audit.refused(
            agent=ctx.caller_name(MAIN_AGENT_NAME),
            tool=ctx.name,
            args=ctx.args,
            decided_by=by,
            mode=self._mode(),
            reason=reason,
        )

    def _ran(
        self,
        ctx: ToolCallContext,
        by: DecidedBy,
        reason: str,
        started: float,
        error_type: str | None,
    ) -> None:
        if self.audit is None:
            return
        self.audit.ran(
            agent=ctx.caller_name(MAIN_AGENT_NAME),
            tool=ctx.name,
            args=ctx.args,
            decided_by=by,
            mode=self._mode(),
            reason=reason,
            duration_ms=(time.perf_counter() - started) * 1000,
            error_type=error_type,
        )

    async def _after(self, ctx: ToolCallContext, result: Any) -> Any:
        """Extensions' PostToolUse hooks: what the model reads of a result.

        A reject withholds the result — the model is told it was
        withheld, and why. A change replaces it; only a text result (or
        an MCP tool's text content) can be changed, so for any other
        kind a change is refused and the result goes through as it is.
        """
        if not extension_hook_gate.hooked(HookEvent.POST_TOOL_USE):
            return result
        text = self._text_of(result)
        post = await extension_hook_gate.ask(
            PostToolUseValue(
                session_id=self._session_id,
                tool_name=ctx.name,
                tool_args=ctx.args,
                result=text if text is not None else str(result),
            )
        )
        if not post.allowed:
            return f"Withheld: {post.reason}"
        changed = post.value.result
        if text is None:
            if changed != str(result):
                logger.warning(
                    "an extension changed %s's result, which is not text; it is not applied",
                    ctx.name,
                )
            return result
        if changed == text:
            return result
        if isinstance(result, ToolResult):
            return result.model_copy(update={"content": changed})
        return changed

    @staticmethod
    def _text_of(result: Any) -> str | None:
        """A result as the text the model reads, when it is text."""
        if isinstance(result, str):
            return result
        if isinstance(result, ToolResult) and isinstance(result.content, str):
            return result.content
        return None

    @staticmethod
    def _unanswered(result: Any) -> str | None:
        """Why an MCP server did not answer a call, or ``None`` when it did
        (:func:`~igni_core.core.hooks.tool_audit_hook.unanswered`)."""
        return unanswered(result)


# ── Backwards-compat module-level shims ─────────────────────────────
#
# ``tests/test_tool_hook.py`` imports these names directly. Each
# is a two-line delegator to :class:`PayloadSanitizer` — the
# audit's "utility-module-of-related-helpers" offender for
# protected-paths / blocked-commands is fully re-homed onto
# :class:`ProtectedPathStage` / :class:`BlockedCommandStage`;
# their tests call the OOP surface directly.


def _safe_args(args: dict[str, Any]) -> dict[str, str]:
    """Compat shim — delegates to :meth:`PayloadSanitizer.safe_args`."""
    return PayloadSanitizer.safe_args(args)


def _preview(result: Any) -> str:
    """Compat shim — delegates to :meth:`PayloadSanitizer.preview`."""
    return PayloadSanitizer.preview(result)
