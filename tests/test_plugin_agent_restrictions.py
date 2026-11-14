"""Tests for the plugin-agent security envelope (row 37) — CC's
"plugin-shipped agents can't declare hooks, mcpServers, or
permissionMode; isolation=worktree is forced."

Three layers covered:

* ``_apply_plugin_restrictions`` — the pure helper that strips
  restricted fields and warns on detected restricted frontmatter
  keys.
* ``AgentPool.load_plugin_directory`` — the loader path that
  applies the helper, plus a regression test that
  ``AgentPool.load_directory`` (user / project agents) keeps
  existing behaviour.
* ``OrchestrateTools.spawn_agent`` — the spawn path that honours
  ``force_isolation`` even when the caller didn't request it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.core.agents import (
    AgentDefinition,
    AgentMarkdownFile,
    AgentPool,
    PluginRestrictionPolicy,
)
from igni_core.core.prompts import PromptFragments, PromptLoader
from igni_core.core.tools.orchestrate import OrchestrateTools


def _prompt_loader() -> PromptLoader:
    """igni's own prompt files, with no extension-supplied anchors."""
    return PromptLoader(PromptFragments())


def _apply_plugin_restrictions(
    definition: AgentDefinition,
    raw_keys: set,
    plugin_name: str = "",
) -> AgentDefinition:
    """Local test helper — replaces the deleted free function that
    used to live on ``core/pool.py``. Delegates to the canonical
    :meth:`PluginRestrictionPolicy.apply`."""
    return PluginRestrictionPolicy().apply(definition, raw_keys, plugin_name)


def _raw_frontmatter_keys(path: Path) -> set:
    """Local test helper — replaces the deleted free function on
    ``core/pool.py``. Delegates to
    :meth:`AgentMarkdownFile.raw_frontmatter_keys`."""
    return AgentMarkdownFile(path, prompt_loader=_prompt_loader()).raw_frontmatter_keys()


@pytest.fixture
def captured_pool_warnings(monkeypatch):
    """Spy on the canonical plugin-policy logger's ``warning``
    method and yield the list of captured messages.

    Why not pytest ``caplog``: when the full suite runs, an earlier
    test imports a dependency (chromadb) whose telemetry layer
    reconfigures logging in a way that makes ``caplog`` flaky for
    our module logger. Patching the bound method on the logger
    instance is immune to that — we own the recording surface
    regardless of what other test code did to the global logger
    config.

    Patches ``plugin_policy.logger`` directly — the emitter
    :class:`PluginRestrictionPolicy` writes through.
    """
    from igni_core.core.agents import plugin_policy as _plugin_policy_mod

    messages: list[str] = []
    real_warning = _plugin_policy_mod.logger.warning

    def _capture(fmt: str, *args: object, **kwargs: object) -> None:
        try:
            messages.append(fmt % args if args else fmt)
        except (TypeError, ValueError):
            messages.append(str(fmt))
        # Still call the real logger so any other instrumentation
        # (file handler, audit pipe) keeps seeing the events.
        real_warning(fmt, *args, **kwargs)

    monkeypatch.setattr(_plugin_policy_mod.logger, "warning", _capture)
    yield messages


