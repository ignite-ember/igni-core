"""Edges of the RPC frames not reached by ``test_protocol_rpc``: the error
codes' words and meanings, which results count as present, and a request's
open fields."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.protocol.schemas.envelope import Direction
from igni_core.protocol.schemas.rpc import RpcErrorCode, RPCRequest, RPCResponse


class TestTheErrorCodesAreStableWords:
    def test_there_are_eight_and_each_is_its_own_lower_case_name(self):
        assert {code.name.lower(): code.value for code in RpcErrorCode} == {
            "unknown_method": "unknown_method",
            "invalid": "invalid",
            "forbidden": "forbidden",
            "not_found": "not_found",
            "refused": "refused",
            "unavailable": "unavailable",
            "cancelled": "cancelled",
            "failed": "failed",
        }

    @pytest.mark.parametrize("code", list(RpcErrorCode), ids=lambda c: c.value)
    def test_every_code_says_what_it_means(self, code):
        assert code.doc.strip()

    def test_no_two_codes_mean_the_same(self):
        assert len({code.doc for code in RpcErrorCode}) == len(RpcErrorCode)

    def test_a_code_is_read_from_its_word_and_an_unknown_word_is_refused(self):
        assert RPCResponse.model_validate({"error": "x", "code": "not_found"}).code is (
            RpcErrorCode.NOT_FOUND
        )
        with pytest.raises(ValidationError):
            RPCResponse.model_validate({"error": "x", "code": "exploded"})

    def test_fail_with_a_word_that_is_not_a_code_is_refused(self):
        with pytest.raises(ValidationError):
            RPCResponse.fail("r1", "boom", "exploded")  # type: ignore[arg-type]

    def test_the_code_is_written_as_its_word(self):
        dumped = RPCResponse.fail("r1", "boom", RpcErrorCode.FORBIDDEN).model_dump(mode="json")

        assert dumped["code"] == "forbidden"


class TestOnlyNoneIsAnAbsentResult:
    """A falsy result is still a result, so an error beside it is two answers."""

    @pytest.mark.parametrize("result", [0, "", [], {}, False, 0.0])
    def test_a_falsy_result_beside_an_error_is_refused(self, result):
        with pytest.raises(ValidationError):
            RPCResponse(id="r1", result=result, error="boom")

    @pytest.mark.parametrize("result", [0, "", [], {}, False])
    def test_a_falsy_result_alone_is_a_success_that_keeps_its_value(self, result):
        response = RPCResponse.ok("r1", result)

        assert response.result == result
        assert type(response.result) is type(result)
        assert response.error is None

    def test_an_empty_error_sentence_is_still_an_error(self):
        with pytest.raises(ValidationError):
            RPCResponse(id="r1", result={"ok": 1}, error="")

    def test_a_success_has_no_code(self):
        assert RPCResponse.ok("r1", 1).code is None

    def test_a_failure_has_a_null_result_on_the_wire(self):
        dumped = RPCResponse.fail("r1", "boom", RpcErrorCode.FAILED).model_dump()

        assert dumped["result"] is None
        assert dumped["error"] == "boom"

    def test_a_large_result_is_carried_whole(self):
        big = {"rows": ["x" * 100] * 5000}

        assert RPCResponse.ok("r1", big).model_dump()["result"] == big


class TestTheFramesGoTheirWays:
    def test_a_request_goes_to_igni_and_a_response_to_the_client(self):
        assert RPCRequest.direction is Direction.TO_IGNI
        assert RPCResponse.direction is Direction.TO_CLIENT

    def test_a_request_keeps_a_method_nobody_catalogued(self):
        assert RPCRequest(method="ext.demo.anything").method == "ext.demo.anything"

    def test_a_request_defaults_to_no_method_no_args_and_no_caller(self):
        request = RPCRequest()

        assert (request.method, request.args, request.caller) == ("", {}, None)

    def test_two_requests_do_not_share_their_args(self):
        RPCRequest().args["a"] = 1

        assert RPCRequest().args == {}

    def test_a_request_without_a_caller_writes_a_null_one(self):
        assert RPCRequest(method="m").model_dump()["caller"] is None

    def test_args_must_be_an_object(self):
        with pytest.raises(ValidationError):
            RPCRequest.model_validate({"args": [1, 2]})

    def test_nested_args_survive_the_wire(self):
        request = RPCRequest(method="m", args={"a": {"b": [1, "é", None]}})

        again = RPCRequest.model_validate_json(request.model_dump_json())

        assert again.args == {"a": {"b": [1, "é", None]}}
