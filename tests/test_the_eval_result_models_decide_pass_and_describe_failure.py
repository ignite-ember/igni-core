"""How an eval case's checks combine into a verdict and how a failure is worded."""

import pytest
from pydantic import ValidationError

from igni_core.core.evals.assertions import FileCheckResult
from igni_core.core.evals.schemas import (
    AgnoRunOptions,
    CaseResult,
    CheckResult,
    EvalCase,
    FixtureSpec,
    SuiteResult,
    ToolArgAssertion,
)


def result(**fields):
    return CaseResult(case=EvalCase(name="c", input="q"), **fields)


def file_result(passed, detail="d"):
    return FileCheckResult(type="file_exists", passed=passed, detail=detail)


class TestComputePassed:
    def test_a_case_with_no_checks_passes(self):
        r = result()
        r.compute_passed()
        assert r.passed is True

    @pytest.mark.parametrize(
        "field", ["reliability_passed", "unexpected_passed", "accuracy_passed", "tool_arg_passed"]
    )
    def test_any_failed_check_fails_the_case(self, field):
        r = result(passed=True, **{field: False})
        r.compute_passed()
        assert r.passed is False

    def test_skipped_and_passed_checks_do_not_fail_it(self):
        r = result(reliability_passed=True, unexpected_passed=None, accuracy_passed=True)
        r.compute_passed()
        assert r.passed is True

    def test_one_failed_file_result_fails_it(self):
        r = result()
        r.add_file_result(file_result(True))
        r.add_file_result(file_result(False))
        r.compute_passed()
        assert r.passed is False

    def test_passing_file_results_keep_it_passing(self):
        r = result()
        r.add_file_result(file_result(True))
        r.compute_passed()
        assert r.passed is True


class TestBadgesAndLines:
    def test_no_check_ran_means_no_badges(self):
        assert result().status_badges() == []

    def test_badges_only_for_checks_that_ran(self):
        r = result(reliability_passed=False, accuracy_score=8.25)
        assert r.status_badges() == ["reliability: FAIL", "accuracy: 8.2"]

    def test_many_file_results_collapse_into_one_badge(self):
        r = result(file_results=[file_result(True), file_result(False)])
        assert r.status_badges() == ["files: FAIL"]
        r = result(file_results=[file_result(True), file_result(True)])
        assert r.status_badges() == ["files: PASS"]

    def test_the_tool_arg_check_has_no_badge(self):
        assert result(tool_arg_passed=False).status_badges() == []

    def test_summary_line_pads_the_name_and_marks_the_verdict(self):
        r = CaseResult(case=EvalCase(name="ab", input="q"), passed=True, elapsed=1.26)
        assert r.summary_line() == f"  + {'ab':<35} 1.3s"
        r = CaseResult(
            case=EvalCase(name="é" * 40, input="q"),
            elapsed=0,
            reliability_passed=True,
        )
        assert r.summary_line() == f"  x {'é' * 40} 0.0s  [reliability: PASS]"

    def test_a_passing_case_has_no_failure_lines(self):
        assert result(passed=True, reliability_passed=True).failure_detail_lines() == []

    def test_each_failed_check_gets_a_line_in_order(self):
        r = result(
            error="boom",
            reliability_passed=False,
            reliability_detail="rel detail",
            unexpected_passed=False,
            unexpected_detail="unexpected tool calls: x",
            accuracy_passed=False,
            accuracy_score=3.5,
            tool_arg_passed=False,
            tool_arg_detail="missing tool-arg matches: t({})",
            file_results=[file_result(True), file_result(False, "gone")],
        )
        assert r.failure_detail_lines() == [
            "    error: boom",
            "    reliability: rel detail",
            "    unexpected tool calls: x",
            "    accuracy: score 3.5",
            "    missing tool-arg matches: t({})",
            "    file_exists: gone",
        ]

    def test_a_zero_accuracy_score_is_shown_as_zero(self):
        r = result(accuracy_passed=False, accuracy_score=0.0)
        assert r.failure_detail_lines() == ["    accuracy: score 0.0"]

    def test_an_accuracy_failure_without_a_score_shows_a_question_mark(self):
        r = result(accuracy_passed=False, accuracy_score=None)
        assert r.failure_detail_lines() == ["    accuracy: score ?"]


class TestSmallModels:
    def test_run_options_drop_a_missing_session_id(self):
        assert AgnoRunOptions().dump() == {"stream": False}
        assert AgnoRunOptions(session_id="s").dump() == {"stream": False, "session_id": "s"}

    def test_run_options_refuse_unknown_keys(self):
        with pytest.raises(ValidationError):
            AgnoRunOptions(bogus=1)

    def test_check_result_defaults_and_closed(self):
        c = CheckResult(ok=True)
        assert (c.detail, c.score, c.reason) == ("", None, "")
        with pytest.raises(ValidationError):
            CheckResult(ok=True, extra=1)

    def test_fixture_and_tool_arg_defaults_ignore_unknown_keys(self):
        f = FixtureSpec(whatever=1)
        assert (f.source, f.target) == ("", "")
        t = ToolArgAssertion(whatever=1)
        assert (t.tool, t.args_must_contain) == ("", {})

    def test_case_defaults(self):
        c = EvalCase(name="n", input="i", unknown="x")
        assert c.accuracy_threshold == 7.0
        assert c.num_iterations == 1
        assert c.prior_messages == []
        assert c.file_assertions is None
        assert c.case_timeout is None

    def test_a_misspelled_file_assertion_type_fails_the_load(self):
        with pytest.raises(ValidationError):
            EvalCase(name="n", input="i", file_assertions=[{"type": "file_exist", "path": "a"}])

    def test_file_assertions_become_their_kinds(self):
        c = EvalCase(name="n", input="i", file_assertions=[{"type": "file_exists", "path": "a"}])
        assert type(c.file_assertions[0]).__name__ != "dict"
        assert c.file_assertions[0].type == "file_exists"

    def test_suite_totals_are_zero_when_empty(self):
        s = SuiteResult(suite={"agent": "a"})
        assert (s.passed, s.failed, s.total, s.elapsed) == (0, 0, 0, 0.0)
