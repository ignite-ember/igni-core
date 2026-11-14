"""The plugin envelope's warning: which keys count, and how it is worded.

A plugin-shipped agent may not declare hooks, MCP servers or permissions of
its own. The policy always strips the MCP servers and forces a worktree; when
the file *declared* any restricted key it also leaves one warning naming the
keys (sorted) and the agent, plugin-qualified.
"""

from __future__ import annotations

import pytest

from igni_core.core.agents import plugin_policy
from igni_core.core.agents.plugin_policy import PluginRestrictionPolicy
from igni_core.core.agents.schemas import AgentDefinition


@pytest.fixture
def warnings(monkeypatch) -> list[str]:
    """The warnings the policy logs, rendered. (``caplog`` is unreliable once
    another test has reconfigured logging, so the logger's method is spied on.)"""
    seen: list[str] = []

    def record(fmt: str, *args: object, **kwargs: object) -> None:
        seen.append(fmt % args if args else fmt)

    monkeypatch.setattr(plugin_policy.logger, "warning", record)
    return seen


def _agent(**fields) -> AgentDefinition:
    return AgentDefinition(name="helper", description="Helps", **fields)


def test_the_restricted_keys_are_exactly_these() -> None:
    restricted = set(PluginRestrictionPolicy.RESTRICTED_KEYS)
    assert restricted == {
        "hooks",
        "mcpServers",
        "mcp_servers",
        "permissionMode",
        "permission_mode",
        "permissions",
    }


@pytest.mark.parametrize("key", sorted(PluginRestrictionPolicy.RESTRICTED_KEYS))
def test_each_restricted_key_alone_draws_a_warning(key, warnings) -> None:
    PluginRestrictionPolicy.strict().apply(_agent(), {"name", "description", key}, "acme")
    assert len(warnings) == 1
    assert f"['{key}']" in warnings[0]


def test_many_keys_make_one_warning_with_the_keys_sorted(warnings) -> None:
    PluginRestrictionPolicy().apply(_agent(), {"permissions", "hooks", "mcp_servers"}, "acme")
    assert len(warnings) == 1
    assert "['hooks', 'mcp_servers', 'permissions']" in warnings[0]


def test_the_agent_is_named_with_its_plugin(warnings) -> None:
    PluginRestrictionPolicy().apply(_agent(), {"hooks"}, "acme")
    assert "Plugin agent acme:helper declared" in warnings[0]


def test_without_a_plugin_name_the_bare_name_is_used(warnings) -> None:
    PluginRestrictionPolicy().apply(_agent(), {"hooks"})
    assert "Plugin agent helper declared" in warnings[0]


def test_key_case_variants_are_not_restricted(warnings) -> None:
    PluginRestrictionPolicy().apply(_agent(), {"Hooks", "MCPSERVERS", "tools"}, "acme")
    assert warnings == []


def test_no_keys_and_unrelated_keys_are_silent(warnings) -> None:
    PluginRestrictionPolicy().apply(_agent(), set(), "acme")
    PluginRestrictionPolicy().apply(_agent(), {"name", "tools"}, "acme")
    assert warnings == []


def test_an_agent_already_isolated_or_without_servers_comes_out_the_same_way() -> None:
    restricted = PluginRestrictionPolicy().apply(
        _agent(force_isolation="worktree", mcp_servers=[]), set(), "acme"
    )
    assert restricted.mcp_servers == []
    assert restricted.force_isolation == "worktree"


def test_the_servers_go_whatever_the_keys_say() -> None:
    original = _agent(mcp_servers=["db"])
    restricted = PluginRestrictionPolicy().apply(original, set(), "acme")
    assert restricted.mcp_servers == []
    assert restricted.force_isolation == "worktree"
    assert original.mcp_servers == ["db"]
    assert original.force_isolation is None
