"""Tests for ``OrchestrateTools.spawn_agent(isolation="worktree")``
— Claude Code's per-spawn worktree isolation parity (row 30).

Layered:

* Unit tests for the sandbox lifecycle (:meth:`SpawnSandbox.finalize`,
  :meth:`SpawnSandbox.rebind_tool_base_dirs` and
  :meth:`SpawnSandbox.create`).
* Integration tests that spin up a real git repo on ``tmp_path``
  and run ``spawn_agent`` with isolation enabled, asserting the
  worktree was created, the tool ``base_dir`` was rebased and
  restored, and the response carries the expected footer.

The sandbox owns all three steps now — the toolkit's private shims
are gone, so the unit tests call :class:`SpawnSandbox` directly
rather than through :class:`OrchestrateTools`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from igni_core.core.tools.orchestrate import OrchestrateTools
from igni_core.core.tools.orchestrate_modes import IsolationMode
from igni_core.core.tools.orchestrate_sandbox import SpawnSandbox

# ── Shared fixtures (mirrored from test_orchestrate.py) ──────


def _mock_stream(content: str):
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


def _mock_pool(content: str = "agent response", agent_tools: list | None = None):
    pool = MagicMock()
    agent = MagicMock()
    agent.arun = MagicMock(return_value=_mock_stream(content))
    run_output = MagicMock()
    run_output.content = content
    agent.aget_run_output = AsyncMock(return_value=run_output)
    agent.aget_last_run_output = AsyncMock(return_value=run_output)
    agent.tools = agent_tools if agent_tools is not None else []
    defn = MagicMock()
    defn.description = "Test agent"
    defn.tools = ["Read", "Write"]
    defn.force_isolation = None
    defn.can_orchestrate = False
    pool.get.return_value = agent
    pool.get_entry.return_value = SimpleNamespace(definition=defn)
    return pool


def _settings():
    s = MagicMock()
    s.orchestration.max_nesting_depth = 5
    s.orchestration.max_total_agents = 20
    s.orchestration.sub_team_timeout = 600
    s.orchestration.max_task_iterations = 10
    return s


def _init_git_repo(path: Path) -> None:
    """Initialise an empty git repo with a committed file so
    ``git worktree add -b`` has a parent commit to fork from."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)


def _sandbox(
    *,
    project_dir: Path | None,
    agent: object,
    isolation: IsolationMode = IsolationMode.WORKTREE,
    task: str = "do the thing",
    session_id: str = "s1",
    agent_name: str = "editor",
):
    """One sandbox, set up the way a spawn sets one up."""
    return SpawnSandbox.create(
        project_dir=project_dir,
        session_id=session_id,
        agent=agent,
        agent_name=agent_name,
        isolation=isolation,
        task=task,
    )


# ── SpawnSandbox.finalize (per-spawn worktree lifecycle) ──────


class TestFinalizeWorktree:
    def test_no_worktree_returns_empty(self):
        """The hot path: spawn without isolation. Call should be
        cheap and produce no footer."""
        sandbox = SpawnSandbox(
            manager=None,
            info=None,
            original_base_dirs={},
            task="",
        )
        assert sandbox.finalize() == ""

    def test_restores_base_dirs_even_when_no_worktree(self):
        """A future caller might rebase tools without creating a
        worktree (custom isolation mode). :meth:`SpawnSandbox.finalize`
        must still restore them."""
        tool = MagicMock()
        tool.base_dir = Path("/changed")
        sandbox = SpawnSandbox(
            manager=None,
            info=None,
            original_base_dirs={tool: Path("/original")},
            task="",
        )
        sandbox.finalize()
        assert tool.base_dir == Path("/original")

    def test_cleanup_failure_surfaces_as_footer(self):
        """If ``manager.cleanup()`` raises, the footer reports
        the failure rather than letting the exception escape."""
        info = MagicMock(worktree_path=Path("/tmp/wt"), branch_name="b1")
        manager = MagicMock()
        manager.cleanup.side_effect = RuntimeError("boom")
        sandbox = SpawnSandbox(
            manager=manager,
            info=info,
            original_base_dirs={},
            task="",
        )
        footer = sandbox.finalize()
        assert "cleanup failed" in footer
        assert "boom" in footer


# ── SpawnSandbox.rebind_tool_base_dirs ────────────────────────


