"""The igni in the cloud keeps running for a group whose mode is ``cloud``.

``docs/design/local-cloud-switch.md`` §6.3. A ``cloud`` group's pack
closes the run gate on its people's computers; the VM's igni reads the
same pack, and it is the cloud the mode sends them to, so its gate stays
open.
"""

from __future__ import annotations

from igni_cloud.layer import CloudLayer

from igni_core.core.config.group_policy.gate import GatePhase, RunGate
from igni_core.core.config.group_policy.pack import GroupMode


def test_the_cloud_layer_is_not_the_person_s_computer():
    assert CloudLayer().runs_on_the_persons_computer() is False


def test_a_cloud_group_leaves_the_vm_s_gate_open(cloud_layer: CloudLayer):
    gate = RunGate()

    for mode in GroupMode:
        assert gate.close_for_mode(mode) is False, mode

    assert gate.phase is GatePhase.open and gate.reason is None
