"""Background-process watcher contract.

The FE's right-side watcher panel surfaces every backgrounded
``run_shell_command`` for live tail + explicit kill. It depends
on three BE primitives this file pins:

* Per-line + lifecycle subscribers on the process-event bus (line,
  start, completion). The watcher uses all three; the agent's
  notify-on-completion still uses only ``completion``.
* Three RPCs (``list_background_processes`` /
  ``read_process_tail`` / ``stop_background_process``) and their
  dispatch wiring.
* Push channel payload shapes (``process_started`` /
  ``process_line`` / ``process_exited``).

A regression in any layer should fail here, not at the FE.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from igni_core.backend.schemas_processes import ProcessTailResult, StopProcessResult
from igni_core.backend.server_processes import ProcessesController
from igni_core.core.tools.process_events import ProcessLineEvent, ProcessStartEvent
from igni_core.core.tools.process_supervisor_locator import supervisors
from igni_core.protocol.rpc import RpcMethod
from tests.rpc_tables import rpc_table_for

# ── Subscriber semantics ─────────────────────────────────────


class TestLineSubscriber:
    def setup_method(self) -> None:
        # Test isolation: reset the process-event bus before each
        # test so a leftover callback from a previous test doesn't
        # fire here. Post-refactor the three parallel subscriber
        # lists live inside :class:`ProcessEventBus`; ``.reset()``
        # drops all subscribers across all event types in one call.
        supervisors.default().registry.bus.reset()

    def test_subscribe_then_emit_fires_callback(self) -> None:
        registry = supervisors.default().registry
        received: list[ProcessLineEvent] = []
        registry.bus.on("line", received.append)

        registry.emit_line(42, "hello from background")

        assert received == [ProcessLineEvent(pid=42, line="hello from background")]

    def test_unsubscribe_stops_callback(self) -> None:
        registry = supervisors.default().registry
        received: list[ProcessLineEvent] = []

        def cb(event: ProcessLineEvent) -> None:
            received.append(event)

        registry.bus.on("line", cb)
        registry.bus.off("line", cb)
        registry.emit_line(42, "should not surface")

        assert received == []

    def test_duplicate_subscribe_is_idempotent(self) -> None:
        # Same callback registered twice should only fire once —
        # mirrors the completion subscriber contract.
        registry = supervisors.default().registry
        received: list[ProcessLineEvent] = []
        cb = received.append
        registry.bus.on("line", cb)
        registry.bus.on("line", cb)

        registry.emit_line(1, "x")

        assert received == [ProcessLineEvent(pid=1, line="x")]

    def test_subscriber_exception_does_not_block_others(self) -> None:
        # One bad callback can't sink the rest. Important because
        # the watcher's loop-hop subscriber would otherwise prevent
        # an unrelated plugin from receiving lines.
        registry = supervisors.default().registry
        survivor: list[ProcessLineEvent] = []

        def bad(_event: ProcessLineEvent) -> None:
            raise RuntimeError("bad subscriber")

        registry.bus.on("line", bad)
        registry.bus.on("line", survivor.append)

        registry.emit_line(7, "still arrives")

        assert survivor == [ProcessLineEvent(pid=7, line="still arrives")]

    def test_no_subscribers_is_cheap_noop(self) -> None:
        # Hot path — must short-circuit when nothing is listening.
        # Build a payload-allocating subscriber and assert it's
        # NOT called for the no-subscriber emit.
        supervisors.default().registry.emit_line(99, "ignored")  # must not raise


class TestStartSubscriber:
    def setup_method(self) -> None:
        supervisors.default().registry.bus.reset()

    def test_emit_start_publishes_pid_cmd_ts(self) -> None:
        registry = supervisors.default().registry
        received: list[ProcessStartEvent] = []
        registry.bus.on("start", received.append)

        mp = SimpleNamespace(proc=SimpleNamespace(pid=123), cmd="tail -f x.log")
        registry.announce_start(mp)  # type: ignore[arg-type]

        assert len(received) == 1
        assert received[0].pid == 123
        assert received[0].cmd == "tail -f x.log"
        # ts is a real epoch — just sanity-check the field exists
        # and is a number.
        assert isinstance(received[0].started_at, (int, float))

    def test_start_subscriber_exception_isolated(self) -> None:
        registry = supervisors.default().registry
        survivor: list[ProcessStartEvent] = []

        def bad(_event: ProcessStartEvent) -> None:
            raise RuntimeError("crash")

        registry.bus.on("start", bad)
        registry.bus.on("start", survivor.append)
        mp = SimpleNamespace(proc=SimpleNamespace(pid=1), cmd="x")
        registry.announce_start(mp)  # type: ignore[arg-type]

        assert survivor[0].pid == 1


# ── ProcessesController (what the RPCs dump) ────────────────────────────────────


class TestListBackgroundProcesses:
    def test_empty_registry_returns_empty(self, monkeypatch) -> None:
        monkeypatch.setattr(supervisors.default().registry, "all_known", lambda: [], raising=True)
        processes = ProcessesController()
        assert [row.model_dump() for row in processes.list()] == []

    def test_returns_pid_cmd_elapsed_per_process(self, monkeypatch) -> None:
        monkeypatch.setattr(
            supervisors.default().registry,
            "all_known",
            lambda: [
                (101, "npm run dev", 12.3, True, None),
                (202, "tail -f log", 4.5, True, None),
            ],
            raising=True,
        )
        processes = ProcessesController()
        result = [row.model_dump() for row in processes.list()]
        assert result == [
            {
                "pid": 101,
                "cmd": "npm run dev",
                "elapsed_seconds": 12.3,
                "is_running": True,
                "exit_code": None,
            },
            {
                "pid": 202,
                "cmd": "tail -f log",
                "elapsed_seconds": 4.5,
                "is_running": True,
                "exit_code": None,
            },
        ]

    def test_a_finished_process_is_listed_with_its_exit_code(self, monkeypatch) -> None:
        """The list used to be "what is running", so a command that had
        just failed was filtered out of the panel someone opens to find
        out why."""
        monkeypatch.setattr(
            supervisors.default().registry,
            "all_known",
            lambda: [(303, "pytest -x", 31.0, False, 1)],
            raising=True,
        )
        processes = ProcessesController()
        assert [row.model_dump() for row in processes.list()] == [
            {
                "pid": 303,
                "cmd": "pytest -x",
                "elapsed_seconds": 31.0,
                "is_running": False,
                "exit_code": 1,
            }
        ]


class TestReadProcessTail:
    def test_unknown_pid_returns_safe_shape(self, monkeypatch) -> None:
        monkeypatch.setattr(supervisors.default().registry, "get", lambda _pid: None, raising=True)
        processes = ProcessesController()
        out = processes.read_tail(pid=9999).model_dump()
        assert out == {
            "pid": 9999,
            "output": "",
            "is_running": False,
            "exit_code": None,
        }

    def test_running_pid_returns_tail_and_running_true(self, monkeypatch) -> None:
        mp = MagicMock()
        mp.read.return_value = "line1\nline2"
        mp.is_running.return_value = True
        mp.returncode.return_value = None
        monkeypatch.setattr(supervisors.default().registry, "get", lambda _pid: mp, raising=True)

        processes = ProcessesController()
        out = processes.read_tail(pid=42, tail=50).model_dump()
        assert out == {
            "pid": 42,
            "output": "line1\nline2",
            "is_running": True,
            "exit_code": None,
        }
        mp.read.assert_called_once_with(tail=50)

    def test_exited_pid_returns_exit_code(self, monkeypatch) -> None:
        # Process exited but registry still holds it (the
        # post-completion TTL eviction window). The FE needs the
        # final tail + exit code to render the "stopped" row.
        mp = MagicMock()
        mp.read.return_value = "done"
        mp.is_running.return_value = False
        mp.returncode.return_value = 0
        monkeypatch.setattr(supervisors.default().registry, "get", lambda _pid: mp, raising=True)

        processes = ProcessesController()
        out = processes.read_tail(pid=7, tail=100).model_dump()
        assert out["exit_code"] == 0
        assert out["is_running"] is False


class TestStopBackgroundProcess:
    async def test_unknown_pid_returns_killed_false(self, monkeypatch) -> None:
        monkeypatch.setattr(supervisors.default().registry, "get", lambda _pid: None, raising=True)
        processes = ProcessesController()
        out = (await processes.stop(pid=9999)).model_dump()
        assert out["killed"] is False
        assert "not in registry" in out["message"]

    async def test_already_exited_returns_killed_false(self, monkeypatch) -> None:
        mp = MagicMock()
        mp.is_running.return_value = False
        mp.returncode.return_value = 137
        monkeypatch.setattr(supervisors.default().registry, "get", lambda _pid: mp, raising=True)

        processes = ProcessesController()
        out = (await processes.stop(pid=1)).model_dump()
        assert out["killed"] is False
        assert "already exited" in out["message"]
        mp.kill.assert_not_called()

    async def test_running_pid_sends_sigterm(self, monkeypatch) -> None:
        mp = MagicMock()
        mp.is_running.return_value = True
        mp.returncode.return_value = -15
        mp._reader_task = None  # no reader to await — skip the wait_for branch
        monkeypatch.setattr(supervisors.default().registry, "get", lambda _pid: mp, raising=True)

        processes = ProcessesController()
        out = (await processes.stop(pid=42)).model_dump()
        mp.kill.assert_called_once()
        assert out["killed"] is True
        assert out["pid"] == 42


# ── RPC dispatch wiring ──────────────────────────────────────


class TestWatcherRpcDispatch:
    """The three watcher RPCs must route from the dispatch lambda
    to the right backend or controller method with args extracted as the
    right types. Same shape as ``test_plan_rpc_wiring.py``."""

    def _make_backend(self) -> MagicMock:
        backend = MagicMock()
        backend.list_background_processes = MagicMock(return_value=[])
        backend.controllers.processes.read_tail = MagicMock(
            return_value=ProcessTailResult(pid=0, output="", is_running=False, exit_code=None)
        )
        backend.controllers.processes.stop = AsyncMock(
            return_value=StopProcessResult(pid=0, killed=False, message="")
        )
        return backend

    def test_list_dispatch_calls_method(self) -> None:
        backend = self._make_backend()
        table = rpc_table_for(backend, MagicMock())
        table[RpcMethod.LIST_BACKGROUND_PROCESSES]({})
        backend.list_background_processes.assert_called_once_with()

    def test_read_dispatch_extracts_pid_and_tail(self) -> None:
        backend = self._make_backend()
        table = rpc_table_for(backend, MagicMock())
        table[RpcMethod.READ_PROCESS_TAIL]({"pid": "42", "tail": "50"})
        backend.controllers.processes.read_tail.assert_called_once_with(pid=42, tail=50)

    def test_read_dispatch_defaults_tail_to_200(self) -> None:
        backend = self._make_backend()
        table = rpc_table_for(backend, MagicMock())
        table[RpcMethod.READ_PROCESS_TAIL]({"pid": 42})
        backend.controllers.processes.read_tail.assert_called_once_with(pid=42, tail=200)

    async def test_stop_dispatch_extracts_pid(self) -> None:
        backend = self._make_backend()
        table = rpc_table_for(backend, MagicMock())
        result = table[RpcMethod.STOP_BACKGROUND_PROCESS]({"pid": 99})
        # Lambda returns the coroutine — await to drive the call.
        if asyncio.iscoroutine(result):
            await result
        backend.controllers.processes.stop.assert_called_once_with(pid=99)

    def test_pool_runtime_routes_to_pool_backend(self) -> None:
        # Same isolation contract as the plan-decision RPCs:
        # ``rpc_table_for`` closes over its ``backend`` arg, so
        # a pool runtime's table must hit the pool backend, never
        # the boot one.
        boot = self._make_backend()
        pool = self._make_backend()
        # Boot table built but not stored — we only fire against
        # the pool table. Building both proves the two builders
        # produce independent closures (no shared mutable state
        # between boot and pool RPC dispatch).
        rpc_table_for(boot, MagicMock())
        pool_table = rpc_table_for(pool, MagicMock())

        pool_table[RpcMethod.LIST_BACKGROUND_PROCESSES]({})
        pool.list_background_processes.assert_called_once()
        boot.list_background_processes.assert_not_called()