def _write_agent(path: Path, name: str, extra_frontmatter: str = "") -> Path:
    """Write a minimal agent .md file with ``extra_frontmatter``
    appended into the YAML block. Returns the file path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"---\nname: {name}\ndescription: test agent\n{extra_frontmatter}---\nSystem prompt body.\n"
    )
    file_path = path / f"{name}.md"
    file_path.write_text(body)
    return file_path


# ── _apply_plugin_restrictions ──────────────────────────────


class TestApplyPluginRestrictions:
    def test_strips_mcp_servers(self):
        """The agent declared ``mcp_servers`` in its frontmatter
        — the security envelope drops them."""
        defn = AgentDefinition(
            name="bad",
            description="d",
            mcp_servers=["should-be-stripped"],
        )
        restricted = _apply_plugin_restrictions(defn, raw_keys=set())
        assert restricted.mcp_servers == []

    def test_sets_force_isolation_to_worktree(self):
        """Every plugin agent gets per-spawn worktree isolation
        regardless of what the caller asks for."""
        defn = AgentDefinition(name="x", description="d")
        restricted = _apply_plugin_restrictions(defn, raw_keys=set())
        assert restricted.force_isolation == "worktree"

    def test_warns_on_restricted_keys(self, captured_pool_warnings):
        """Restricted keys present in the raw frontmatter trip a
        WARNING so plugin authors see the policy violation and
        a security audit can spot escalation attempts in logs."""
        defn = AgentDefinition(name="bad", description="d")
        _apply_plugin_restrictions(
            defn,
            raw_keys={"hooks", "permissionMode", "name", "description"},
            plugin_name="some-plugin",
        )
        assert any("restricted frontmatter keys" in m for m in captured_pool_warnings)
        joined = " ".join(captured_pool_warnings)
        assert "hooks" in joined
        assert "permissionMode" in joined

    def test_silent_when_no_restricted_keys(self, captured_pool_warnings):
        """A clean plugin agent shouldn't generate any warnings —
        keeps the log signal-to-noise high."""
        defn = AgentDefinition(name="good", description="d")
        _apply_plugin_restrictions(defn, raw_keys={"name", "description"})
        assert not any("restricted frontmatter keys" in m for m in captured_pool_warnings)

    def test_does_not_mutate_input(self):
        """The helper returns a new model — the original
        definition stays usable elsewhere (defence-in-depth: if
        we ever cache the original, the cache doesn't get
        corrupted)."""
        defn = AgentDefinition(name="x", description="d", mcp_servers=["original-server"])
        _apply_plugin_restrictions(defn, raw_keys=set())
        assert defn.mcp_servers == ["original-server"]
        assert defn.force_isolation is None


# ── _raw_frontmatter_keys ───────────────────────────────────


class TestRawFrontmatterKeys:
    def test_returns_keys_for_valid_file(self, tmp_path):
        file_path = _write_agent(
            tmp_path,
            "x",
            extra_frontmatter="hooks:\n  - PreToolUse: x\nmcp_servers: []\n",
        )
        keys = _raw_frontmatter_keys(file_path)
        assert "hooks" in keys
        assert "mcp_servers" in keys
        assert "name" in keys
        assert "description" in keys

    def test_returns_empty_for_missing_file(self, tmp_path):
        assert _raw_frontmatter_keys(tmp_path / "nope.md") == set()

    def test_returns_empty_for_no_frontmatter(self, tmp_path):
        f = tmp_path / "no_fm.md"
        f.write_text("Just a body, no header.\n")
        assert _raw_frontmatter_keys(f) == set()


# ── AgentPool plugin-restricted load ──────


class TestAgentPoolPluginRestrictedLoad:
    def test_plugin_agent_mcp_servers_stripped(self, tmp_path, captured_pool_warnings):
        """End-to-end: a plugin agent declaring mcp_servers gets
        them stripped at load time."""
        _write_agent(
            tmp_path,
            "bad",
            extra_frontmatter="mcp_servers:\n  - smuggled\n",
        )
        pool = AgentPool()
        pool.load_plugin_directory(
            tmp_path,
            priority=1,
            namespace="evil-plugin",
            restriction_policy=PluginRestrictionPolicy.strict(),
        )
        defn = pool.get_entry("evil-plugin:bad").definition
        assert defn.mcp_servers == []
        assert defn.force_isolation == "worktree"
        # And the warning fired — visible in logs for audit.
        assert any("restricted frontmatter keys" in m for m in captured_pool_warnings)

    def test_plugin_agent_with_hooks_warns_and_proceeds(self, tmp_path, captured_pool_warnings):
        """``hooks`` isn't a field on ``AgentDefinition``, so it's
        already silently dropped by ``parse_agent_file``. The
        plugin-restriction layer still WARNs about the attempt
        — that's the audit trail."""
        _write_agent(
            tmp_path,
            "p",
            extra_frontmatter='hooks:\n  PreToolUse:\n    - command: "echo"\n',
        )
        pool = AgentPool()
        pool.load_plugin_directory(
            tmp_path,
            priority=1,
            namespace="plug",
            restriction_policy=PluginRestrictionPolicy.strict(),
        )
        defn = pool.get_entry("plug:p").definition
        assert defn.force_isolation == "worktree"
        joined = " ".join(captured_pool_warnings)
        assert "hooks" in joined

    @pytest.mark.parametrize("priority", [6, 7])
    def test_a_managed_plugins_agent_loads_at_its_roots_priority(self, tmp_path, priority):
        """The managed plugin roots rank 6 and 7; 7 is no AgentPriority, and converting it
        raised out of Session construction for any managed plugin that ships agents/."""
        _write_agent(tmp_path, "helper")
        pool = AgentPool()
        pool.load_plugin_directory(tmp_path, priority=priority, namespace="it-plugin")
        entry = pool.get_entry("it-plugin:helper")
        assert entry.priority == priority
        assert entry.definition.force_isolation == "worktree"

    def test_the_same_plugin_agent_from_a_higher_root_wins(self, tmp_path):
        low, high = tmp_path / "low", tmp_path / "high"
        low.mkdir()
        high.mkdir()
        _write_agent(low, "helper")
        _write_agent(high, "helper")
        pool = AgentPool()
        pool.load_plugin_directory(high, priority=7, namespace="plug")
        pool.load_plugin_directory(low, priority=2, namespace="plug")
        assert pool.get_entry("plug:helper").priority == 7

    def test_non_plugin_load_preserves_mcp_servers(
        self, tmp_path, settings, captured_pool_warnings
    ):
        """The ``load_directory`` path (user / project agents) must
        NOT strip mcp_servers — only plugin agents get the security
        envelope."""
        _write_agent(
            tmp_path,
            "userp",
            extra_frontmatter="mcp_servers:\n  - my-server\n",
        )
        pool = AgentPool(settings=settings)
        pool.load_directory(tmp_path, priority=1)
        defn = pool.get_entry("userp").definition
        assert defn.mcp_servers == ["my-server"]
        assert defn.force_isolation is None
        # And NO warning fires for user / project agents.
        assert not any("restricted frontmatter keys" in m for m in captured_pool_warnings)


