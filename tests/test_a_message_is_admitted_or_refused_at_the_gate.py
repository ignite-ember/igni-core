"""The gate every message passes: who may call what, from which connection.

:class:`MessageAdmission` checks three things in a fixed order: the
connection's standing, the extension a message claims to speak for, and
the platform's own calls. :func:`access_of` says which kind of caller a
method needs. Both are driven here with the real policy tables, a real
extension registry and the real layer switch, and what is pinned is the
refusal a client would receive (or its absence).
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.backend.message_admission import MessageAdmission, access_of
from igni_core.backend.rpc_handlers.pool_guard import PAGE_RPCS, PLATFORM_RPCS
from igni_core.backend.standing_policy import APPROVAL_RPCS, WINDOW_ONLY_RPCS
from igni_core.core.layer import Layer, layers
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcAccess, RpcMethod
from igni_core.protocol.schemas.envelope import Standing


def _manifest(rpc: list[str] | None = None) -> ExtensionManifest:
    raw: dict[str, Any] = {
        "id": "acme.notes",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": ["node", "server.js"]},
    }
    if rpc:
        raw["permissions"] = {"rpc": rpc}
    return ExtensionManifest.model_validate(raw)


def _gate(installed: ExtensionManifest | None = None) -> MessageAdmission:
    registry = ExtensionRegistry()
    if installed is not None:
        registry.install(installed, state=ExtensionState.READY)
    return MessageAdmission(ExtensionRpc(registry))


def _rpc(method: str, *, caller: str | None = None) -> msg.RPCRequest:
    return msg.RPCRequest(method=method, args={}, caller=caller)


class _Cloud(Layer):
    def connections_speak_for_extensions(self) -> bool:
        return False


class _ControlOnly(Layer):
    def platform_calls_need_control(self) -> bool:
        return True


@pytest.fixture
def as_layer(monkeypatch):
    def use(layer: Layer) -> None:
        monkeypatch.setattr(layers, "current", lambda: layer)

    return use


# ── access_of ─────────────────────────────────────────────────────


def test_each_kind_of_method_needs_its_own_kind_of_caller():
    assert {access_of(m) for m in PLATFORM_RPCS} == {RpcAccess.PLATFORM}
    assert {access_of(m) for m in PAGE_RPCS} == {RpcAccess.PAGE}
    assert {access_of(m) for m in WINDOW_ONLY_RPCS - PAGE_RPCS} == {RpcAccess.WINDOW}
    assert {access_of(m) for m in APPROVAL_RPCS} == {RpcAccess.APPROVER}
    # A page's call that only the window may make is judged as the page's: narrower.
    assert {access_of(m) for m in WINDOW_ONLY_RPCS & PAGE_RPCS} <= {RpcAccess.PAGE}
    assert access_of(RpcMethod.LIST_SESSIONS) == RpcAccess.CLIENT


def test_the_narrowest_access_wins_when_a_method_is_in_two_sets(monkeypatch):
    method = RpcMethod.RUN_SHELL  # window-only
    monkeypatch.setattr(
        "igni_core.backend.message_admission.PAGE_RPCS", frozenset({method}), raising=True
    )
    assert access_of(method) == RpcAccess.PAGE
    monkeypatch.setattr(
        "igni_core.backend.message_admission.PLATFORM_RPCS", frozenset({method}), raising=True
    )
    assert access_of(method) == RpcAccess.PLATFORM


# ── The gate ──────────────────────────────────────────────────────


def test_an_ordinary_call_is_admitted():
    assert _gate().refusal(_rpc(RpcMethod.LIST_SESSIONS)) is None


def test_a_message_that_is_not_a_call_is_admitted_whatever_it_carries(as_layer):
    as_layer(_Cloud())
    assert _gate().refusal(msg.UserMessage(text="hi")) is None
    assert _gate().refusal(msg.Typing(text="x", client_id="c")) is None


def test_the_extensions_a_call_is_authorised_against_are_the_ones_the_gate_was_given():
    extensions = ExtensionRpc(ExtensionRegistry())
    assert MessageAdmission(extensions).extensions is extensions


def test_a_call_for_an_extension_that_is_not_installed_is_refused_with_the_method():
    refusal = _gate().refusal(_rpc(RpcMethod.LIST_SESSIONS, caller="acme.notes"))

    assert refusal is not None
    assert refusal.reason.startswith("list_sessions: ")
    assert "no extension 'acme.notes' is installed" in refusal.reason


def test_an_empty_caller_is_no_caller():
    assert _gate().refusal(_rpc(RpcMethod.LIST_SESSIONS, caller="")) is None


def test_an_installed_extension_may_call_its_pages_methods_and_what_its_manifest_grants():
    gate = _gate(_manifest(rpc=["list_sessions"]))

    assert gate.refusal(_rpc(RpcMethod.HOST_MODEL, caller="acme.notes")) is None
    assert gate.refusal(_rpc(RpcMethod.LIST_SESSIONS, caller="acme.notes")) is None


def test_an_installed_extension_is_refused_what_its_manifest_does_not_grant():
    refusal = _gate(_manifest()).refusal(_rpc(RpcMethod.LIST_SESSIONS, caller="acme.notes"))

    assert refusal is not None
    assert "list_sessions: 'acme.notes' is not permitted to call 'list_sessions'" in refusal.reason
    assert "permissions.rpc" in refusal.reason


def test_an_extension_s_page_never_reaches_a_pool_level_call():
    refusal = _gate(_manifest(rpc=["attach_session"])).refusal(
        _rpc(RpcMethod.ATTACH_SESSION, caller="acme.notes")
    )

    assert refusal is not None
    assert "an extension's page does not reach 'attach_session'" in refusal.reason


def test_in_the_cloud_no_connection_speaks_for_an_extension_even_an_installed_one(as_layer):
    as_layer(_Cloud())

    refusal = _gate(_manifest(rpc=["list_sessions"])).refusal(
        _rpc(RpcMethod.LIST_SESSIONS, caller="acme.notes")
    )

    assert refusal is not None
    assert refusal.reason == "list_sessions: no connection to this igni speaks for an extension"


def test_without_a_caller_the_cloud_layer_does_not_refuse(as_layer):
    as_layer(_Cloud())
    assert _gate().refusal(_rpc(RpcMethod.LIST_SESSIONS)) is None


# ── The platform's calls ──────────────────────────────────────────


def test_a_platform_call_from_any_connection_is_admitted_when_the_layer_asks_no_control():
    assert _gate().refusal(_rpc(RpcMethod.DELIVER_SERVICE_EVENTS)) is None


def test_a_layer_that_needs_control_refuses_a_platform_call_from_another_connection(as_layer):
    as_layer(_ControlOnly())

    refusal = _gate().refusal(_rpc(RpcMethod.VM_SIGN_IN))

    assert refusal is not None
    assert refusal.reason == "vm_sign_in: only on the platform's control connection"


def test_a_layer_that_needs_control_admits_a_platform_call_from_the_control_connection(as_layer):
    as_layer(_ControlOnly())
    request = _rpc(RpcMethod.VM_SIGN_IN)
    request.mark_control()

    assert _gate().refusal(request) is None


def test_the_control_requirement_is_for_platform_calls_only(as_layer):
    as_layer(_ControlOnly())
    assert _gate().refusal(_rpc(RpcMethod.LIST_SESSIONS)) is None


# ── The order ─────────────────────────────────────────────────────


def test_the_connections_standing_is_judged_before_the_caller(as_layer):
    request = _rpc(RpcMethod.LIST_SESSIONS, caller="acme.notes")
    request.mark_from("program", Standing.PROGRAM)

    refusal = _gate().refusal(request)

    assert refusal is not None
    assert refusal.reason == "list_sessions: only igni's window speaks for an extension"


def test_the_caller_is_judged_before_the_platform_control(as_layer):
    as_layer(_ControlOnly())

    refusal = _gate().refusal(_rpc(RpcMethod.VM_SIGN_IN, caller="acme.notes"))

    assert refusal is not None
    assert "no extension 'acme.notes' is installed" in refusal.reason
