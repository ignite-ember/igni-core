"""What each assertion driver gates on, reports and records, and how the runner orders them.

The agno evals the drivers construct are swapped for fakes that return a chosen
result; the drivers and the runner are the real ones.
"""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace as NS

import pytest

import igni_core.core.evals.assertion_runner as module
from igni_core.core.evals.agno_adapter import AgnoResponseAdapter
from igni_core.core.evals.assertion_runner import (
    AccuracyDriver,
    AssertionContext,
    AssertionDriver,
    CaseAssertionRunner,
    FileDriver,
    ReliabilityDriver,
    ToolArgDriver,
    UnexpectedToolsDriver,
)
from igni_core.core.evals.assertions import FileExists, FileNotExists
from igni_core.core.evals.schemas import (
    CaseResult,
    CheckResult,
    EvalCase,
    ToolArgAssertion,
    ToolTraceEntry,
)
from igni_core.core.evals.tool_names import DEFAULT_CATALOG


def ctx(*, adapter=None, judge="judge", work_dir=None, allow_extra=False, trace=None, **case):
    case.setdefault("name", "c")
    case.setdefault("input", "q")
    return AssertionContext(
        case=EvalCase(**case),
        adapter=adapter or AgnoResponseAdapter(NS(messages=[], tools=[], content="out")),
        output_text="out",
        tool_trace=trace or [],
        agent=NS(),  # type: ignore[arg-type]
        judge_model=judge,  # type: ignore[arg-type]
        work_dir=work_dir,
        catalog=DEFAULT_CATALOG,
        allow_additional_tool_calls=allow_extra,
    )


def adapter_of(messages, tools=None):
    return AgnoResponseAdapter(NS(messages=messages, tools=tools, content=""))


class FakeReliability:
    """Stands in for agno's ReliabilityEval; remembers how it was built."""

    built: list[dict] = []
    outcome: object = None
    boom: Exception | None = None

    def __init__(self, **kwargs):
        type(self).built.append(kwargs)

    async def arun(self, **_kwargs):
        if self.boom:
            raise self.boom
        return self.outcome


@pytest.fixture
def reliability(monkeypatch):
    FakeReliability.built = []
    FakeReliability.boom = None
    monkeypatch.setattr(module, "ReliabilityEval", FakeReliability)

    def install(outcome=None, boom=None):
        FakeReliability.outcome = outcome
        FakeReliability.boom = boom

    return install


def outcome(status="FAILED", passed=None, failed=None, missing=None):
    return NS(
        eval_status=status,
        passed_tool_calls=passed or [],
        failed_tool_calls=failed or [],
        missing_tool_calls=missing or [],
    )


