"""A sandbox finalized twice reports its worktree once.

``AgentSpawn`` finalizes on the success path and again from its cleanup
hook. The manager answers the second call with ``noop`` (it owns no
worktree any more); that is not a failure and must not read as one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from igni_core.core.tools.orchestrate_modes import IsolationMode
from igni_core.core.tools.orchestrate_sandbox import SpawnSandbox


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)


def test_a_clean_worktree_is_reaped_and_a_second_finalize_says_nothing(tmp_path):
    """``AgentSpawn`` finalizes on the success path and again from
    its cleanup hook; the second call must not invent a footer."""
    _init_git_repo(tmp_path)
    sandbox = SpawnSandbox.create(
        project_dir=tmp_path,
        session_id="s1",
        agent=SimpleNamespace(tools=[]),
        agent_name="editor",
        isolation=IsolationMode.WORKTREE,
        task="do it",
    ).sandbox
    assert sandbox is not None and sandbox.info is not None
    path = sandbox.info.worktree_path

    first = sandbox.finalize()
    second = sandbox.finalize()

    assert "(clean) — reaped." in first
    assert not path.exists()
    assert second == ""
