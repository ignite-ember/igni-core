"""Two tabs of one browser are one window, and closing one leaves the other.

Every tab of a browser profile keeps the same window id (the web client
stores it in ``localStorage``), so two tabs are one window that two
connections show. The registry kept one connection per window: the
newer tab's hydrate replaced the older tab's connection, so

* the older tab's connection was no longer recognised as that window's
  (``by_connection``): a page it frames lost its home window, and what
  it typed or sent no longer made the window the active one;
* closing the newer tab closed the window although the older tab still
  showed it: ``host.windows`` said it was gone and ``host.ui`` had no
  active window to address.

The owner's word (F5, igni-app #96, CHAT-027): both tabs follow the same
session and both work; the old tab must not die. These run the window
model through the backend as the app wires it (:class:`HostModelWiring`),
with the connections closing as the WebSocket transport reports them.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from igni_core.extensions.host.wire import Reply
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import HostRig


def _from(client_id: str, method: str, *, caller: str | None = None, **args: Any) -> Any:
    request = msg.RPCRequest(id=f"{method}-{client_id}", method=method, args=args, caller=caller)
    request.mark_from(client_id)
    return request


class Connections:
    """The transport's connection watchers, so a test can close a connection."""

    def __init__(self) -> None:
        self.watchers: list[Callable[[str, str], None]] = []

    def watch(self, watcher: Callable[[str, str], None]) -> None:
        self.watchers.append(watcher)

    def close(self, client_id: str) -> None:
        for watcher in self.watchers:
            watcher("detached", client_id)


async def _rig(tmp_path: Path) -> tuple[HostRig, Connections]:
    rig = HostRig(tmp_path)
    connections = Connections()
    rig.windows.watch_connections = connections.watch  # type: ignore[method-assign]
    await rig.start()
    assert connections.watchers, "the wiring watches the connections close"
    return rig, connections


async def _two_tabs(rig: HostRig) -> None:
    assert rig.orchestrator is not None
    for tab in ("ws-old", "ws-new"):
        await rig.orchestrator.dispatch(
            _from(tab, RpcMethod.WINDOW_MODEL, op="hydrate", window="w-1", kind="web")
        )


async def test_both_tabs_connections_are_the_window_s(tmp_path: Path) -> None:
    rig, _ = await _rig(tmp_path)
    try:
        await _two_tabs(rig)
        assert rig.model is not None
        registry = rig.model.services.windows.registry

        assert registry.by_connection("ws-old") == "w-1"
        assert registry.by_connection("ws-new") == "w-1"
        assert [w.id for w in registry.describe_all()] == ["w-1"]
    finally:
        await rig.stop()


async def test_closing_the_newer_tab_leaves_the_window_open_in_the_older(tmp_path: Path) -> None:
    rig, connections = await _rig(tmp_path)
    try:
        await _two_tabs(rig)
        assert rig.model is not None
        registry = rig.model.services.windows.registry

        connections.close("ws-new")

        assert registry.known("w-1")
        assert registry.active() == "w-1"
        assert registry.by_connection("ws-old") == "w-1"
        assert [w.id for w in registry.describe_all()] == ["w-1"]

        connections.close("ws-old")
        assert not registry.known("w-1")
        assert registry.describe_all() == []
    finally:
        await rig.stop()


async def test_a_page_in_the_older_tab_keeps_its_home_window(tmp_path: Path, monkeypatch) -> None:
    rig, connections = await _rig(tmp_path)
    try:
        assert rig.orchestrator is not None and rig.model is not None
        page = rig.install("acme.reviewer", host=["ui"])
        await _two_tabs(rig)
        connections.close("ws-new")
        seen: list[dict[str, Any]] = []
        real = rig.model.handle_page

        async def recording(caller: str, args: dict[str, Any]) -> Reply:
            seen.append(dict(args))
            return await real(caller, args)

        monkeypatch.setattr(rig.model, "handle_page", recording)

        await rig.orchestrator.dispatch(
            _from("ws-old", RpcMethod.HOST_MODEL, caller=page, op="get", path="ui.route")
        )

        assert seen[0].get("home_window") == "w-1"
    finally:
        await rig.stop()
