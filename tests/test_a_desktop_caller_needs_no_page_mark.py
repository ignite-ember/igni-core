"""On the desktop a window's ``caller`` needs no page mark.

``docs/design/local-cloud-switch.md`` §15.2. igni's window is the page
host, and the per-launch token is what says a connection is that window.
The page mark is the cloud's (``Layer.extension_calls_need_page_mark``):
on the desktop an unmarked window's ``caller`` is honoured as before, and a
``pages`` mark changes nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from igni_core.core.layer import Layer, layers
from igni_core.protocol import messages as msg
from tests.host_model_helpers import HostRig


def test_the_desktop_asks_for_no_mark():
    assert Layer().extension_calls_need_page_mark() is False
    assert Layer().connections_speak_for_extensions() is True


@pytest.mark.parametrize("marked", [False, True], ids=["unmarked", "pages header"])
async def test_a_window_s_caller_reaches_the_host_model(tmp_path: Path, monkeypatch, marked):
    layers.install(Layer())
    rig = await HostRig(tmp_path).start()
    reached: list[str] = []
    try:
        assert rig.model is not None and rig.orchestrator is not None
        answer_page = rig.model.handle_page

        async def handle_page(caller: str, args: dict[str, Any]) -> Any:
            reached.append(caller)
            return await answer_page(caller, args)

        monkeypatch.setattr(rig.model, "handle_page", handle_page)
        page = rig.install("acme.reviewer", host=["sessions"])
        request = msg.RPCRequest(id="h", method="host_model", caller=page, args={"op": "get"})
        request.mark_from("ws-window")
        if marked:
            request.mark_page_host()

        await rig.orchestrator.dispatch(request)

        (answer,) = [m for m in rig.windows.of(msg.RPCResponse) if m.id == "h"]
        assert "speaks for an extension" not in (answer.error or "")
        assert reached == [page]
    finally:
        await rig.stop()