class TestReliabilityDriver:
    def test_it_runs_only_when_tool_calls_are_expected(self):
        assert ReliabilityDriver().should_run(ctx(expected_tool_calls=["Write"])) is True
        assert ReliabilityDriver().should_run(ctx(expected_tool_calls=[])) is False
        assert ReliabilityDriver().should_run(ctx()) is False

    async def test_no_messages_is_reported_as_an_api_failure_without_running_agno(
        self, reliability
    ):
        check = await ReliabilityDriver().run(
            ctx(adapter=adapter_of(None), expected_tool_calls=["Write"])
        )
        assert check.ok is False
        assert check.detail == "agent run produced no response (likely API error / rate limit)"
        assert FakeReliability.built == []

    async def test_a_passing_eval(self, reliability):
        reliability(outcome("PASSED"))
        check = await ReliabilityDriver().run(
            ctx(adapter=adapter_of([]), expected_tool_calls=["Write"])
        )
        assert (check.ok, check.detail) == (True, "all expected tools called")

    async def test_a_none_result(self, reliability):
        reliability(None)
        check = await ReliabilityDriver().run(
            ctx(adapter=adapter_of([]), expected_tool_calls=["Write"])
        )
        assert (check.ok, check.detail) == (False, "reliability eval returned None")

    async def test_an_exception_becomes_a_failed_check(self, reliability):
        reliability(boom=RuntimeError("kaput"))
        check = await ReliabilityDriver().run(
            ctx(adapter=adapter_of([]), expected_tool_calls=["Write"])
        )
        assert (check.ok, check.detail) == (False, "reliability eval error: kaput")

    async def test_calling_no_tools_says_so_and_samples_four_expected(self, reliability):
        expanded = DEFAULT_CATALOG.expand_expected(["Bash"])
        assert len(expanded) > 4
        reliability(outcome(failed=expanded))
        check = await ReliabilityDriver().run(
            ctx(adapter=adapter_of([]), expected_tool_calls=["Bash"])
        )
        sample = ", ".join(expanded[:4])
        assert check.detail == f"agent called no tools (expected at least one of: {sample}, ...)"

    async def test_a_short_expected_list_has_no_ellipsis(self, reliability):
        expanded = DEFAULT_CATALOG.expand_expected(["Write"])
        assert len(expanded) <= 4
        reliability(outcome(failed=expanded))
        check = await ReliabilityDriver().run(
            ctx(adapter=adapter_of([]), expected_tool_calls=["Write"])
        )
        assert check.detail == (
            f"agent called no tools (expected at least one of: {', '.join(expanded)})"
        )

    async def test_missing_tools_are_named(self, reliability):
        reliability(outcome(passed=["a"], failed=["a", "b"], missing=["b", "c"]))
        check = await ReliabilityDriver().run(
            ctx(adapter=adapter_of([]), expected_tool_calls=["a", "b"])
        )
        assert check.detail == "expected tools never called: b, c"

    async def test_some_tools_called_but_all_failed_is_not_the_no_tools_message(self, reliability):
        expanded = DEFAULT_CATALOG.expand_expected(["Write"])
        reliability(outcome(passed=["x"], failed=expanded))
        check = await ReliabilityDriver().run(
            ctx(adapter=adapter_of([]), expected_tool_calls=["Write"])
        )
        assert check.detail.startswith("unexpected tool calls (outside allowlist)")

    async def test_unexpected_calls_are_listed(self, reliability):
        reliability(outcome(passed=["ok"], failed=["rogue"]))
        check = await ReliabilityDriver().run(
            ctx(adapter=adapter_of([]), expected_tool_calls=["Write"])
        )
        assert check.detail == "unexpected tool calls (outside allowlist): rogue"

    async def test_no_failed_list_reads_unknown(self, reliability):
        reliability(outcome(passed=["ok"]))
        check = await ReliabilityDriver().run(
            ctx(adapter=adapter_of([]), expected_tool_calls=["Write"])
        )
        assert check.detail == "unexpected tool calls (outside allowlist): unknown"

    async def test_strict_reading_adds_the_always_allowed_system_tools(self, reliability):
        reliability(outcome("PASSED"))
        await ReliabilityDriver().run(
            ctx(adapter=adapter_of([]), expected_tool_calls=["Write"], allow_extra=False)
        )
        sent = FakeReliability.built[0]
        assert sent["expected_tool_calls"] == DEFAULT_CATALOG.expand_expected(["Write"])
        assert set(DEFAULT_CATALOG.always_allowed_system_tools) <= set(sent["expected_tool_calls"])
        assert sent["allow_additional_tool_calls"] is False

    async def test_allowing_extra_calls_does_not_make_the_system_tools_required(self, reliability):
        reliability(outcome("PASSED"))
        await ReliabilityDriver().run(
            ctx(adapter=adapter_of([]), expected_tool_calls=["Write"], allow_extra=True)
        )
        sent = FakeReliability.built[0]
        assert sent["expected_tool_calls"] == DEFAULT_CATALOG.expand(["Write"])
        assert not set(DEFAULT_CATALOG.always_allowed_system_tools) & set(
            sent["expected_tool_calls"]
        )
        assert sent["allow_additional_tool_calls"] is True

    def test_it_writes_the_reliability_fields(self):
        result = CaseResult(case=EvalCase(name="c", input="q"))
        ReliabilityDriver().apply_to(result, CheckResult(ok=False, detail="d"))
        assert (result.reliability_passed, result.reliability_detail) == (False, "d")


class TestUnexpectedToolsDriver:
    def test_gate(self):
        assert UnexpectedToolsDriver().should_run(ctx(unexpected_tool_calls=["Bash"])) is True
        assert UnexpectedToolsDriver().should_run(ctx(unexpected_tool_calls=[])) is False

    async def test_a_display_name_is_expanded_before_comparing(self):
        adapter = adapter_of(None, tools=[NS(tool_name="run_shell_command")])
        check = await UnexpectedToolsDriver().run(
            ctx(adapter=adapter, unexpected_tool_calls=["Bash"])
        )
        assert check.ok is False
        assert check.detail == "unexpected tools called: run_shell_command"

    def test_forbidden_tools_are_listed_sorted(self):
        check = UnexpectedToolsDriver.check({"b", "a", "c"}, ["c", "a", "z"])
        assert check.detail == "unexpected tools called: a, c"

    def test_none_called_passes(self):
        check = UnexpectedToolsDriver.check({"a"}, ["z"])
        assert (check.ok, check.detail) == (True, "no unexpected tool calls")

    def test_it_writes_the_unexpected_fields(self):
        result = CaseResult(case=EvalCase(name="c", input="q"))
        UnexpectedToolsDriver().apply_to(result, CheckResult(ok=True, detail="d"))
        assert (result.unexpected_passed, result.unexpected_detail) == (True, "d")


