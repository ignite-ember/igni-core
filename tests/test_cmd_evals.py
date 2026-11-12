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
    assert result.content.startswith("No eval suites found")
