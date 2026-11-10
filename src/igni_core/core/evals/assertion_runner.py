"""Per-check assertion drivers composed by :class:`CaseAssertionRunner`.

Replaces the god-branching ``_apply_case_assertions`` free function.
Each driver:
    * owns its gate (``.should_run(ctx)``) so the runner doesn't
      re-implement ``if case.expected_tool_calls: …`` for every check;
    * writes its own subset of :class:`CaseResult` fields via
      ``.record(ctx, result)``, so no single mutator touches every
      field on the result.

Drivers that produce one verdict derive from :class:`CheckDriver`,
which splits ``record`` into ``run`` (returns a :class:`CheckResult`)
and ``apply_to`` (writes it). :class:`FileDriver` records one
:class:`~igni_core.core.evals.assertions.FileCheckResult` per assertion instead.

The overall aggregate (``result.passed``) is derived by
:meth:`CaseResult.compute_passed` after every driver has run — killing
the scattered ``all_passed`` bool that used to thread through the
old free-function pipeline.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agno.agent import Agent
from agno.eval.accuracy import AccuracyEval
from agno.eval.reliability import ReliabilityEval
from agno.models.base import Model

from igni_core.core.evals.agno_adapter import AgnoResponseAdapter
from igni_core.core.evals.schemas import (
    CaseResult,
    CheckResult,
    EvalCase,
    ToolArgAssertion,
    ToolTraceEntry,
)
from igni_core.core.evals.tool_names import ToolNameCatalog

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssertionContext:
    """Snapshot passed to every driver.

    Bundling into one object keeps driver signatures uniform and stops
    each new field from cascading through every driver's positional
    args (which is why the old free-function chain had 7+ params).
    A frozen dataclass rather than a Pydantic model: it carries live
    runtime objects (the agent, the judge model, the response adapter),
    not data, and Pydantic cannot build a schema for Agno's ``Agent``.
    """

    case: EvalCase
    adapter: AgnoResponseAdapter
    output_text: str
    tool_trace: list[ToolTraceEntry]
    agent: Agent
    judge_model: Model | None
    work_dir: Path | None
    catalog: ToolNameCatalog
    allow_additional_tool_calls: bool = False


class AssertionDriver(ABC):
    """Base class for one kind of eval check.

    Subclasses implement:
        * :meth:`should_run` — cheap gate on the case and its context;
        * :meth:`record` — async, runs the check and writes its fields
          on the result.
    """

    @abstractmethod
    def should_run(self, ctx: AssertionContext) -> bool: ...

    @abstractmethod
    async def record(self, ctx: AssertionContext, result: CaseResult) -> None: ...


class CheckDriver(AssertionDriver):
    """A driver whose check yields exactly one :class:`CheckResult`."""

    @abstractmethod
    async def run(self, ctx: AssertionContext) -> CheckResult: ...

    @abstractmethod
    def apply_to(self, result: CaseResult, check: CheckResult) -> None: ...

    async def record(self, ctx: AssertionContext, result: CaseResult) -> None:
        self.apply_to(result, await self.run(ctx))


class ReliabilityDriver(CheckDriver):
    """Runs Agno's ReliabilityEval against ``case.expected_tool_calls``.

    Agno's ReliabilityEval crashes with ``'NoneType' is not reversible``
    when ``response.messages`` is None (typically because the underlying
    LLM run errored out — rate-limited, network failure, etc.). We guard
    that upstream and surface a clean error instead of a stack trace
    pretending to be a tool-call mismatch.

    We also strip errored / unknown tool calls before Agno scans them
    (done in :class:`AgnoResponseAdapter`) — see that class's docstring
    for the full rationale.
    """

    def should_run(self, ctx: AssertionContext) -> bool:
        return bool(ctx.case.expected_tool_calls)

    async def run(self, ctx: AssertionContext) -> CheckResult:
        # ``expand_expected`` appends the always-allowed system tools,
        # which only makes sense under the strict reading, where the
        # list is also the allowlist. Agno treats every entry as
        # required too, so appending them there says "the agent must
        # call update_user_memory" — the opposite of excusing it. When
        # extra calls are already allowed, plain ``expand`` is correct
        # and the append is not just redundant but wrong.
        expanded = (
            ctx.catalog.expand(ctx.case.expected_tool_calls or [])
            if ctx.allow_additional_tool_calls
            else ctx.catalog.expand_expected(ctx.case.expected_tool_calls or [])
        )
        response = ctx.adapter.response
        if not ctx.adapter.has_messages():
            return CheckResult(
                ok=False,
                detail="agent run produced no response (likely API error / rate limit)",
            )
        try:
            rel = ReliabilityEval(
                agent_response=response,
                expected_tool_calls=expanded,
                allow_additional_tool_calls=ctx.allow_additional_tool_calls,
                print_results=False,
                telemetry=False,
            )
            result = await rel.arun(print_results=False)
            if result is None:
                return CheckResult(ok=False, detail="reliability eval returned None")
            if result.eval_status == "PASSED":
                return CheckResult(ok=True, detail="all expected tools called")
            # When the agent called ZERO tools, Agno reports every expected tool
            # as "failed" (see reliability.py:233 — `failed = expected_tool_calls`).
            # Surface a clearer message — "no tools called" is the actionable signal.
            called_any = bool(getattr(result, "passed_tool_calls", None))
            if (
                not called_any
                and result.failed_tool_calls
                and set(result.failed_tool_calls) == set(expanded)
            ):
                sample = ", ".join(expanded[:4])
                suffix = ", ..." if len(expanded) > 4 else ""
                return CheckResult(
                    ok=False,
                    detail=f"agent called no tools (expected at least one of: {sample}{suffix})",
                )
            if result.missing_tool_calls:
                missing = ", ".join(result.missing_tool_calls)
                return CheckResult(ok=False, detail=f"expected tools never called: {missing}")
            failed = ", ".join(result.failed_tool_calls) if result.failed_tool_calls else "unknown"
            return CheckResult(
                ok=False,
                detail=f"unexpected tool calls (outside allowlist): {failed}",
            )
        except Exception as exc:
            return CheckResult(ok=False, detail=f"reliability eval error: {exc}")

    def apply_to(self, result: CaseResult, check: CheckResult) -> None:
        result.reliability_passed = check.ok
        result.reliability_detail = check.detail


class UnexpectedToolsDriver(CheckDriver):
    """Blocklist check — fails if any forbidden tool was called."""

    def should_run(self, ctx: AssertionContext) -> bool:
        return bool(ctx.case.unexpected_tool_calls)

    async def run(self, ctx: AssertionContext) -> CheckResult:
        expanded = ctx.catalog.expand(ctx.case.unexpected_tool_calls or [])
        return self.check(ctx.adapter.called_tool_names(), expanded)

    @staticmethod
    def check(called: set[str], unexpected: list[str]) -> CheckResult:
        """Fail when any of ``unexpected`` appears in ``called``."""
        forbidden_used = called & set(unexpected)
        if forbidden_used:
            return CheckResult(
                ok=False,
                detail=f"unexpected tools called: {', '.join(sorted(forbidden_used))}",
            )
        return CheckResult(ok=True, detail="no unexpected tool calls")

    def apply_to(self, result: CaseResult, check: CheckResult) -> None:
        result.unexpected_passed = check.ok
        result.unexpected_detail = check.detail


class AccuracyDriver(CheckDriver):
    """Runs Agno's AccuracyEval against ``case.expected_output``.

    Skipped when no judge model is available or the case doesn't
    specify an expected output — both are part of the gate, so a
    skipped case leaves every ``accuracy_*`` field unset.
    """

    def should_run(self, ctx: AssertionContext) -> bool:
        return bool(ctx.case.expected_output) and ctx.judge_model is not None

    async def run(self, ctx: AssertionContext) -> CheckResult:
        if ctx.judge_model is None:
            # Unreachable through the runner (the gate requires a judge);
            # guards direct callers from Agno silently picking its own
            # default model.
            return CheckResult(ok=False, detail="accuracy eval error: no judge model configured")
        try:
            acc = AccuracyEval(
                agent=ctx.agent,
                input=ctx.case.input,
                expected_output=ctx.case.expected_output,
                model=ctx.judge_model,
                additional_guidelines=ctx.case.judge_guidelines,
                num_iterations=ctx.case.num_iterations,
                print_summary=False,
                print_results=False,
                telemetry=False,
            )
            result = await acc.arun_with_output(
                output=ctx.output_text,
                print_summary=False,
                print_results=False,
            )
            if result is None:
                return CheckResult(ok=False, detail="accuracy eval returned None")
            score = result.avg_score
            threshold = ctx.case.accuracy_threshold
            # ``avg_score`` is ``float | None`` — an eval that produced no
            # scored iterations leaves it unset, and ``None >= threshold``
            # is a TypeError, not a failed assertion. Reported the same way
            # as the ``result is None`` case just above rather than crashing
            # the run that was measuring something else.
            if score is None:
                return CheckResult(ok=False, detail="accuracy eval returned no score")
            passed = score >= threshold
            # Concat per-iteration reasons. Usually 1 iter, but support all.
            reasons: list[str] = []
            for r in getattr(result, "results", None) or []:
                reason = getattr(r, "reason", None)
                if reason:
                    reasons.append(reason)
            reason_text = "\n---\n".join(reasons)
            return CheckResult(
                ok=passed,
                detail=f"score {score:.1f}/{threshold}",
                score=score,
                reason=reason_text,
            )
        except Exception as exc:
            return CheckResult(ok=False, detail=f"accuracy eval error: {exc}")

    def apply_to(self, result: CaseResult, check: CheckResult) -> None:
        result.accuracy_passed = check.ok
        result.accuracy_score = check.score
        result.accuracy_reason = check.reason


class ToolArgDriver(CheckDriver):
    """Verifies each :class:`ToolArgAssertion` matched at least one call.

    An assertion passes when *some* call to ``tool`` has every key/value
    in ``args_must_contain`` present in its captured ``args`` dict. Used
    for "did spawn_team get called with mode='coordinate'?"-style checks.

    Can also be used stand-alone via :meth:`check` (used by tests to
    verify assertion behaviour without spinning up a full runner).
    """

    def should_run(self, ctx: AssertionContext) -> bool:
        return bool(ctx.case.tool_arg_assertions)

    async def run(self, ctx: AssertionContext) -> CheckResult:
        return self.check(ctx.tool_trace, ctx.case.tool_arg_assertions or [])

    @staticmethod
    def check(
        tool_trace: list[ToolTraceEntry],
        assertions: list[ToolArgAssertion],
    ) -> CheckResult:
        """Public entry point used by :meth:`run` and tests.

        Assertions with an empty ``tool`` are skipped — they name no
        call to match, so they can neither pass nor fail.
        """
        failures: list[str] = []
        for a in assertions:
            target_tool = a.tool
            required = a.args_must_contain
            if not target_tool:
                continue
            matched = False
            for call in tool_trace:
                if call.name != target_tool:
                    continue
                args = call.args or {}
                if all(args.get(k) == v for k, v in required.items()):
                    matched = True
                    break
            if not matched:
                failures.append(f"{target_tool}({required})")
        if failures:
            return CheckResult(
                ok=False,
                detail="missing tool-arg matches: " + ", ".join(failures),
            )
        return CheckResult(ok=True, detail="all tool-arg assertions matched")

    def apply_to(self, result: CaseResult, check: CheckResult) -> None:
        result.tool_arg_passed = check.ok
        result.tool_arg_detail = check.detail


class FileDriver(AssertionDriver):
    """Walks ``case.file_assertions`` and emits one
    :class:`FileCheckResult` per assertion.

    Aggregates into ``result.file_results`` via
    :meth:`CaseResult.add_file_result`. The overall pass/fail for
    "did every file assertion pass" is computed in
    :meth:`CaseResult.compute_passed` — this driver only writes the
    per-assertion outcomes.
    """

    def should_run(self, ctx: AssertionContext) -> bool:
        return bool(ctx.case.file_assertions)

    async def record(self, ctx: AssertionContext, result: CaseResult) -> None:
        for assertion in ctx.case.file_assertions or []:
            result.add_file_result(assertion.check(ctx.work_dir))


class CaseAssertionRunner:
    """Composes every :class:`AssertionDriver` and applies them to a case.

    Iteration order is intentional (reliability → unexpected → accuracy
    → tool-arg → file) so the human-readable per-check report keeps its
    historical column order.
    """

    def __init__(self, drivers: list[AssertionDriver] | None = None) -> None:
        self._drivers: list[AssertionDriver] = drivers or [
            ReliabilityDriver(),
            UnexpectedToolsDriver(),
            AccuracyDriver(),
            ToolArgDriver(),
            FileDriver(),
        ]

    async def run(self, ctx: AssertionContext, result: CaseResult) -> None:
        """Populate ``result`` with per-check outcomes from every driver."""
        for driver in self._drivers:
            if driver.should_run(ctx):
                await driver.record(ctx, result)
        result.compute_passed()
