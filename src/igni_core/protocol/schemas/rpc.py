"""Process-split RPC contract.

The RPC seam separates the in-process backend from an out-of-process
FE. :class:`RPCRequest` is the call; :class:`RPCResponse` is the
answer.

An answer is one of two things — a result, or an error — never both.
That used to be a convention every producer honoured by hand, so a
response carrying both was accepted and the reader had to guess which
one the sender meant. The two factories (:meth:`RPCResponse.ok` and
:meth:`RPCResponse.fail`) build the two shapes, and the model itself
refuses a frame that carries a result *and* an error, so the
invariant belongs to the contract rather than to the callsite's
discipline.

The wire shape is unchanged and stays that way: the frontend parses
the ``"rpc_response"`` discriminator plus the ``result`` / ``error``
fields, so both outcomes keep the one type literal and the same field
names. A success with no payload is a null result and no error; a
failure is an error string and a null result.

TODO — ``RPCRequest.args`` stays ``dict[str, Any]`` and
``RPCRequest.method`` stays a plain ``str``. Tightening either would
need one model per RPC method, and the method names are open — an
extension answers under its own ``ext.*`` namespace — so the surface
cannot be closed by enumerating it here.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import Field, model_validator

from igni_core.protocol.schemas.envelope import Message


class RPCRequest(Message):
    """Generic RPC call for accessor/utility methods."""

    type: Literal["rpc_request"] = "rpc_request"
    #: The method the caller asks for. A plain string, not an enum:
    #: the platform's own methods are a known list, but an extension
    #: answers under its own ``ext.*`` namespace, and the set of those
    #: is not known here.
    method: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    #: Which extension's frame this came from, if any.
    #:
    #: ``None`` is igni's own UI, and igni's own UI may not call the
    #: ``ext.*`` namespace — an extension reaches its own service and
    #: no other, and without a caller there is nothing to check that
    #: against. The field is optional so a client that predates it
    #: keeps working: it simply cannot make extension calls, which is
    #: what it could do before.
    caller: str | None = None


class RPCResponse(Message):
    """Response to an RPCRequest.

    Carries either ``result`` (success) or ``error`` (failure) —
    never both. The two factories build the two shapes; a frame that
    carries both is refused on the way in.
    """

    type: Literal["rpc_response"] = "rpc_response"
    result: Any = None
    error: str | None = None

    @model_validator(mode="after")
    def _carries_one_outcome(self) -> Self:
        """Refuse a response that answers twice.

        A null result is a success with no payload, so the two
        outcomes are told apart by the error being present, not by the
        result being absent.
        """
        if self.result is not None and self.error is not None:
            raise ValueError("an RPC response carries a result or an error, never both")
        return self

    @classmethod
    def ok(cls, request_id: str, result: Any = None) -> RPCResponse:
        """Build a success response. Sets ``error=None``
        explicitly so the invariant is obvious at the callsite."""
        return cls(id=request_id, result=result, error=None)

    @classmethod
    def fail(cls, request_id: str, error: str) -> RPCResponse:
        """Build a failure response. Sets ``result=None``
        explicitly and takes the error message as a required
        positional so a callsite can't accidentally build a
        failure with a falsy error string."""
        return cls(id=request_id, result=None, error=error)


__all__ = [
    "RPCRequest",
    "RPCResponse",
]
