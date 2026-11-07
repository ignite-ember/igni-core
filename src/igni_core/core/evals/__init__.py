"""Agent evaluation framework — YAML-driven evals backed by Agno."""

from igni_core.core.evals.loader import EvalCase, EvalSuite
from igni_core.core.evals.runner import CaseResult, CaseRunner, SuiteResult, SuiteRunner

__all__ = [
    "EvalCase",
    "EvalSuite",
    "CaseResult",
    "SuiteResult",
    "CaseRunner",
    "SuiteRunner",
]
