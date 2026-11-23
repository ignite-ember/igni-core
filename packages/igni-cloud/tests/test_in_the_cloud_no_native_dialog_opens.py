"""In the cloud, no call opens a native dialog: it is refused as an RPC error.

``docs/design/local-cloud-switch.md`` §5. Nobody sees a VM's screen, so
``pick_dir_native`` would open a folder picker nowhere and wait for ever.
igni-app hides the picker in cloud mode; any other client, or an old one,
gets an ``unavailable`` answer at once, and the picker is never started.
"""

from __future__ import annotations

from pathlib import Path

from igni_cloud.layer import CloudLayer

from igni_core.backend.folder_picker import NativeFolderPicker
from igni_core.core.layer import Layer, layers
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.rpc import RpcErrorCode
from tests.host_model_helpers import HostRig


class NoScreenHere(Layer):
    """The cloud layer's answer to this one seam, on a desktop backend."""

    def opens_native_dialogs(self) -> bool:
        return CloudLayer().opens_native_dialogs()


def test_the_cloud_layer_opens_no_native_dialog():
    assert CloudLayer().opens_native_dialogs() is False


async def test_the_folder_picker_is_refused_before_any_picker_starts(tmp_path: Path, monkeypatch):
    layers.install(NoScreenHere())
    started: list[Path] = []

    def for_platform(start_dir: Path) -> NativeFolderPicker:
        started.append(start_dir)
        raise AssertionError("no picker may start in the cloud")

    monkeypatch.setattr(NativeFolderPicker, "for_platform", staticmethod(for_platform))
    rig = await HostRig(tmp_path).start()
    try:
        assert rig.orchestrator is not None
        request = msg.RPCRequest(id="p", method="pick_dir_native", args={})
        request.mark_from("ws-producer")
        await rig.orchestrator.dispatch(request)

        (answer,) = [m for m in rig.windows.of(msg.RPCResponse) if m.id == "p"]
        assert answer.code == RpcErrorCode.UNAVAILABLE
        assert "cannot open a dialog" in (answer.error or "")
        assert answer.to_client == "ws-producer"
        assert started == []
    finally:
        await rig.stop()
