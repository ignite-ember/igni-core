"""The LSP server manager: one launch per server however many callers ask, the
launch error it remembers, what it reports about each server, and a shutdown
that survives a client that will not stop.

The client is replaced by a small stand-in with the same surface, since the
client itself has its own tests against a real process.
"""

from __future__ import annotations

import asyncio

import pytest

from igni_core.core.lsp import manager as manager_mod
from igni_core.core.lsp.client import LspClientError
from igni_core.core.lsp.manager import LspServerManager
from igni_core.core.lsp.schemas import LspServerConfig


class _Clients:
    """Stands in for ``LspClient``; records each launch and shutdown."""

    def __init__(self):
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.fail_start: dict[str, Exception] = {}
        self.fail_stop: set[str] = set()
        self.start_delay = 0.0

    def __call__(self, config, project_dir):
        outer = self
        name = config.name

        class _One:
            async def start(self):
                await asyncio.sleep(outer.start_delay)
                if name in outer.fail_start:
                    raise outer.fail_start[name]
                outer.started.append(name)

            async def shutdown(self):
                outer.stopped.append(name)
                if name in outer.fail_stop:
                    raise RuntimeError("will not stop")

            async def request(self, method, params):
                return {"from": name, "method": method}

        return _One()


@pytest.fixture
def clients(monkeypatch) -> _Clients:
    fake = _Clients()
    monkeypatch.setattr(manager_mod, "LspClient", fake)
    return fake


def _manager(tmp_path, *names, **extra) -> LspServerManager:
    configs = {n: LspServerConfig(name=n, command=n, languages=[n + "-lang"]) for n in names}
    return LspServerManager(configs, tmp_path)


class TestLaunching:
    @pytest.mark.asyncio
    async def test_concurrent_first_calls_for_one_server_launch_it_once(self, tmp_path, clients):
        clients.start_delay = 0.05
        manager = _manager(tmp_path, "a")
        got = await asyncio.gather(*(manager.ensure("a") for _ in range(5)))
        assert clients.started == ["a"]
        assert all(c is got[0] for c in got)

    @pytest.mark.asyncio
    async def test_different_servers_launch_independently(self, tmp_path, clients):
        clients.start_delay = 0.05
        manager = _manager(tmp_path, "a", "b")
        await asyncio.gather(manager.ensure("a"), manager.ensure("b"))
        assert sorted(clients.started) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_a_failed_launch_is_remembered_not_cached_and_cleared_by_a_retry(
        self, tmp_path, clients
    ):
        manager = _manager(tmp_path, "a")
        clients.fail_start["a"] = LspClientError("failed to launch 'a': nope")
        with pytest.raises(LspClientError):
            await manager.ensure("a")
        assert manager.last_error("a") == "failed to launch 'a': nope"
        assert not manager.is_running("a")

        del clients.fail_start["a"]
        await manager.ensure("a")
        assert manager.last_error("a") == ""
        assert manager.is_running("a")
        assert clients.started == ["a"]

    @pytest.mark.asyncio
    async def test_an_error_that_is_not_a_client_error_propagates_unrecorded(
        self, tmp_path, clients
    ):
        manager = _manager(tmp_path, "a")
        clients.fail_start["a"] = ValueError("odd")
        with pytest.raises(ValueError, match="odd"):
            await manager.ensure("a")
        assert manager.last_error("a") == ""
        assert not manager.is_running("a")

    @pytest.mark.asyncio
    async def test_every_call_for_an_unknown_server_says_it_is_not_configured(
        self, tmp_path, clients
    ):
        manager = _manager(tmp_path, "a")
        for call in (manager.ensure("zz"), manager.query("zz", "m", None)):
            with pytest.raises(LspClientError, match="LSP server not configured: 'zz'"):
                await call
        with pytest.raises(LspClientError, match="LSP server not configured: 'zz'"):
            manager.server_info("zz")
        assert clients.started == []

    @pytest.mark.asyncio
    async def test_query_launches_then_returns_the_servers_answer(self, tmp_path, clients):
        manager = _manager(tmp_path, "a")
        assert await manager.query("a", "hover", {}) == {"from": "a", "method": "hover"}
        assert clients.started == ["a"]


class TestReporting:
    @pytest.mark.asyncio
    async def test_server_info_has_name_languages_running_and_last_error(self, tmp_path, clients):
        manager = _manager(tmp_path, "a")
        assert manager.server_info("a").model_dump() == {
            "name": "a",
            "languages": ["a-lang"],
            "running": False,
            "last_error": "",
        }
        await manager.ensure("a")
        assert manager.server_info("a").running is True

    @pytest.mark.asyncio
    async def test_all_server_info_is_sorted_and_marks_only_the_running(self, tmp_path, clients):
        manager = _manager(tmp_path, "z", "a", "m")
        await manager.ensure("m")
        info = manager.all_server_info()
        assert [(i.name, i.running) for i in info] == [("a", False), ("m", True), ("z", False)]

    def test_the_manager_copies_the_config_it_was_given(self, tmp_path):
        configs = {"a": LspServerConfig(name="a", command="a")}
        manager = LspServerManager(configs, tmp_path)
        configs["b"] = LspServerConfig(name="b", command="b")
        assert manager.list_servers() == ["a"]


class TestShutdownAll:
    @pytest.mark.asyncio
    async def test_a_client_that_will_not_stop_does_not_stop_the_others(self, tmp_path, clients):
        manager = _manager(tmp_path, "a", "b", "c")
        for n in ("a", "b", "c"):
            await manager.ensure(n)
        clients.fail_stop.add("a")
        await manager.shutdown_all()
        assert sorted(clients.stopped) == ["a", "b", "c"]
        assert not any(manager.is_running(n) for n in ("a", "b", "c"))

    @pytest.mark.asyncio
    async def test_shutdown_all_is_safe_when_nothing_runs_and_when_repeated(
        self, tmp_path, clients
    ):
        manager = _manager(tmp_path, "a")
        await manager.shutdown_all()
        await manager.ensure("a")
        await manager.shutdown_all()
        await manager.shutdown_all()
        assert clients.stopped == ["a"]

    @pytest.mark.asyncio
    async def test_a_server_can_be_launched_again_after_shutdown_all(self, tmp_path, clients):
        manager = _manager(tmp_path, "a")
        await manager.ensure("a")
        await manager.shutdown_all()
        await manager.ensure("a")
        assert clients.started == ["a", "a"]
