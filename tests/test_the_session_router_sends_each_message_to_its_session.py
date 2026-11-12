"""The router between a window's messages and the pool's runtimes.

On the real :class:`SessionPool` and :class:`PoolDirectories` (SQLite under
``tmp_path``) with recording dispatchers: every message is shown to the
watchers first; the session list always goes to the boot dispatcher;
anything else goes to the runtime its ``session_id`` names, resumed when
it is not live, with the ids and the directory recorded around the
dispatch; an attach refusal is passed back untouched and builds nothing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.pool_directories import PoolDirectories
from igni_core.backend.schemas_pool import AttachRefused, ProjectDirs
from igni_core.backend.schemas_rpc import AttachSessionResult
from igni_core.backend.session_pool import SessionPool, SessionRuntime
from igni_core.backend.session_router import SessionRouter
from igni_core.core.config.settings import Settings
from igni_core.core.db.locator import StorageLocator
from igni_core.core.session.session_directories import SessionDirectoryStore
from igni_core.protocol import messages as msg


class _Dispatcher:
    def __init__(self, name: str, log: list[tuple[str, Any]]) -> None:
        self.name = name
        self._log = log
        self.on_dispatch: Any = None

    async def dispatch(self, message: Any) -> None:
        self._log.append((self.name, message))
        if self.on_dispatch is not None:
            self.on_dispatch()


def _runtime(session_id: str, project_dir: Path) -> SessionRuntime:
    backend = MagicMock()
    backend.session_id = session_id
    backend.project_dir = project_dir
    backend.processing = False
    backend.shutdown = AsyncMock()
    return SessionRuntime(backend=backend, rpc_table={}, queue=[], transport=MagicMock())


def _rig(tmp_path: Path):
    settings = Settings(storage={"data_dir": str(tmp_path / "data")})
    store = SessionDirectoryStore.from_locator(StorageLocator.from_settings(settings))
    project = tmp_path / "project"
    project.mkdir()
    dirs = PoolDirectories(store, ProjectDirs(project_dir=project))
    log: list[tuple[str, Any]] = []
    resumed: list[str] = []
    dispatchers: dict[int, _Dispatcher] = {}

    async def factory(session_id: str) -> SessionRuntime:
        resumed.append(session_id)
        return _runtime(session_id, dirs.dir_for(session_id))

    def dispatcher_for(runtime: SessionRuntime) -> _Dispatcher:
        return dispatchers.setdefault(
            id(runtime), _Dispatcher(f"runtime:{runtime.backend.session_id}", log)
        )

    boot = _runtime("boot", project)
    pool = SessionPool(boot, factory)
    default_dispatcher = _Dispatcher("boot", log)
    router = SessionRouter(
        pool=pool,
        dirs=dirs,
        wiring=SimpleNamespace(dispatcher_for=dispatcher_for),  # type: ignore[arg-type]
        default_dispatcher=default_dispatcher,  # type: ignore[arg-type]
    )
    return SimpleNamespace(
        router=router,
        pool=pool,
        boot=boot,
        store=store,
        project=project,
        log=log,
        resumed=resumed,
        default_dispatcher=default_dispatcher,
        dispatcher_for=dispatcher_for,
    )


def _text(text: str, session_id: str = "") -> msg.Message:
    return msg.UserMessage(text=text, session_id=session_id)


async def test_a_message_without_a_stamp_goes_to_the_boot_dispatcher(tmp_path) -> None:
    rig = _rig(tmp_path)
    message = _text("hello")

    await rig.router.route(message)

    assert rig.log == [("boot", message)]
    assert rig.resumed == []


async def test_a_message_for_an_unloaded_session_resumes_it_and_goes_there(tmp_path) -> None:
    rig = _rig(tmp_path)
    message = _text("hello", "other")

    await rig.router.route(message)

    assert rig.resumed == ["other"]
    assert rig.log == [("runtime:other", message)]
    assert rig.pool.runtimes[1].backend.session_id == "other"


async def test_a_second_message_for_the_same_session_does_not_resume_again(tmp_path) -> None:
    rig = _rig(tmp_path)

    await rig.router.route(_text("one", "other"))
    await rig.router.route(_text("two", "other"))

    assert rig.resumed == ["other"]
    assert [name for name, _ in rig.log] == ["runtime:other", "runtime:other"]


async def test_the_session_list_goes_to_the_boot_dispatcher_whatever_its_stamp(tmp_path) -> None:
    rig = _rig(tmp_path)
    message = msg.SessionList(session_id="other")

    await rig.router.route(message)

    assert rig.log == [("boot", message)]
    assert rig.resumed == []


async def test_watchers_see_every_message_before_it_is_routed_in_order(tmp_path) -> None:
    rig = _rig(tmp_path)
    seen: list[tuple[str, str]] = []
    rig.router.watch_inbound(lambda m: seen.append(("a", m.type)))
    rig.router.watch_inbound(lambda m: seen.append(("b", m.type)))
    rig.router.watch_inbound(lambda m: seen.append(("log", str(len(rig.log)))))

    await rig.router.route(_text("hi"))
    await rig.router.route(msg.SessionList())

    assert seen == [
        ("a", "user_message"),
        ("b", "user_message"),
        ("log", "0"),
        ("a", "session_list"),
        ("b", "session_list"),
        ("log", "1"),
    ]


async def test_a_watcher_that_raises_stops_the_message_from_being_routed(tmp_path) -> None:
    rig = _rig(tmp_path)

    def boom(message: Any) -> None:
        raise RuntimeError("watcher failed")

    rig.router.watch_inbound(boom)

    with pytest.raises(RuntimeError, match="watcher failed"):
        await rig.router.route(_text("hi"))

    assert rig.log == []


async def test_a_session_renamed_while_handling_still_routes_by_both_ids(tmp_path) -> None:
    """``/clear`` renews the id during dispatch; the old one must keep finding the runtime."""
    rig = _rig(tmp_path)
    await rig.router.route(_text("first", "chat"))
    runtime = rig.pool.runtimes[1]
    rig.dispatcher_for(runtime).on_dispatch = lambda: setattr(
        runtime.backend, "session_id", "renewed"
    )

    await rig.router.route(_text("/clear", "chat"))
    rig.dispatcher_for(runtime).on_dispatch = None
    await rig.router.route(_text("after", "renewed"))
    await rig.router.route(_text("late", "chat"))

    assert rig.resumed == ["chat"]
    assert len(rig.pool.runtimes) == 2
    assert runtime.known_ids >= {"chat", "renewed"}


async def test_the_directory_is_recorded_once_and_again_only_when_it_changes(tmp_path) -> None:
    rig = _rig(tmp_path)
    await rig.router.route(_text("one", "chat"))
    runtime = rig.pool.runtimes[1]
    assert Path(rig.store.get_dir("chat")) == rig.project

    moved = tmp_path / "moved"
    moved.mkdir()
    runtime.backend.project_dir = moved
    await rig.router.route(_text("two", "chat"))
    assert Path(rig.store.get_dir("chat")) == moved

    runtime.backend.project_dir = tmp_path
    rig.store.set_dir("chat", moved)
    runtime.record_dir_registered("chat", tmp_path)  # already registered: no rewrite
    await rig.router.route(_text("three", "chat"))
    assert Path(rig.store.get_dir("chat")) == moved


async def test_the_boot_runtime_uses_the_boot_dispatcher_and_others_the_wirings(tmp_path) -> None:
    rig = _rig(tmp_path)
    other = await rig.pool.get_or_create("other")

    assert rig.router.dispatcher_for(rig.boot) is rig.default_dispatcher
    assert rig.router.dispatcher_for(other) is rig.dispatcher_for(other)
    assert rig.router.dispatcher_for(other) is not rig.default_dispatcher


async def test_attaching_a_session_returns_its_id_and_directory(tmp_path) -> None:
    rig = _rig(tmp_path)

    result = await rig.router.attach("chat", None)

    assert result == AttachSessionResult(session_id="chat", project_dir=str(rig.project))
    assert rig.resumed == ["chat"]


async def test_a_refused_attach_is_returned_as_it_is_and_builds_no_runtime(tmp_path) -> None:
    rig = _rig(tmp_path)

    refused = await rig.router.attach("chat", str(tmp_path / "not-a-folder"))

    assert isinstance(refused, AttachRefused)
    assert refused.reason == f"not a directory: {tmp_path / 'not-a-folder'}"
    assert rig.resumed == []
    assert rig.pool.runtimes == [rig.boot]


async def test_a_session_that_cannot_be_resumed_raises_and_leaves_the_pool_alone(tmp_path) -> None:
    rig = _rig(tmp_path)

    async def failing(session_id: str) -> SessionRuntime:
        raise RuntimeError("resume failed")

    rig.pool._factory = failing

    with pytest.raises(RuntimeError, match="resume failed"):
        await rig.router.route(_text("hi", "other"))

    assert rig.pool.runtimes == [rig.boot]
    assert rig.log == []


async def test_refreshing_agents_rebuilds_every_live_session(tmp_path) -> None:
    rig = _rig(tmp_path)
    other = await rig.pool.get_or_create("other")

    rig.router.refresh_agents()

    rig.boot.backend.reload_agents.assert_called_once_with()
    other.backend.reload_agents.assert_called_once_with()
