"""Every session's RPC table carries its own workflow runner.

The runner used to be attached to the boot ``BackendServer`` alone,
after the ready line had gone out. Every runtime the pool created
later answered ``list_workflows`` with nothing and ``run_workflow`` /
``cancel_workflow`` with "workflow_runner not initialised". Each
``RpcRouter`` now builds a runner rooted at its own backend's project.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from igni_core.backend.rpc_router import RpcRouter
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.protocol.rpc import RpcMethod


def _table(project_dir: Path) -> dict:
    backend = SimpleNamespace(
        project_dir=project_dir,
        session=MagicMock(),
        supervisor=ProcessSupervisor(),
        # Whether this backend process may run agent sessions. The real
        # ``BackendServer`` carries one; the RPC router reads it to build
        # the workflow runner. This one is open, so a run starts.
        gate=RunGate(),
    )
    router = RpcRouter(backend=backend, login=MagicMock(), push=MagicMock())
    return router.build_table()


async def test_each_router_runs_workflows_from_its_own_project(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    for project in (first, second):
        run = _table(project)[RpcMethod.RUN_WORKFLOW]
        with pytest.raises(FileNotFoundError) as caught:
            await run({"name": "no-such-workflow"})
        assert str(project / CONFIG_DIR / "workflows") in str(caught.value)


async def test_a_fresh_router_can_cancel_without_a_boot_step(tmp_path: Path) -> None:
    cancel = _table(tmp_path)[RpcMethod.CANCEL_WORKFLOW]

    out = await cancel({"workflow_run_id": "no-such-run"})

    assert out.cancelled is False
