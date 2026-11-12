"""The `/evals` chat card: an empty-state hint, or the report of what ran."""

from __future__ import annotations

import pytest

from igni_core.backend.schemas_evals import EvalRunView
from igni_core.core.evals.schemas import CaseResult, EvalCase, EvalSuite, SuiteResult
from igni_core.core.paths import CONFIG_DIR
from igni_core.protocol.messages import CommandResultKind


def suite_result() -> SuiteResult:
    suite = EvalSuite(agent="reviewer")
    return SuiteResult(
        suite=suite,
        case_results=[
            CaseResult(case=EvalCase(name="finds-bug", input="x"), passed=True, elapsed=1.0),
            CaseResult(case=EvalCase(name="misses-bug", input="y"), passed=False, elapsed=2.0),
        ],
    )


def test_no_results_tells_where_to_put_suites():
    result = EvalRunView(results=[]).to_command_result()

    assert result.kind == CommandResultKind.INFO
    assert result.content == f"No eval suites found. Add YAML files to {CONFIG_DIR}/evals/"


def test_results_render_as_a_markdown_report_with_the_totals_and_failures():
    result = EvalRunView(results=[suite_result()]).to_command_result()

    assert result.kind == CommandResultKind.MARKDOWN
    lines = result.content.splitlines()
    assert lines[0] == "## Eval Results"
    assert "**reviewer** (2 cases)" in lines
    assert "  1/2 passed" in lines
    assert "**Total: 1/2 passed (50%) in 3.0s**" in lines
    assert "Failed: reviewer.misses-bug" in lines


def test_a_suite_with_no_cases_is_still_a_report_not_the_empty_state():
    result = EvalRunView(results=[SuiteResult(suite=EvalSuite(agent="idle"))]).to_command_result()

    assert result.kind == CommandResultKind.MARKDOWN
    assert "**idle** (0 cases)" in result.content
    assert "**Total: 0/0 passed (0%) in 0.0s**" in result.content


def test_a_result_the_reporter_cannot_read_raises_rather_than_rendering_nothing():
    with pytest.raises(AttributeError):
        EvalRunView(results=[object()]).to_command_result()
