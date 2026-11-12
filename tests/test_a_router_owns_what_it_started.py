"""What one ``RpcRouter`` owns: its workflow runs, and its own file index.

Every session has its own router, so a session's live workflow runs keep
it busy and are stopped with it, and the ``@`` completion of one session
never lists the files of another's project.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from igni_core.backend.rpc_handlers import McpRpcHandler
from igni_core.backend.rpc_router import RPC_HANDLERS, RpcRouter
from igni_core.backend.schemas_workflows import WorkflowRunPhase
from igni_core.protocol.rpc import RpcMethod
from tests.test_workflow_run_registry import _Process, _run, _settle


def _router(project) -> RpcRouter:
    backend = MagicMock()
    backend.project_dir = project
    return RpcRouter(backend=backend, login=MagicMock(), push=MagicMock())


def _admit(router: RpcRouter, proc: _Process, run_id: str):
    run = _run(proc, run_id)
    router._workflows._registry.admit(run)
    return run


class TestLiveWorkflows:
    def test_a_fresh_router_has_none(self, tmp_path):
        assert _router(tmp_path).has_live_workflows() is False

    async def test_a_run_in_flight_is_live_until_it_ends(self, tmp_path):
        router = _router(tmp_path)
        proc = _Process()
        _admit(router, proc, "wf_a")

        assert router.has_live_workflows() is True

        proc.exit(0)
        await _settle(router._workflows._registry, "wf_a")
        assert router.has_live_workflows() is False

    async def test_closing_stops_every_live_run(self, tmp_path):
        router = _router(tmp_path)
        procs = [_Process(), _Process()]
        runs = [_admit(router, proc, f"wf_{i}") for i, proc in enumerate(procs)]

        await router.close()

        assert router.has_live_workflows() is False
        assert all(run.phase is WorkflowRunPhase.CANCELLED for run in runs)

    async def test_closing_with_nothing_live_is_fine_and_can_be_repeated(self, tmp_path):
        router = _router(tmp_path)

        await router.close()
        await router.close()

        assert router.has_live_workflows() is False

    async def test_one_routers_runs_are_not_anothers(self, tmp_path):
        first, second = _router(tmp_path), _router(tmp_path)
        proc = _Process()
        _admit(first, proc, "wf_a")

        assert second.has_live_workflows() is False
        await first.close()


class TestEachRouterHasItsOwnFileIndex:
    async def test_two_routers_list_only_their_own_projects_files(self, tmp_path):
        (tmp_path / "one").mkdir()
        (tmp_path / "two").mkdir()
        (tmp_path / "one" / "only_in_one.txt").write_text("")
        (tmp_path / "two" / "only_in_two.txt").write_text("")
        one = _router(tmp_path / "one").build_table()
        two = _router(tmp_path / "two").build_table()

        in_one = await one[RpcMethod.COMPLETE_FILES]({"query": "only"})
        in_two = await two[RpcMethod.COMPLETE_FILES]({"query": "only"})

        assert in_one.matches == ["only_in_one.txt"]
        assert in_two.matches == ["only_in_two.txt"]


class TestTheTable:
    def test_a_handler_left_out_fails_the_build_not_the_first_call(self, tmp_path):
        router = _router(tmp_path)
        router._handlers = [h for h in router._handlers if not isinstance(h, McpRpcHandler)]

        with pytest.raises(RuntimeError):
            router.build_table()

    def test_every_handler_class_is_built_once(self, tmp_path):
        router = _router(tmp_path)

        assert [type(h) for h in router._handlers] == list(RPC_HANDLERS)
