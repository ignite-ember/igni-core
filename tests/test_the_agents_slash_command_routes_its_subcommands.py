"""``/agents``: which subcommand runs, how its arguments are read, what it says.

A real ``AgentPool`` with its real ephemeral store on a tmp project; only
the construction of the model-backed agent is replaced, as in the
neighbouring ephemeral-agent tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from igni_core.backend.cmd_agents import AgentsCommand, cmd_agents
from igni_core.core.agents import AgentPool
from igni_core.core.paths import CONFIG_DIR
from igni_core.protocol.messages import CommandAction, CommandResultKind

USAGE = AgentsCommand.PROMOTE_USAGE


@pytest.fixture(autouse=True)
def _no_model_build():
    with patch("igni_core.core.agents.builder.AgentBuilder.build", return_value=MagicMock()):
        yield


@pytest.fixture
def session(tmp_path, settings):
    pool = AgentPool(settings=settings, project_dir=tmp_path)
    pool.init_ephemeral(tmp_path)
    pool.ephemeral.register(name="helper", description="a helper", system_prompt="Help.")
    return SimpleNamespace(pool=pool, project_dir=tmp_path)


def _agents_dir(tmp_path, where="agents"):
    return tmp_path / CONFIG_DIR / where


@pytest.mark.parametrize("args", ["", "   ", "bogus", "bogus helper", "list"])
async def test_no_or_unknown_subcommand_opens_the_agents_panel(session, args):
    result = await AgentsCommand(session).dispatch(args)

    assert result.kind == CommandResultKind.ACTION
    assert result.action == CommandAction.AGENTS


async def test_the_subcommand_is_case_insensitive(session):
    result = await AgentsCommand(session).dispatch("EPHEMERAL")

    assert "helper" in result.content


async def test_promote_by_default_goes_to_the_project(session, tmp_path):
    result = await AgentsCommand(session).dispatch("promote helper")

    dest = _agents_dir(tmp_path) / "helper.md"
    assert result.kind == CommandResultKind.INFO
    assert result.content == f"Promoted 'helper' to {dest}"
    assert dest.exists()


@pytest.mark.parametrize("args", ["promote --personal helper", "promote helper --personal"])
async def test_personal_goes_to_agents_local_with_the_flag_on_either_side(session, tmp_path, args):
    result = await AgentsCommand(session).dispatch(args)

    assert (
        result.content == f"Promoted 'helper' to {_agents_dir(tmp_path, 'agents.local')}/helper.md"
    )
    assert (_agents_dir(tmp_path, "agents.local") / "helper.md").exists()


@pytest.mark.parametrize(
    "args",
    [
        "promote",
        "promote   ",
        "promote one two",
        "promote helper --project --personal",
        "promote helper --team",
        "promote --personal",
    ],
)
async def test_a_malformed_promote_gets_the_usage_and_promotes_nothing(session, tmp_path, args):
    result = await AgentsCommand(session).dispatch(args)

    assert result.kind == CommandResultKind.ERROR
    assert result.content == USAGE
    assert not _agents_dir(tmp_path).exists()
    assert session.pool.ephemeral.count == 1


async def test_promoting_an_agent_that_is_not_there_is_an_error_with_the_reason(session):
    result = await AgentsCommand(session).dispatch("promote ghost")

    assert result.kind == CommandResultKind.ERROR
    assert "not found" in result.content


async def test_discard_removes_the_agent_and_says_so(session, tmp_path):
    result = await AgentsCommand(session).dispatch("discard helper")

    assert result.kind == CommandResultKind.INFO
    assert result.content == "Discarded ephemeral agent 'helper'."
    assert not (_agents_dir(tmp_path, "agents.tmp") / "helper.md").exists()
    assert session.pool.ephemeral.count == 0


async def test_discard_with_no_name_is_a_usage_error(session):
    result = await AgentsCommand(session).dispatch("discard")

    assert result.kind == CommandResultKind.ERROR
    assert result.content == "Usage: /agents discard <name>"
    assert session.pool.ephemeral.count == 1


async def test_discarding_an_unknown_agent_is_an_error_with_the_reason(session):
    result = await AgentsCommand(session).dispatch("discard ghost")

    assert result.kind == CommandResultKind.ERROR
    assert "ghost" in result.content


async def test_a_pool_with_no_ephemeral_store_lists_none_and_refuses_the_rest(tmp_path, settings):
    session = SimpleNamespace(
        pool=AgentPool(settings=settings, project_dir=tmp_path), project_dir=tmp_path
    )
    command = AgentsCommand(session)

    listed = await command.dispatch("ephemeral")
    promoted = await command.dispatch("promote helper")
    discarded = await command.dispatch("discard helper")

    assert (listed.kind, listed.content) == (CommandResultKind.INFO, "No ephemeral agents.")
    assert promoted.kind == CommandResultKind.ERROR and "not initialized" in promoted.content
    assert discarded.kind == CommandResultKind.ERROR and "not initialized" in discarded.content


async def test_the_shim_runs_the_command_for_the_handlers_session(session):
    result = await cmd_agents(SimpleNamespace(session=session), "ephemeral")

    assert "helper" in result.content and "a helper" in result.content
