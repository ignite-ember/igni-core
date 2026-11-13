"""The classes an RPC declares itself by: how long, who may, what it acts on."""

from __future__ import annotations

import inspect

import pytest

from igni_core.protocol import rpc_contract
from igni_core.protocol.rpc import RpcAccess, RpcDuration, RpcMethod, RpcScope


@pytest.mark.parametrize(
    ("duration", "seconds"),
    [
        (RpcDuration.INSTANT, 10),
        (RpcDuration.IO, 30),
        (RpcDuration.NETWORK, 60),
        (RpcDuration.LONG, 600),
        (RpcDuration.PERSON, None),
    ],
)
def test_each_duration_gives_its_timeout(duration, seconds):
    assert duration.timeout_seconds == seconds


def test_the_durations_are_these_five():
    assert [d.value for d in RpcDuration] == ["instant", "io", "network", "long", "person"]


def test_timeouts_grow_with_the_wait_and_a_person_has_none():
    ordered = [d.timeout_seconds for d in RpcDuration if d is not RpcDuration.PERSON]
    assert ordered == sorted(ordered)
    assert RpcDuration.PERSON.timeout_seconds is None


def test_every_duration_says_in_a_sentence_what_it_means():
    docs = [d.doc for d in RpcDuration]
    assert all(doc.endswith(".") and len(doc) > 20 for doc in docs)
    assert len(set(docs)) == len(docs)


def test_the_access_levels_are_these_five_and_the_scopes_these_two():
    assert [a.value for a in RpcAccess] == ["client", "approver", "window", "page", "platform"]
    assert [s.value for s in RpcScope] == ["session", "igni"]


def test_a_member_is_its_wire_string():
    assert RpcMethod.GET_CLOUD_PLAN == "get_cloud_plan"
    assert RpcAccess.WINDOW == "window"
    assert {RpcMethod.GET_CLOUD_PLAN: 1}["get_cloud_plan"] == 1


def test_the_cloud_plan_is_an_rpc_with_a_network_wait():
    method = rpc_contract.build()["methods"]["get_cloud_plan"]
    assert method["duration"] == "network"
    assert method["timeout_seconds"] == 60


def test_the_contract_publishes_each_duration_and_the_access_description():
    contract = rpc_contract.build()
    assert contract["durations"] == {
        d.value: {"doc": d.doc, "timeout_seconds": d.timeout_seconds} for d in RpcDuration
    }
    assert contract["access"] == (inspect.getdoc(RpcAccess) or "").replace("``", "`")
    assert "approver" in contract["access"]


def test_every_method_publishes_the_timeout_of_its_duration():
    for name, method in rpc_contract.build()["methods"].items():
        duration = RpcDuration(method["duration"])
        assert method["timeout_seconds"] == duration.timeout_seconds, name
        assert method["access"] in {a.value for a in RpcAccess}, name
        assert method["scope"] in {s.value for s in RpcScope}, name
