"""Tests for the process-split RPC contract.

The wire shape is fixed by the frontend — the ``"rpc_response"``
discriminator plus the ``result`` / ``error`` fields — so these tests
pin that shape, and pin the rule it rests on: a response answers with
a result or with an error, never with both. A failure also carries the
code that says what kind of failure it was, so a client can branch on
it without reading the sentence.
"""

import pytest
from pydantic import ValidationError

from igni_core.protocol.schemas.rpc import RpcErrorCode, RPCRequest, RPCResponse


class TestTheResponseAnswersOnce:
    """A response carries one outcome, and the model enforces it."""

    def test_a_result_and_an_error_together_are_refused(self):
        with pytest.raises(ValidationError):
            RPCResponse(id="r1", result={"ok": True}, error="boom")

    def test_the_refusal_says_which_rule_was_broken(self):
        with pytest.raises(ValidationError) as caught:
            RPCResponse(id="r1", result={"ok": True}, error="boom")
        assert "never both" in str(caught.value)

    def test_a_result_alone_is_a_success(self):
        response = RPCResponse(id="r1", result={"ok": True})
        assert response.error is None
        assert response.result == {"ok": True}

    def test_an_error_alone_is_a_failure(self):
        response = RPCResponse(id="r1", error="boom")
        assert response.error == "boom"
        assert response.result is None

    def test_a_success_with_no_payload_is_allowed(self):
        response = RPCResponse(id="r1")
        assert response.result is None
        assert response.error is None


class TestTheFactoriesBuildTheTwoShapes:
    """The factories are how a producer says which outcome it means."""

    def test_ok_carries_the_result_and_no_error(self):
        response = RPCResponse.ok("r1", {"ok": True})
        assert response.id == "r1"
        assert response.result == {"ok": True}
        assert response.error is None

    def test_ok_without_a_payload_is_still_a_success(self):
        response = RPCResponse.ok("r1")
        assert response.result is None
        assert response.error is None

    def test_fail_carries_the_error_the_code_and_no_result(self):
        response = RPCResponse.fail("r1", "boom", RpcErrorCode.FAILED)
        assert response.id == "r1"
        assert response.error == "boom"
        assert response.code == RpcErrorCode.FAILED
        assert response.result is None

    def test_fail_needs_an_error_to_fail_with(self):
        with pytest.raises(TypeError):
            RPCResponse.fail("r1")  # type: ignore[call-arg]

    def test_fail_needs_to_say_what_kind_of_failure_it_was(self):
        """A failure without a code leaves a client reading the
        sentence to find out whether to retry, refuse or give up."""
        with pytest.raises(TypeError):
            RPCResponse.fail("r1", "boom")  # type: ignore[call-arg]


class TestTheWireShapeIsUnchanged:
    """The frontend parses this frame; it does not get to change."""

    def test_a_response_keeps_the_rpc_response_discriminator(self):
        assert RPCResponse.ok("r1").type == "rpc_response"

    def test_a_request_keeps_the_rpc_request_discriminator(self):
        assert RPCRequest().type == "rpc_request"

    def test_a_response_round_trips_through_json(self):
        original = RPCResponse.fail("r1", "boom", RpcErrorCode.REFUSED)
        restored = RPCResponse.model_validate_json(original.model_dump_json())
        assert restored.type == "rpc_response"
        assert restored.id == "r1"
        assert restored.error == "boom"
        assert restored.code == RpcErrorCode.REFUSED
        assert restored.result is None

    def test_a_request_round_trips_through_json(self):
        original = RPCRequest(id="r1", method="list_sessions", args={"limit": 5}, caller="ext.demo")
        restored = RPCRequest.model_validate_json(original.model_dump_json())
        assert restored.method == "list_sessions"
        assert restored.args == {"limit": 5}
        assert restored.caller == "ext.demo"

    def test_a_request_without_a_caller_still_parses(self):
        restored = RPCRequest.model_validate_json('{"type": "rpc_request"}')
        assert restored.caller is None
        assert restored.method == ""
        assert restored.args == {}
