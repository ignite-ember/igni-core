"""The pool-level calls are exactly those the pool answers, and no runtime answers them.

``POOL_LEVEL_RPCS`` is what the pool's admission and router ``in``-check
against: a method missing from it would be dispatched to a runtime that
cannot answer it, a method added to it would never reach the runtime that
should. The two subsets say who may reach a pool-level call — an
extension's page, or only the platform — and both sit inside the whole.
The guard registered under each of them fails loudly instead of answering.
"""

from __future__ import annotations

import pytest

from igni_core.backend.rpc_handlers.pool_guard import (
    PAGE_RPCS,
    PLATFORM_RPCS,
    POOL_LEVEL_RPCS,
    PoolGuardRpcHandler,
)
from igni_core.protocol.rpc import RpcMethod


def test_the_pool_level_calls_are_exactly_these_eight():
    assert POOL_LEVEL_RPCS == {
        RpcMethod.ATTACH_SESSION,
        RpcMethod.HOST_MODEL,
        RpcMethod.WINDOW_MODEL,
        RpcMethod.EXTENSION_SERVICE_TOKEN,
        RpcMethod.DELIVER_SERVICE_EVENTS,
        RpcMethod.VM_SIGN_IN,
        RpcMethod.MINT_APPROVER_TOKEN,
        RpcMethod.REVOKE_APPROVER_TOKENS,
    }


def test_an_extension_page_may_reach_only_the_host_model_and_the_service_token():
    assert PAGE_RPCS == {RpcMethod.HOST_MODEL, RpcMethod.EXTENSION_SERVICE_TOKEN}


def test_only_the_platform_makes_service_event_delivery_and_vm_sign_in():
    assert PLATFORM_RPCS == {RpcMethod.DELIVER_SERVICE_EVENTS, RpcMethod.VM_SIGN_IN}


def test_the_subsets_stay_inside_the_pool_level_calls_and_never_overlap():
    assert PAGE_RPCS <= POOL_LEVEL_RPCS
    assert PLATFORM_RPCS <= POOL_LEVEL_RPCS
    assert not PAGE_RPCS & PLATFORM_RPCS


@pytest.mark.parametrize("calls", [POOL_LEVEL_RPCS, PAGE_RPCS, PLATFORM_RPCS])
def test_the_sets_cannot_be_changed_by_a_caller(calls):
    assert isinstance(calls, frozenset)
    with pytest.raises(AttributeError):
        calls.add(RpcMethod.SHUTDOWN)  # type: ignore[attr-defined]


def test_the_guard_is_registered_under_every_pool_level_call_and_nothing_else():
    table = PoolGuardRpcHandler(ctx=None).methods()  # type: ignore[arg-type]

    assert set(table) == POOL_LEVEL_RPCS


@pytest.mark.parametrize("args", [{}, {"anything": 1}, {"name": None}])
def test_a_pool_level_call_that_reaches_a_runtime_raises_whatever_it_carries(args):
    table = PoolGuardRpcHandler(ctx=None).methods()  # type: ignore[arg-type]

    for method, guard in table.items():
        with pytest.raises(RuntimeError, match="pool-level RPC dispatched through per-runtime"):
            guard(args)
        assert method in POOL_LEVEL_RPCS