class _FakeTool:
    """Stand-in for a toolkit with a ``base_dir`` attribute. The
    real Agno Toolkits set this in __init__; bypassing the parent
    class avoids the registration machinery."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir


class TestRebindBaseDirs:
    def test_rebinds_each_tool_and_returns_originals(self):
        a = _FakeTool(Path("/orig/a"))
        b = _FakeTool(Path("/orig/b"))
        agent = MagicMock()
        agent.tools = [a, b]

        originals = SpawnSandbox.rebind_tool_base_dirs(agent, Path("/wt"))
        # All tools' base_dirs now point at the worktree.
        for tool in agent.tools:
            assert tool.base_dir == Path("/wt")
        # The map records the ORIGINAL paths so the caller can restore.
        assert set(originals.values()) == {Path("/orig/a"), Path("/orig/b")}
        # And the mapping uses the (shallow-copied) tools as keys.
        for tool in agent.tools:
            assert tool in originals

    def test_shallow_copies_tools_so_shared_instance_untouched(self):
        """The pool's cached agent shares its tool refs with
        every spawn — mutating those would race other spawns.
        After rebind, the agent's tool list contains DIFFERENT
        instances from the originals."""
        a = _FakeTool(Path("/orig/a"))
        agent = MagicMock()
        agent.tools = [a]

        SpawnSandbox.rebind_tool_base_dirs(agent, Path("/wt"))
        # Original tool's base_dir is unchanged.
        assert a.base_dir == Path("/orig/a")
        # But agent now points at a copy with the new base_dir.
        assert agent.tools[0] is not a
        assert agent.tools[0].base_dir == Path("/wt")

    def test_skips_tools_without_base_dir(self):
        """Toolkits without ``base_dir`` (MCP clients, etc.) are
        documented as "still see project root". The rebind must
        not crash on them; they simply aren't tracked in the
        returned originals dict."""
        good = _FakeTool(Path("/orig"))
        bare = MagicMock(spec=[])  # no attributes at all
        agent = MagicMock()
        agent.tools = [good, bare]

        originals = SpawnSandbox.rebind_tool_base_dirs(agent, Path("/wt"))
        assert len(originals) == 1
        # Tool that DID have base_dir is rebound.
        rebased_good = next(iter(originals))
        assert rebased_good.base_dir == Path("/wt")

    def test_no_tools_attribute_is_no_op(self):
        agent = MagicMock(spec=[])  # no .tools
        originals = SpawnSandbox.rebind_tool_base_dirs(agent, Path("/wt"))
        assert originals == {}


# ── SpawnSandbox.create (the worktree fork) ───────────────────


class TestCreateIsolatedWorktree:
    def test_returns_error_when_no_project_dir(self):
        agent = SimpleNamespace(tools=[])

        setup = _sandbox(project_dir=None, agent=agent)

        assert setup.sandbox is None
        assert "requires a project" in setup.error

    def test_returns_error_when_not_a_git_repo(self, tmp_path):
        agent = SimpleNamespace(tools=[])

        setup = _sandbox(project_dir=tmp_path, agent=agent)

        assert setup.sandbox is None
        assert "Error" in setup.error
        assert "Not a git repository" in setup.error or "cannot create worktree" in setup.error

    def test_creates_worktree_in_git_repo(self, tmp_path):
        _init_git_repo(tmp_path)
        agent = SimpleNamespace(tools=[])

        setup = _sandbox(project_dir=tmp_path, agent=agent)
        sandbox = setup.sandbox
        assert sandbox is not None
        assert sandbox.info is not None
        try:
            assert sandbox.info.worktree_path.is_dir()
            assert "editor" in sandbox.info.branch_name
        finally:
            sandbox.finalize()

    def test_no_isolation_is_a_no_op(self, tmp_path):
        """``isolation=NONE`` never forks a worktree — and never
        touches the agent's tools."""
        agent = SimpleNamespace(tools=[_FakeTool(Path("/orig"))])

        setup = _sandbox(project_dir=tmp_path, agent=agent, isolation=IsolationMode.NONE)

        assert setup.error is None
        assert setup.sandbox is not None
        assert setup.sandbox.manager is None
        assert setup.sandbox.info is None
        assert setup.sandbox.original_base_dirs == {}
        assert setup.task == "do the thing"
        assert agent.tools[0].base_dir == Path("/orig")

    def test_the_task_names_the_worktree(self, tmp_path):
        """The model has to know where it is: tools without a
        ``base_dir`` are not rebased, so the task text is the only
        thing telling it."""
        _init_git_repo(tmp_path)
        agent = SimpleNamespace(tools=[])

        setup = _sandbox(project_dir=tmp_path, agent=agent)
        sandbox = setup.sandbox
        assert sandbox is not None
        try:
            assert "isolated git worktree" in setup.task
            assert "do the thing" in setup.task
        finally:
            sandbox.finalize()


# ── End-to-end spawn_agent(isolation="worktree") ──────────────


