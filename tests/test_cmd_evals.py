"""``/evals`` — run the project's suites, optionally for one agent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend import cmd_evals as module
from igni_core.backend.cmd_evals import cmd_evals
from igni_core.core.config.settings import Settings
from igni_core.core.paths import CONFIG_DIR
from igni_core.protocol.messages import CommandResultKind


def _handler(tmp_path):
    session = SimpleNamespace(pool=object(), settings=Settings(), project_dir=tmp_path)
    return SimpleNamespace(session=session), session


@pytest.mark.asyncio
async def test_a_project_without_suites_says_where_to_add_them(tmp_path):
    handler, _ = _handler(tmp_path)
    result = await cmd_evals(handler, "")
    assert result.kind is CommandResultKind.INFO
    assert result.content == f"No eval suites found. Add YAML files to {CONFIG_DIR}/evals/"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "expected"), [("", None), ("   ", None), ("  reviewer ", "reviewer")]
)
async def test_the_filter_is_the_trimmed_argument_or_none(monkeypatch, tmp_path, args, expected):
    seen: dict = {}

    async def run_all(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(module.SuiteRunner, "run_all", run_all)
    handler, session = _handler(tmp_path)

    await cmd_evals(handler, args)

    assert seen["agent_filter"] == expected
    assert seen["pool"] is session.pool
    assert seen["settings"] is session.settings
    assert seen["project_dir"] == tmp_path


@pytest.mark.asyncio
async def test_a_filter_naming_no_agent_runs_nothing(tmp_path):
    handler, _ = _handler(tmp_path)
    suites = tmp_path / CONFIG_DIR / "evals"
    suites.mkdir(parents=True)
    (suites / "one.yaml").write_text(
        "agent: reviewer\ncases:\n  - name: c\n    input: hi\n    expected_output: hi\n"
    )
    result = await cmd_evals(handler, "nobody")
    # The suites exist; saying "No eval suites found" sent people looking
    # for a missing file (campaign R056).
    assert "No eval suites found" not in result.content
    assert "nobody" in result.content and "reviewer" in result.content


def _suite(tmp_path, agent="reviewer", cases=("capital", "river")):
    suites = tmp_path / CONFIG_DIR / "evals"
    suites.mkdir(parents=True, exist_ok=True)
    body = "".join(f"  - name: {c}\n    input: hi\n    expected_output: hi\n" for c in cases)
    (suites / f"{agent}.yaml").write_text(f"agent: {agent}\ncases:\n{body}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "agent", "case"),
    [
        ("run", None, None),
        ("run reviewer", "reviewer", None),
        ("run reviewer.capital", "reviewer", "capital"),
        ("reviewer.capital", "reviewer", "capital"),
        ("reviewer", "reviewer", None),
    ],
)
async def test_the_documented_forms_reach_the_runner(monkeypatch, tmp_path, args, agent, case):
    """EVALS.md documents ``/evals run``, ``/evals run <agent>`` and
    ``/evals run <agent>.<case>``; the whole string was taken as an agent
    name, so each form answered 'No eval suites found' (campaign R056)."""
    seen: dict = {}

    async def run_all(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(module.SuiteRunner, "run_all", run_all)
    handler, _ = _handler(tmp_path)

    await cmd_evals(handler, args)

    assert seen["agent_filter"] == agent
    assert seen.get("case_filter") == case


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args", ["run --changed", "run --orchestrator", "diff", "baseline set", "baseline check"]
)
async def test_a_form_igni_does_not_have_says_so(monkeypatch, tmp_path, args):
    called: list = []

    async def run_all(**kwargs):
        called.append(kwargs)
        return []

    monkeypatch.setattr(module.SuiteRunner, "run_all", run_all)
    handler, _ = _handler(tmp_path)

    result = await cmd_evals(handler, args)

    assert called == []
    assert result.kind is CommandResultKind.INFO
    assert "not available" in result.content
    assert "/evals <agent>.<case>" in result.content


@pytest.mark.asyncio
async def test_a_case_filter_keeps_only_that_case(monkeypatch, tmp_path):
    _suite(tmp_path)
    ran: list = []

    async def run(self):
        ran.append([c.name for c in self._suite.cases])
        return SimpleNamespace()

    monkeypatch.setattr(module.SuiteRunner, "run", run)
    handler, _ = _handler(tmp_path)
    monkeypatch.setattr(module.EvalRunView, "to_command_result", lambda self: None)

    await cmd_evals(handler, "run reviewer.capital")

    assert ran == [["capital"]]


@pytest.mark.asyncio
async def test_a_case_naming_nothing_says_which_cases_there_are(tmp_path):
    _suite(tmp_path)
    handler, _ = _handler(tmp_path)

    result = await cmd_evals(handler, "reviewer.nosuch")

    assert "nosuch" in result.content
    assert "capital" in result.content and "river" in result.content
