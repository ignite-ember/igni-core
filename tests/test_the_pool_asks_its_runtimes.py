"""The pool asks its runtimes; it does not reach into them.

Three things the pool used to do itself:

* it re-exported the stamping transport, so one class had two import
  paths and every importer had to know which was the real one;
* it read ``last_used_at`` off a runtime to work out how long that
  runtime had been idle — the field the runtime's own docstring says
  belongs to the runtime;
* it keyed the directory registry on a bare ``str(...)`` of whatever
  the backend handed it, without saying what that was.

Each is now a question the pool asks its owner, and the answers are
pinned here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend import session_pool as session_pool_module
from igni_core.backend.schemas_rpc import SessionPoolConfig
from igni_core.backend.session_pool import SessionPool, SessionRuntime
from igni_core.backend.session_stamping_transport import SessionStampingTransport


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _runtime(session_id: str) -> SessionRuntime:
    backend = MagicMock()
    backend.session_id = session_id
    backend.processing = False
    backend.shutdown = AsyncMock()
    return SessionRuntime(backend=backend, rpc_table={}, queue=[], transport=MagicMock())


def _pool(clock: _Clock, *, idle_timeout: float = 60.0) -> SessionPool:
    async def factory(session_id: str) -> SessionRuntime:
        return _runtime(session_id)

    return SessionPool(
        _runtime("default-s"),
        factory,
        config=SessionPoolConfig(idle_timeout_seconds=idle_timeout),
        clock=clock,
    )


class TestTheStampingTransportHasOneHome:
    def test_the_pool_does_not_re_export_it(self) -> None:
        """One class, one import path.

        The re-export was a shim for importers we control: every one of
        them is in this tree, and every one of them imports from the
        module the class lives in. A second import path is a second
        place to look, and a second thing to keep in step.
        """
        assert not hasattr(session_pool_module, "SessionStampingTransport")
        assert "SessionStampingTransport" not in session_pool_module.__all__

    def test_the_class_lives_in_its_own_module(self) -> None:
        assert SessionStampingTransport.__module__ == "igni_core.backend.session_stamping_transport"


class TestTheRuntimeAnswersHowLongItHasBeenIdle:
    @pytest.mark.asyncio
    async def test_the_eviction_report_carries_the_runtime_s_answer(self) -> None:
        """The idle age in the report is the runtime's answer.

        The pool used to compute it from the runtime's field. Now the
        runtime answers, and the report carries that answer — so the
        number is the runtime's to define, not the pool's to derive.
        """
        clock = _Clock()
        pool = _pool(clock)
        rt = await pool.get_or_create("alpha")
        rt.idle_seconds = lambda _now: 999.0  # type: ignore[method-assign]

        clock.advance(61.0)
        report = await pool.evict_idle()

        assert report.evicted_ids == ["alpha"]
        assert report.evicted[0].idle_seconds == 999.0

    @pytest.mark.asyncio
    async def test_without_a_stub_the_answer_is_the_real_idle_time(self) -> None:
        """And the answer is right when nobody stubs it."""
        clock = _Clock()
        pool = _pool(clock)
        await pool.get_or_create("alpha")

        clock.advance(61.0)
        report = await pool.evict_idle()

        assert report.evicted[0].idle_seconds == pytest.approx(61.0)


class TestTheDirKeyIsTheDirectory:
    def test_a_path_and_its_string_are_the_same_key(self) -> None:
        """The key is the directory, not the spelling of it.

        A backend that reports a ``Path`` and one that reports a
        ``str`` for the same folder must agree, or the registry would
        be written twice for one directory.
        """
        rt = _runtime("s")

        assert rt.record_dir_registered("s", "/tmp/project") is True
        assert rt.record_dir_registered("s", Path("/tmp/project")) is False

    def test_a_different_directory_is_a_different_key(self) -> None:
        rt = _runtime("s")

        assert rt.record_dir_registered("s", Path("/tmp/project")) is True
        assert rt.record_dir_registered("s", Path("/tmp/other")) is True
