"""A layer's :class:`BackendHooks` are called in order as a real backend boots, serves and stops.

``core/layer.py``, ``backend/app.py``. The cloud's guest contract is a
``BackendHooks`` of its own and lives in another package; what it can rely
on is this order and what each seam is handed, so the real
:class:`BackendApp` is run end to end under a layer that records them.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path

import pytest

from igni_core.backend.app import BackendApp
from igni_core.backend.schemas_lifecycle import BackendLaunch
from igni_core.backend.schemas_rpc import LifecyclePhase
from igni_core.core.layer import BackendHooks, Layer, layers

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the loop's signal handlers")


class _Recording(BackendHooks):
    def __init__(self, bound: bool, then: Path) -> None:
        self.calls: list[str] = []
        self.serving_kwargs: dict | None = None
        self.cwd_after_bind: str | None = None
        self._bound = bound
        self._then = then

    def starting(self) -> None:
        self.calls.append("starting")

    async def listening(self, transport) -> None:
        self.calls.append("listening")
        self.transport_when_listening = transport

    async def bind(self, transport) -> bool:
        self.calls.append("bind")
        if self._bound:
            os.chdir(self._then)  # what a mount would change
        return self._bound

    def serving(self, **kwargs) -> None:
        self.calls.append("serving")
        self.serving_kwargs = kwargs
        self.cwd_after_bind = os.getcwd()
        kwargs["on_rebind"]()  # ask the backend to shut down, as a layer that rebinds does

    async def stop(self) -> None:
        self.calls.append("stop")


class _Guest(Layer):
    name = "guest"

    def __init__(self, hooks: BackendHooks) -> None:
        self._hooks = hooks

    def backend(self) -> BackendHooks:
        return self._hooks


@contextlib.contextmanager
def _signal_handlers_restored():
    loop = asyncio.get_running_loop()
    try:
        yield
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGUSR1", None)):
            if sig is not None:
                loop.remove_signal_handler(sig)


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / ".igni").mkdir()
    monkeypatch.delenv("IGNI_PARENT_PID", raising=False)
    monkeypatch.chdir(tmp_path)  # restored by the fixture
    return tmp_path


async def _run(project: Path, hooks: _Recording) -> BackendApp:
    layers.install(_Guest(hooks))
    app = BackendApp(BackendLaunch(ws_port=0, project_dir=project))
    with _signal_handlers_restored():
        await asyncio.wait_for(app.run(), 60)
    return app


async def test_the_seams_run_in_order_and_serving_is_handed_the_backends_parts(project):
    hooks = _Recording(bound=False, then=project)
    app = await _run(project, hooks)

    assert hooks.calls == ["starting", "listening", "bind", "serving", "stop"]
    assert set(hooks.serving_kwargs) == {
        "model",
        "transport",
        "orchestrator",
        "notifications",
        "on_rebind",
        "api_url",
    }
    assert hooks.serving_kwargs["transport"] is hooks.transport_when_listening
    assert callable(hooks.serving_kwargs["api_url"])
    assert app.supervisor is not None and app.supervisor.phase is LifecyclePhase.STOPPED


async def test_a_layer_that_bound_something_has_the_backend_change_into_its_project_again(
    project, tmp_path_factory
):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    hooks = _Recording(bound=True, then=elsewhere)
    await _run(project, hooks)
    assert Path(hooks.cwd_after_bind).resolve() == project.resolve()


async def test_a_layer_that_bound_nothing_leaves_the_working_directory_alone(
    project, tmp_path_factory
):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    hooks = _Recording(bound=False, then=elsewhere)
    await _run(project, hooks)
    assert Path(hooks.cwd_after_bind).resolve() == project.resolve()  # never moved


async def test_stop_runs_even_when_the_boot_fails(project):
    class Failing(_Recording):
        async def listening(self, transport) -> None:
            await super().listening(transport)
            raise RuntimeError("the producer never came")

    hooks = Failing(bound=False, then=project)
    layers.install(_Guest(hooks))
    app = BackendApp(BackendLaunch(ws_port=0, project_dir=project))
    with _signal_handlers_restored(), pytest.raises(RuntimeError, match="producer"):
        await asyncio.wait_for(app.run(), 60)
    assert hooks.calls == ["starting", "listening", "stop"]