# ── spawn_agent honours force_isolation ────────────────────


def _mock_stream(content: str):
    """Minimal Agno event stream mock."""
    from agno.run import agent as ae

    async def stream():
        started = MagicMock(spec=ae.RunStartedEvent)
        started.run_id = "r1"
        started.agent_id = "a1"
        started.__class__ = ae.RunStartedEvent
        yield started
        event = MagicMock(spec=ae.RunContentEvent)
        event.content = content
        event.__class__ = ae.RunContentEvent
        yield event

    return stream()


def _mock_pool_with_definition(definition: AgentDefinition):
    pool = MagicMock()
    agent = MagicMock()
    agent.arun = MagicMock(return_value=_mock_stream("ok"))
    run_output = MagicMock()
    run_output.content = "ok"
    agent.aget_run_output = AsyncMock(return_value=run_output)
    agent.aget_last_run_output = AsyncMock(return_value=run_output)
    agent.tools = []
    pool.get.return_value = agent
    pool.get_entry.return_value = SimpleNamespace(definition=definition)
    return pool


def _settings():
    s = MagicMock()
    s.orchestration.max_nesting_depth = 5
    s.orchestration.max_total_agents = 20
    s.orchestration.sub_team_timeout = 600
    s.orchestration.max_task_iterations = 10
    return s


class TestSpawnAgentForceIsolation:
    @pytest.mark.asyncio
    async def test_plugin_agent_triggers_worktree_creation(self, tmp_path):
        """Caller didn't pass ``isolation=`` — but the agent
        definition has ``force_isolation="worktree"``, so spawn
        proceeds as if the caller had asked. With no
        ``project_dir`` configured the worktree creation fails
        with the same error path as ``isolation="worktree"``;
        that's the proof the override fired."""
        defn = AgentDefinition(
            name="plug-agent",
            description="plugin-shipped",
            force_isolation="worktree",
        )
        pool = _mock_pool_with_definition(defn)
        tool = OrchestrateTools(pool=pool, settings=_settings(), project_dir=None)
        result = await tool.spawn_agent("do work", "plug-agent")
        # The caller did NOT ask for isolation; the agent forced
        # it; the missing-project-dir error path is what we should
        # land in.
        assert "requires a project" in result

    @pytest.mark.asyncio
    async def test_non_plugin_agent_respects_caller_no_isolation(self, tmp_path):
        """A non-plugin agent's ``force_isolation is None`` —
        spawn obeys the (empty) caller arg and runs without
        isolation. Regression guard so we don't accidentally
        worktree every spawn."""
        defn = AgentDefinition(
            name="user-agent",
            description="user-defined",
            force_isolation=None,
        )
        pool = _mock_pool_with_definition(defn)
        tool = OrchestrateTools(pool=pool, settings=_settings(), project_dir=tmp_path)
        result = await tool.spawn_agent("do work", "user-agent")
        # No worktree footer in the response — non-isolated spawn.
        assert "Worktree" not in result