class FakeAccuracy:
    built: list[dict] = []
    calls: list[dict] = []
    result: object = None
    boom: Exception | None = None

    def __init__(self, **kwargs):
        type(self).built.append(kwargs)

    async def arun_with_output(self, **kwargs):
        type(self).calls.append(kwargs)
        if self.boom:
            raise self.boom
        return self.result


@pytest.fixture
def accuracy(monkeypatch):
    FakeAccuracy.built, FakeAccuracy.calls, FakeAccuracy.boom = [], [], None
    monkeypatch.setattr(module, "AccuracyEval", FakeAccuracy)

    def install(result=None, boom=None):
        FakeAccuracy.result = result
        FakeAccuracy.boom = boom

    return install


class TestAccuracyDriver:
    def test_it_needs_an_expected_output_and_a_judge(self):
        assert AccuracyDriver().should_run(ctx(expected_output="a")) is True
        assert AccuracyDriver().should_run(ctx(expected_output="a", judge=None)) is False
        assert AccuracyDriver().should_run(ctx()) is False
        assert AccuracyDriver().should_run(ctx(expected_output="")) is False

    async def test_without_a_judge_the_direct_call_fails_instead_of_defaulting(self):
        check = await AccuracyDriver().run(ctx(expected_output="a", judge=None))
        assert (check.ok, check.detail) == (
            False,
            "accuracy eval error: no judge model configured",
        )

    async def test_a_none_result(self, accuracy):
        accuracy(None)
        check = await AccuracyDriver().run(ctx(expected_output="a"))
        assert (check.ok, check.detail) == (False, "accuracy eval returned None")

    async def test_an_exception_becomes_a_failed_check(self, accuracy):
        accuracy(boom=ValueError("judge down"))
        check = await AccuracyDriver().run(ctx(expected_output="a"))
        assert (check.ok, check.detail) == (False, "accuracy eval error: judge down")

    async def test_the_detail_shows_score_and_threshold_and_reasons_are_joined(self, accuracy):
        accuracy(
            NS(
                avg_score=8.25,
                results=[NS(reason="first"), NS(reason=None), NS(reason=""), NS(reason="second")],
            )
        )
        check = await AccuracyDriver().run(
            ctx(
                expected_output="a",
                accuracy_threshold=7.5,
                num_iterations=2,
                judge_guidelines="be strict",
            )
        )
        assert check.ok is True
        assert check.detail == "score 8.2/7.5"
        assert check.score == 8.25
        assert check.reason == "first\n---\nsecond"

    async def test_the_judge_is_given_the_case_and_the_agent_output(self, accuracy):
        accuracy(NS(avg_score=9.0, results=[]))
        await AccuracyDriver().run(
            ctx(expected_output="exp", input="ask", num_iterations=4, judge_guidelines="g")
        )
        built = FakeAccuracy.built[0]
        assert (built["input"], built["expected_output"], built["model"]) == (
            "ask",
            "exp",
            "judge",
        )
        assert (built["num_iterations"], built["additional_guidelines"]) == (4, "g")
        assert FakeAccuracy.calls[0]["output"] == "out"

    def test_it_writes_the_accuracy_fields(self):
        result = CaseResult(case=EvalCase(name="c", input="q"))
        AccuracyDriver().apply_to(result, CheckResult(ok=True, detail="d", score=3.0, reason="r"))
        assert (result.accuracy_passed, result.accuracy_score, result.accuracy_reason) == (
            True,
            3.0,
            "r",
        )


def entry(name, args):
    return ToolTraceEntry(name=name, args=args)


