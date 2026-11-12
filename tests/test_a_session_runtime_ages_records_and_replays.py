"""A session runtime's lifetime, as the pool and the router use it.

Pinned, on the real :class:`SessionRuntime` and :class:`SessionPool` with
a fake backend and an injected clock:

* how long a runtime sat idle is what the eviction report says, and the
  idle test is strictly "before the cutoff";
* the directory record is keyed on the directory as text, so a ``Path``
  and a ``str`` of the same folder are one record;
* a pool takes its idle timeout from its config, and falls back to
  thirty minutes;
* a failing shutdown still drops the runtime and is reported, and the
  pool's own shutdown carries on past a runtime that fails;
* a factory that raises leaves the pool as it was and the lock free;
* what a window attaching is shown again, tagged by its request;
* the auto-name task is held until it ends;
* the pool no longer re-exports the stamping transport.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend import session_pool
from igni_core.backend.schemas_rpc import SessionPoolConfig
from igni_core.backend.session_pool import SessionPool, SessionRuntime
from igni_core.protocol import messages as msg


def _backend(session_id: str = "s") -> MagicMock:
    backend = MagicMock()
    backend.session_id = session_id
    backend.processing = False
    backend.shutdown = AsyncMock()
    return backend


def _runtime(session_id: str = "s", transport: Any = None) -> SessionRuntime:
    return SessionRuntime(
        backend=_backend(session_id),
        rpc_table={},
        queue=[],
        transport=transport if transport is not None else MagicMock(),
    )


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _pool(clock: _Clock, factory=None, *, config=None) -> SessionPool:
    async def default_factory(session_id: str) -> SessionRuntime:
        return _runtime(session_id)

    return SessionPool(_runtime("boot"), factory or default_factory, config=config, clock=clock)


class TestIdleAge:
    def test_idle_is_strictly_before_the_cutoff(self):
        rt = _runtime()
        rt.touch(50.0)

        assert rt.is_idle_since(50.1)
        assert not rt.is_idle_since(50.0)
        assert not rt.is_idle_since(49.9)

    def test_idle_seconds_is_now_minus_the_last_touch(self):
        rt = _runtime()
        rt.touch(100.0)

        assert rt.idle_seconds(160.5) == pytest.approx(60.5)

    @pytest.mark.asyncio
    async def test_the_eviction_report_says_how_long_the_session_sat_idle(self):
        clock = _Clock()
        pool = _pool(clock, config=SessionPoolConfig(idle_timeout_seconds=60.0))
        await pool.get_or_create("alpha")
        clock.t += 90.0

        report = await pool.evict_idle()

        assert [e.idle_seconds for e in report.evicted] == [pytest.approx(90.0)]

    @pytest.mark.asyncio
    async def test_a_runtime_idle_exactly_the_timeout_is_kept(self):
        clock = _Clock()
        pool = _pool(clock, config=SessionPoolConfig(idle_timeout_seconds=60.0))
        rt = await pool.get_or_create("alpha")
        clock.t += 60.0

        report = await pool.evict_idle()

        assert report.evicted == []
        assert rt in pool.runtimes

    @pytest.mark.asyncio
    async def test_no_configured_timeout_means_thirty_minutes(self):
        clock = _Clock()
        pool = _pool(clock, config=SessionPoolConfig(idle_timeout_seconds=None))
        await pool.get_or_create("alpha")

        clock.t += 30 * 60 - 1
        assert (await pool.evict_idle()).evicted == []
        clock.t += 2
        assert (await pool.evict_idle()).evicted_ids == ["alpha"]


class TestTheDirectoryRecord:
    def test_the_first_call_changes_the_record_and_a_repeat_does_not(self, tmp_path):
        rt = _runtime()

        assert rt.record_dir_registered("s", tmp_path) is True
        assert rt.record_dir_registered("s", tmp_path) is False

    def test_a_path_and_a_string_of_the_same_folder_agree(self, tmp_path):
        rt = _runtime()

        assert rt.record_dir_registered("s", tmp_path) is True
        assert rt.record_dir_registered("s", str(tmp_path)) is False
        assert rt.last_dir_registered == ("s", str(tmp_path))

    def test_a_new_id_or_a_new_folder_changes_the_record(self, tmp_path):
        rt = _runtime()
        rt.record_dir_registered("s", tmp_path)

        assert rt.record_dir_registered("renewed", tmp_path) is True
        assert rt.record_dir_registered("renewed", Path(tmp_path) / "other") is True


class TestEvictionFailures:
    @pytest.mark.asyncio
    async def test_a_failing_shutdown_still_drops_the_runtime_and_reports_it(self):
        clock = _Clock()
        failing = _runtime("alpha")
        failing.backend.shutdown = AsyncMock(side_effect=RuntimeError("disk gone"))

        async def factory(session_id: str) -> SessionRuntime:
            return failing

        pool = _pool(clock, factory, config=SessionPoolConfig(idle_timeout_seconds=1.0))
        await pool.get_or_create("alpha")
        clock.t += 5.0

        report = await pool.evict_idle()

        assert failing not in pool.runtimes
        assert [(e.session_id, e.shutdown.ok, e.shutdown.error) for e in report.evicted] == [
            ("alpha", False, "disk gone")
        ]
        assert report.kept == 1

    @pytest.mark.asyncio
    async def test_a_session_without_an_id_is_named_unknown(self):
        clock = _Clock()
        nameless = _runtime("")

        async def factory(session_id: str) -> SessionRuntime:
            nameless.register_id(session_id)
            return nameless

        pool = _pool(clock, factory, config=SessionPoolConfig(idle_timeout_seconds=1.0))
        await pool.get_or_create("alpha")
        clock.t += 5.0

        report = await pool.evict_idle()

        assert report.evicted_ids == ["<unknown>"]

    @pytest.mark.asyncio
    async def test_the_pool_shutdown_carries_on_past_a_runtime_that_fails(self):
        clock = _Clock()
        pool = _pool(clock)
        pool.default.backend.shutdown = AsyncMock(side_effect=RuntimeError("first"))
        other = await pool.get_or_create("other")

        reports = await pool.shutdown()

        assert [r.ok for r in reports] == [False, True]
        assert reports[0].error == "first"
        other.backend.shutdown.assert_awaited_once()


class TestAFactoryThatFails:
    @pytest.mark.asyncio
    async def test_the_pool_is_unchanged_and_the_next_attempt_goes_through(self):
        clock = _Clock()
        attempts: list[str] = []

        async def factory(session_id: str) -> SessionRuntime:
            attempts.append(session_id)
            if len(attempts) == 1:
                raise RuntimeError("resume failed")
            return _runtime(session_id)

        pool = _pool(clock, factory)

        with pytest.raises(RuntimeError, match="resume failed"):
            await pool.get_or_create("alpha")
        assert [r for r in pool.runtimes] == [pool.default]
        assert pool.find("alpha") is None

        rt = await asyncio.wait_for(pool.get_or_create("alpha"), timeout=1)
        assert rt.backend.session_id == "alpha"
        assert attempts == ["alpha", "alpha"]


class TestRoutingAliases:
    def test_an_empty_id_is_not_an_alias(self):
        rt = _runtime("")

        rt.register_id()
        rt.register_id("")

        assert rt.known_ids == set()

    def test_the_current_id_is_the_default_alias(self):
        rt = _runtime("s")

        rt.register_id()
        rt.backend.session_id = "renewed"
        rt.register_id()

        assert rt.known_ids == {"s", "renewed"}
        assert rt.current_session_id() == "renewed"

    @pytest.mark.asyncio
    async def test_an_unknown_id_finds_nothing_and_does_not_resume(self):
        pool = _pool(_Clock())

        assert pool.find("nobody") is None
        assert pool.runtimes == [pool.default]


class _Hitl:
    def __init__(self, waiting: list[Any], resumed: list[Any]) -> None:
        self._waiting = waiting
        self._resumed = resumed

    def waiting_messages(self) -> list[Any]:
        return list(self._waiting)

    async def resume_interrupted(self):
        for m in self._resumed:
            yield m


class _Transport:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


def _attaching(waiting, resumed):
    transport = _Transport()
    rt = _runtime("sess", transport)
    rt.backend.controllers = SimpleNamespace(hitl=_Hitl(waiting, resumed))
    return rt, transport


class TestReplayForAnAttachingWindow:
    @pytest.mark.asyncio
    async def test_without_a_request_the_messages_go_as_they_are(self):
        paused = msg.Info(text="paused", id="p1")
        resumed = msg.Info(text="resumed", id="r1")
        rt, transport = _attaching([paused], [resumed])

        await rt.replay_waiting_approvals()

        assert transport.sent == [paused, resumed]

    @pytest.mark.asyncio
    async def test_with_a_request_each_message_carries_it_and_a_stream_end_closes(self):
        rt, transport = _attaching(
            [msg.Info(text="paused", id="p1")], [msg.Info(text="resumed", id="r1")]
        )

        await rt.replay_waiting_approvals("req-7")

        assert [m.id for m in transport.sent] == ["req-7", "req-7", "req-7"]
        assert [type(m) for m in transport.sent] == [msg.Info, msg.Info, msg.StreamEnd]
        assert [getattr(m, "text", None) for m in transport.sent[:2]] == ["paused", "resumed"]
        assert transport.sent[-1].session_id == "sess"

    @pytest.mark.asyncio
    async def test_nothing_waiting_still_ends_the_requests_stream(self):
        rt, transport = _attaching([], [])

        await rt.replay_waiting_approvals("req-1")

        assert [type(m) for m in transport.sent] == [msg.StreamEnd]


class TestTheAutoNameTask:
    @pytest.mark.asyncio
    async def test_the_task_is_held_until_it_ends_and_then_let_go(self):
        rt = _runtime()
        release = asyncio.Event()
        ran: list[str] = []

        async def naming() -> None:
            await release.wait()
            ran.append("named")

        task = rt.spawn_auto_name(naming())
        await asyncio.sleep(0)

        assert rt._auto_name_tasks == {task}
        release.set()
        await task
        await asyncio.sleep(0)

        assert ran == ["named"]
        assert rt._auto_name_tasks == set()


def test_the_pool_module_no_longer_reexports_the_stamping_transport():
    """Importers take it from ``session_stamping_transport``."""
    assert not hasattr(session_pool, "SessionStampingTransport")
    assert "SessionStampingTransport" not in session_pool.__all__
