"""The exact Markdown the eval report renders, its totals and its failed-case list."""

from igni_core.core.evals.assertions import FileCheckResult
from igni_core.core.evals.reporter import EvalReport, format_results
from igni_core.core.evals.schemas import CaseResult, EvalCase, EvalSuite, SuiteResult


def case(name, passed, elapsed=0.0, **fields):
    return CaseResult(case=EvalCase(name=name, input="q"), passed=passed, elapsed=elapsed, **fields)


def suite(agent, *cases):
    return SuiteResult(suite=EvalSuite(agent=agent), case_results=list(cases))


class TestRender:
    def test_the_whole_layout_for_a_mixed_suite(self):
        failing = case(
            "broken",
            False,
            elapsed=2.25,
            error="boom",
            reliability_passed=False,
            reliability_detail="expected tools never called: Edit",
            accuracy_score=4.0,
            accuracy_passed=False,
            file_results=[FileCheckResult(type="file_exists", passed=False, detail="x missing")],
        )
        report = EvalReport([suite("editor", case("fine", True, elapsed=1.0), failing)]).render()

        assert report == "\n".join(
            [
                "## Eval Results",
                "",
                "**editor** (2 cases)",
                f"  + {'fine':<35} 1.0s",
                f"  x {'broken':<35} 2.2s  [reliability: FAIL, accuracy: 4.0, files: FAIL]",
                "    error: boom",
                "    reliability: expected tools never called: Edit",
                "    accuracy: score 4.0",
                "    file_exists: x missing",
                "  1/2 passed",
                "",
                "---",
                "**Total: 1/2 passed (50%) in 3.2s**",
                "Failed: editor.broken",
            ]
        )

    def test_no_results_still_renders_a_header_and_a_zero_total(self):
        assert (
            EvalReport([]).render() == "## Eval Results\n\n---\n**Total: 0/0 passed (0%) in 0.0s**"
        )

    def test_a_suite_with_no_cases(self):
        text = EvalReport([suite("empty")]).render()
        assert "**empty** (0 cases)\n  0/0 passed\n" in text
        assert text.endswith("**Total: 0/0 passed (0%) in 0.0s**")

    def test_an_all_passing_run_has_no_failed_line(self):
        text = EvalReport([suite("a", case("x", True), case("y", True))]).render()
        assert "Failed:" not in text
        assert "**Total: 2/2 passed (100%) in 0.0s**" in text

    def test_the_percentage_is_floored(self):
        text = EvalReport([suite("a", case("x", True), case("y", True), case("z", False))]).render()
        assert "(66%)" in text

    def test_totals_sum_every_suite_and_failures_keep_suite_then_case_order(self):
        text = EvalReport(
            [
                suite("one", case("a", False, elapsed=0.5), case("b", True, elapsed=0.5)),
                suite("two", case("a", False, elapsed=1.0), case("c", True)),
            ]
        ).render()
        assert "  1/2 passed" in text
        assert "**Total: 2/4 passed (50%) in 2.0s**" in text
        assert text.endswith("Failed: one.a, two.a")


class TestShim:
    def test_format_results_is_the_report_render(self):
        results = [suite("a", case("x", True), case("y", False))]
        assert format_results(results) == EvalReport(results).render()