class TestToolArgDriver:
    def test_gate(self):
        assert ToolArgDriver().should_run(ctx()) is False
        assert (
            ToolArgDriver().should_run(ctx(tool_arg_assertions=[ToolArgAssertion(tool="t")]))
            is True
        )

    def test_one_matching_call_among_several_is_enough(self):
        trace = [entry("spawn", {"mode": "broadcast"}), entry("spawn", {"mode": "coordinate"})]
        check = ToolArgDriver.check(
            trace, [ToolArgAssertion(tool="spawn", args_must_contain={"mode": "coordinate"})]
        )
        assert (check.ok, check.detail) == (True, "all tool-arg assertions matched")

    def test_an_empty_requirement_matches_any_call_to_the_tool(self):
        check = ToolArgDriver.check([entry("t", None)], [ToolArgAssertion(tool="t")])
        assert check.ok is True

    def test_a_call_without_args_cannot_satisfy_a_requirement(self):
        check = ToolArgDriver.check(
            [entry("t", None)], [ToolArgAssertion(tool="t", args_must_contain={"a": 1})]
        )
        assert check.ok is False

    def test_failures_are_listed_as_tool_and_args(self):
        check = ToolArgDriver.check(
            [entry("t", {"a": 1})],
            [
                ToolArgAssertion(tool="t", args_must_contain={"a": 2}),
                ToolArgAssertion(tool="u", args_must_contain={"b": 3}),
                ToolArgAssertion(tool="t", args_must_contain={"a": 1}),
            ],
        )
        assert check.ok is False
        assert check.detail == "missing tool-arg matches: t({'a': 2}), u({'b': 3})"

    def test_an_assertion_naming_no_tool_is_skipped(self):
        check = ToolArgDriver.check([], [ToolArgAssertion(tool="", args_must_contain={"a": 1})])
        assert check.ok is True

    async def test_run_reads_the_trace_from_the_context(self):
        assertions = [ToolArgAssertion(tool="t", args_must_contain={"a": 1})]
        check = await ToolArgDriver().run(
            ctx(trace=[entry("t", {"a": 1})], tool_arg_assertions=assertions)
        )
        assert check.ok is True

    def test_it_writes_the_tool_arg_fields(self):
        result = CaseResult(case=EvalCase(name="c", input="q"))
        ToolArgDriver().apply_to(result, CheckResult(ok=False, detail="d"))
        assert (result.tool_arg_passed, result.tool_arg_detail) == (False, "d")


class TestFileDriver:
    def test_gate(self):
        assert FileDriver().should_run(ctx()) is False
        assert FileDriver().should_run(ctx(file_assertions=[FileExists(path="a")])) is True

    async def test_one_result_per_assertion_resolved_against_the_work_dir(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        result = CaseResult(case=EvalCase(name="c", input="q"))
        await FileDriver().record(
            ctx(
                work_dir=tmp_path,
                file_assertions=[FileExists(path="a.txt"), FileNotExists(path="a.txt")],
            ),
            result,
        )
        assert [(r.type, r.passed) for r in result.file_results] == [
            ("file_exists", True),
            ("file_not_exists", False),
        ]


class Recorder(AssertionDriver):
    def __init__(self, label, log, gate=True):
        self.label, self.log, self.gate = label, log, gate

    def should_run(self, ctx):
        return self.gate

    async def record(self, ctx, result):
        self.log.append(self.label)


class TestCaseAssertionRunner:
    def test_the_default_drivers_are_in_the_report_order(self):
        runner = CaseAssertionRunner()
        names = [type(d).__name__ for d in runner._drivers]
        assert names == [
            "ReliabilityDriver",
            "UnexpectedToolsDriver",
            "AccuracyDriver",
            "ToolArgDriver",
            "FileDriver",
        ]

    async def test_every_open_gate_records_and_the_verdict_is_derived(
        self, tmp_path, reliability, accuracy
    ):
        reliability(outcome("PASSED"))
        accuracy(NS(avg_score=9.0, results=[]))
        context = ctx(
            adapter=adapter_of([], tools=[]),
            work_dir=tmp_path,
            expected_tool_calls=["Write"],
            unexpected_tool_calls=["Bash"],
            expected_output="a",
            tool_arg_assertions=[ToolArgAssertion(tool="t")],
            file_assertions=[FileExists(path="missing.txt")],
            trace=[entry("t", {})],
        )
        result = CaseResult(case=context.case)

        await CaseAssertionRunner().run(context, result)

        assert result.reliability_passed is True
        assert result.unexpected_passed is True
        assert result.accuracy_passed is True
        assert result.tool_arg_passed is True
        assert [r.passed for r in result.file_results] == [False]
        assert result.passed is False

    async def test_closed_gates_leave_their_fields_unset_and_the_case_passes(self):
        result = CaseResult(case=EvalCase(name="c", input="q"))
        await CaseAssertionRunner().run(ctx(), result)
        assert result.reliability_passed is None
        assert result.unexpected_passed is None
        assert result.accuracy_passed is None
        assert result.tool_arg_passed is None
        assert result.file_results == []
        assert result.passed is True

    async def test_custom_drivers_run_in_the_given_order_and_closed_gates_are_skipped(self):
        log: list[str] = []
        runner = CaseAssertionRunner(
            [Recorder("b", log), Recorder("skipped", log, gate=False), Recorder("a", log)]
        )
        await runner.run(ctx(), CaseResult(case=EvalCase(name="c", input="q")))
        assert log == ["b", "a"]

    async def test_an_empty_driver_list_falls_back_to_the_defaults(self):
        runner = CaseAssertionRunner([])
        assert len(runner._drivers) == 5


class TestAssertionContext:
    def test_it_is_frozen(self):
        with pytest.raises(FrozenInstanceError):
            ctx().output_text = "changed"  # type: ignore[misc]

    def test_extra_tool_calls_are_not_allowed_by_default(self):
        assert ctx().allow_additional_tool_calls is False
