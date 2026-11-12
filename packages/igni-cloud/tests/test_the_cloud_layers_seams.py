"""The cloud layer's seams: what ``IGNI_PROFILE=cloud`` changes.

``docs/CLOUD.md`` §9. The core asks the layer where logs go, where the
credentials come from, whether it pulls the person's service events
itself, how an MCP server is approved, and what the backend does around
its boot. In the cloud the answers are the cloud's own.
"""

from __future__ import annotations

import json

import pytest
from igni_cloud.layer import CloudLayer
from igni_cloud.logs import StdoutLog

from igni_core.core.layer import Layer


def test_the_layer_is_the_cloud():
    assert CloudLayer.name == "cloud"


def test_the_credentials_are_one_instance_of_the_layers_own():
    layer = CloudLayer()
    assert layer.credentials() is layer.credentials(), "not process-wide state"


def test_the_logs_are_one_instance_of_the_layers_own():
    layer = CloudLayer()
    assert isinstance(layer.logs, StdoutLog)
    assert layer.logs is layer.logs


def test_the_guest_contract_is_given_the_layers_credentials():
    layer = CloudLayer()
    guest = layer.backend()
    assert guest.tokens is layer.credentials()


async def test_the_guest_contract_is_given_the_layers_writer(capsys: pytest.CaptureFixture[str]):
    """The writer the guest holds after a restore is the layer's own."""
    layer = CloudLayer()
    guest = layer.backend()
    log = layer.logs.attach("igni_cloud.tests.the_writer")
    layer.logs.hold()
    try:
        log.info("held for the restore")
        layer.logs.flush()
        assert capsys.readouterr().out == "", "nothing written while held"
        await guest.stop()  # the guest lets its writer go
        layer.logs.flush()
        lines = [json.loads(line)["message"] for line in capsys.readouterr().out.splitlines()]
        assert lines == ["held for the restore"]
    finally:
        log.removeHandler(layer.logs.handler())
        layer.logs.reset()


def test_igni_in_a_vm_pulls_no_service_events_of_its_own():
    assert CloudLayer().fetches_own_events() is False
    assert Layer().fetches_own_events() is True, "the desktop's"


def test_an_mcp_server_is_approved_by_the_person_on_igni_server():
    """The cloud's policy, not the desktop's file and prompt."""
    layer = CloudLayer()
    policy = layer.mcp_approvals()
    assert policy is not None, "the desktop's would prompt; the cloud's does not"
    refusal = policy.refusal("search", "/gp/search.json")
    assert "/v1/me/mcp-approvals" in refusal, "the person's approval endpoint is named"
