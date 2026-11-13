"""The skill executor: which agent runs a skill, and what a failure looks like.

Real ``SkillDefinition`` objects, real rendering; only the agent pool and the
agent's ``arun`` are doubles, because the model is outside this unit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.core.skills.executor import SkillExecutor, SkillResult
from igni_core.core.skills.parser import SkillDefinition


class _Agent:
    def __init__(self, response=None, raises: BaseException | None = None):
        self.prompts: list[str] = []
        self._response = response
        self._raises = raises

    async def arun(self, prompt):
        self.prompts.append(prompt)
        if self._raises is not None:
            raise self._raises
        return self._response


class _Pool:
    def __init__(self, **agents):
        self.agents = agents
        self.asked: list[str] = []

    def get(self, name):
        self.asked.append(name)
        return self.agents.get(name)


def _settings(default_agent="orchestrator"):
    return SimpleNamespace(skills=SimpleNamespace(default_agent=default_agent))


def _skill(**kw):
    kw.setdefault("name", "deploy")
    kw.setdefault("body", "Deploy $ARGUMENTS in ${IGNI_SESSION_ID}")
    return SkillDefinition(**kw)


def test_a_skill_result_defaults_to_an_empty_success():
    result = SkillResult()
    assert (result.ok, result.text, result.error) == (True, "", None)


@pytest.mark.asyncio
async def test_an_inline_skill_runs_on_the_default_agent_with_the_rendered_body():
    agent = _Agent(response=SimpleNamespace(content="done"))
    pool = _Pool(orchestrator=agent, editor=_Agent())
    result = await SkillExecutor(pool, _settings(), session_id="s-1").execute(_skill(), "prod")

    assert pool.asked == ["orchestrator"]
    assert agent.prompts == ["Deploy prod in s-1"]
    assert result == SkillResult(ok=True, text="done", error=None)


@pytest.mark.asyncio
async def test_a_fork_skill_runs_on_its_named_agent():
    editor = _Agent(response=SimpleNamespace(content="edited"))
    pool = _Pool(orchestrator=_Agent(), editor=editor)
    result = await SkillExecutor(pool, _settings()).execute(_skill(context="fork", agent="editor"))

    assert pool.asked == ["editor"]
    assert result.text == "edited"


@pytest.mark.asyncio
async def test_a_fork_skill_without_an_agent_falls_back_to_the_default_agent():
    pool = _Pool(orchestrator=_Agent(response=SimpleNamespace(content="x")))
    result = await SkillExecutor(pool, _settings()).execute(_skill(context="fork", agent=None))

    assert pool.asked == ["orchestrator"]
    assert result.ok is True


@pytest.mark.asyncio
async def test_an_agent_on_an_inline_skill_is_ignored():
    pool = _Pool(orchestrator=_Agent(response=SimpleNamespace(content="x")))
    await SkillExecutor(pool, _settings()).execute(_skill(context="inline", agent="editor"))
    assert pool.asked == ["orchestrator"]


@pytest.mark.asyncio
async def test_a_missing_fork_agent_is_reported_in_text_and_error():
    result = await SkillExecutor(_Pool(), _settings()).execute(
        _skill(context="fork", agent="ghost")
    )
    message = "Error: Agent 'ghost' not found for skill 'deploy'."
    assert result == SkillResult(ok=False, text=message, error=message)


@pytest.mark.asyncio
async def test_a_missing_default_agent_names_it_in_text_and_error():
    result = await SkillExecutor(_Pool(), _settings("boss")).execute(_skill())
    message = "Error: No 'boss' agent available for skill 'deploy'."
    assert result == SkillResult(ok=False, text=message, error=message)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError("boom"), OSError("boom")])
async def test_an_agent_runtime_failure_becomes_a_failed_result(error):
    pool = _Pool(orchestrator=_Agent(raises=error))
    result = await SkillExecutor(pool, _settings()).execute(_skill())
    assert result == SkillResult(
        ok=False, text="Error executing skill 'deploy': boom", error="boom"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TypeError("bug"), AttributeError("bug")])
async def test_a_programming_error_in_the_agent_propagates(error):
    pool = _Pool(orchestrator=_Agent(raises=error))
    with pytest.raises(type(error)):
        await SkillExecutor(pool, _settings()).execute(_skill())


@pytest.mark.asyncio
async def test_the_success_text_is_the_extracted_response_text():
    pool = _Pool(orchestrator=_Agent(response="plain string answer"))
    result = await SkillExecutor(pool, _settings()).execute(_skill())
    assert result.text == "plain string answer"


def test_the_skill_package_exports_its_public_names():
    import igni_core.core.skills as skills
    from igni_core.core.skills.loader import SkillEntry, SkillPool

    assert sorted(skills.__all__) == [
        "SkillDefinition",
        "SkillEntry",
        "SkillExecutor",
        "SkillParser",
        "SkillPool",
        "SkillResult",
    ]
    assert skills.SkillPool is SkillPool
    assert skills.SkillEntry is SkillEntry
    assert skills.SkillExecutor is SkillExecutor
    assert skills.SkillResult is SkillResult


def test_the_skill_result_fields_are_described_for_the_published_schema():
    props = SkillResult.model_json_schema()["properties"]
    assert set(props) == {"ok", "text", "error"}
    assert all(p.get("description") for p in props.values())
