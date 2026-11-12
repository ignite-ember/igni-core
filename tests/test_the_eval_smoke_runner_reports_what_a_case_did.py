"""The live-model smoke runner (``scripts/run_evals_smoke.py``).

The scheduled live-model workflow runs this script against a real model;
nothing here needs one. The model-facing parts are replaced by a fake
case runner, so what is pinned is what the runner itself decides: how a
case is categorised, retried, timed out, reported and summarised, and
what it leaves on disk.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from igni_core.core.evals import runner as eval_runner
from igni_core.core.evals.loader import load_eval_file
from igni_core.core.evals.schemas import CaseResult, EvalCase, ToolTraceEntry

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "run_evals_smoke.py"


@pytest.fixture
def smoke(monkeypatch):
    """The script as a module, without leaving its DuckDuckGo stubs behind."""
    from agno.tools.duckduckgo import DuckDuckGoTools

    monkeypatch.setattr(DuckDuckGoTools, "web_search", DuckDuckGoTools.web_search)
    monkeypatch.setattr(DuckDuckGoTools, "search_news", DuckDuckGoTools.search_news)
    spec = importlib.util.spec_from_file_location("_run_evals_smoke_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _case(name: str = "c", **kw) -> EvalCase:
    return EvalCase(name=name, input="do it", **kw)


# ── Nothing in the script names the old product ────────────────────


def test_the_script_does_not_name_the_old_product():
    assert not re.search(r"\bember", SCRIPT.read_text(), re.IGNORECASE)


def test_every_editor_case_lands_in_the_category_its_name_asks_for(smoke):
    """The category keywords are matched against the shipped case names;
    a keyword that matches nothing sends its case to the catch-all."""
    suite = load_eval_file(REPO / "evals" / "editor.yaml")
    by_name = {c.name: smoke._categorize(c.name, "editor") for c in suite.cases}
    assert by_name["follows_igni_md_conventions"] == "E. verification"
    assert by_name["conflicting_igni_md_vs_request"] == "F. judgement"
    assert not [n for n in by_name if "ember" in n]


# ── What --help promises about timeouts ────────────────────────────


def _help_text(smoke, monkeypatch, capsys) -> str:
    monkeypatch.setattr(sys, "argv", ["run_evals_smoke.py", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(smoke.main())
    assert exit_info.value.code == 0
    return " ".join(capsys.readouterr().out.split())


def test_the_help_says_a_timed_out_case_is_retried(smoke, monkeypatch, capsys):
    """``_run_case`` retries a timeout like a model error; the help must not
    tell the person a stuck case is run only once."""
    text = _help_text(smoke, monkeypatch, capsys)
    assert "Does NOT retry on case_timeout" not in text
    assert "and on case_timeout" in text


def test_the_help_says_a_timed_out_case_is_reported_as_env(smoke, monkeypatch, capsys):
    """The suite prints a timeout as ENV, not FAIL (it is the environment's
    failure, not the eval's); the help must say the same."""
    text = _help_text(smoke, monkeypatch, capsys)
    assert "marked FAIL" not in text
    assert "reported as ENV with reason=case_timeout" in text
