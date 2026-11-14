"""The names the evals package re-exports at its root."""

import igni_core.core.evals as evals_package


class TestPackageRoot:
    def test_it_exports_exactly_the_six_names(self):
        assert sorted(evals_package.__all__) == [
            "CaseResult",
            "CaseRunner",
            "EvalCase",
            "EvalSuite",
            "SuiteResult",
            "SuiteRunner",
        ]

    def test_each_name_is_the_submodule_class(self):
        from igni_core.core.evals import loader, runner

        assert evals_package.EvalCase is loader.EvalCase
        assert evals_package.EvalSuite is loader.EvalSuite
        assert evals_package.CaseResult is runner.CaseResult
        assert evals_package.SuiteResult is runner.SuiteResult
        assert evals_package.CaseRunner is runner.CaseRunner
        assert evals_package.SuiteRunner is runner.SuiteRunner