class TestSpawnAgentIsolation:
    @pytest.mark.asyncio
    async def test_unknown_isolation_mode_returns_error(self, tmp_path):
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings(), project_dir=tmp_path)
        result = await tool.spawn_agent("task", "editor", isolation="docker")
        assert "Error" in result
        assert "isolation" in result.lower()

    @pytest.mark.asyncio
    async def test_isolation_without_project_dir_errors(self):
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings())
        result = await tool.spawn_agent("task", "editor", isolation="worktree")
        assert "Error" in result
        assert "project directory" in result

    @pytest.mark.asyncio
    async def test_isolation_non_git_repo_errors(self, tmp_path):
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings(), project_dir=tmp_path)
        result = await tool.spawn_agent("task", "editor", isolation="worktree")
        assert "Error" in result
        # The error should reference the underlying git failure so
        # the user / agent can act on it.
        assert "git" in result.lower() or "worktree" in result.lower()

    @pytest.mark.asyncio
    async def test_isolation_creates_and_reaps_clean_worktree(self, tmp_path):
        """Happy path: the agent doesn't touch the worktree, so
        ``manager.cleanup()`` reaps it and the footer says so."""
        _init_git_repo(tmp_path)
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings(), project_dir=tmp_path)
        result = await tool.spawn_agent("Fix bug", "editor", isolation="worktree")
        assert "agent response" in result
        assert "Worktree" in result
        assert "reaped" in result

    @pytest.mark.asyncio
    async def test_isolation_preserves_dirty_worktree(self, tmp_path):
        """When the spawned agent leaves changes in the worktree,
        the cleanup step preserves it and the footer surfaces the
        merge / remove commands. Simulated here by having the
        ``_mock_stream`` agent write a file via a fake tool that
        has ``base_dir`` honoring rebind."""
        _init_git_repo(tmp_path)

        # A tool that, when its method runs, writes a file into
        # its ``base_dir`` — so we can see the worktree change.
        class _WriterTool(_FakeTool):
            def write(self, payload: str) -> str:
                (self.base_dir / "dirt.txt").write_text(payload)
                return "ok"

        pool = _mock_pool(agent_tools=[_WriterTool(tmp_path)])
        tool = OrchestrateTools(pool=pool, settings=_settings(), project_dir=tmp_path)

        # We can't realistically have the mocked agent invoke its
        # tool, so write directly via the post-rebind tool copy
        # AFTER spawn_agent has rebound base_dirs. To exercise the
        # preserved-worktree path, write through the rebased tool
        # by patching the streamer the spawn runs through.
        async def _fake_run_agent_streaming(context):
            # Use the agent's (already-rebound) writer tool.
            writer = next(t for t in context.runnable.tools if hasattr(t, "write"))
            writer.write("hello from isolated agent")
            return "did it", []

        with patch(
            "igni_core.core.tools.orchestrate.run_agent_streaming",
            new=_fake_run_agent_streaming,
        ):
            result = await tool.spawn_agent("Write a file", "editor", isolation="worktree")

        assert "Worktree preserved" in result
        assert "git merge" in result
        # Manual cleanup so test artifacts don't pile up.
        branch_name = result.split("branch: ", 1)[1].split(")", 1)[0]
        subprocess.run(
            ["git", "worktree", "remove", "--force", branch_name],
            cwd=tmp_path,
            capture_output=True,
        )

    @pytest.mark.asyncio
    async def test_isolation_prepends_worktree_note_to_task(self, tmp_path):
        """The agent receives a task body that begins with a
        description of the worktree path / branch so the model
        knows where to operate even for tools that lack
        ``base_dir`` rebinding."""
        _init_git_repo(tmp_path)
        captured: dict = {}

        async def _capturing_run(context):
            captured["task"] = context.task
            return "ok", []

        with patch(
            "igni_core.core.tools.orchestrate.run_agent_streaming",
            new=_capturing_run,
        ):
            tool = OrchestrateTools(pool=_mock_pool(), settings=_settings(), project_dir=tmp_path)
            await tool.spawn_agent("Original task", "editor", isolation="worktree")

        body = captured["task"]
        assert "isolated git worktree" in body
        assert "Original task" in body  # original task is preserved verbatim

    @pytest.mark.asyncio
    async def test_isolation_restores_base_dirs_after_run(self, tmp_path):
        """The pool's shared tools must not be left pointing at
        the worktree after the spawn ends — the rebind happens on
        shallow-copied tool instances, and even those copies are
        restored to original ``base_dir`` so a follow-up spawn
        sees the right cwd."""
        _init_git_repo(tmp_path)
        original_path = tmp_path / "original"
        original_path.mkdir()
        writer = _FakeTool(original_path)
        pool = _mock_pool(agent_tools=[writer])

        # Keep a ref to the agent so we can inspect post-spawn.
        agents_seen: list = []

        async def _capture_agent(context):
            agents_seen.append(context.runnable)
            return "ok", []

        with patch(
            "igni_core.core.tools.orchestrate.run_agent_streaming",
            new=_capture_agent,
        ):
            tool = OrchestrateTools(pool=pool, settings=_settings(), project_dir=tmp_path)
            await tool.spawn_agent("task", "editor", isolation="worktree")

        spawned_agent = agents_seen[0]
        # The agent's tool copy was rebased to the worktree
        # DURING the run, but restored to the original AFTER.
        rebased_writer = spawned_agent.tools[0]
        assert rebased_writer.base_dir == original_path
        # And the pool's original writer was never mutated.
        assert writer.base_dir == original_path
        assert rebased_writer is not writer

    @pytest.mark.asyncio
    async def test_isolation_empty_string_is_no_op(self, tmp_path):
        """No isolation requested → no worktree, no footer.
        Validates the non-isolated regression path."""
        _init_git_repo(tmp_path)
        tool = OrchestrateTools(pool=_mock_pool(), settings=_settings(), project_dir=tmp_path)
        result = await tool.spawn_agent("Plain task", "editor")
        assert "agent response" in result
        assert "Worktree" not in result
