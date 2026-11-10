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

from enum import StrEnum
from typing import Any, ClassVar, Literal, Self

from pydantic import Field, model_validator

from igni_core.protocol.json_value import WireJson
from igni_core.protocol.schemas.envelope import Direction, Message


class RpcErrorCode(StrEnum):
    """Why a call failed, as a word a client can branch on; ``error`` beside
    it is the sentence for a person."""

    UNKNOWN_METHOD = "unknown_method"
    INVALID = "invalid"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def doc(self) -> str:
        """What the code means, for whoever reads the contract."""
        return _ERROR_DOCS[self]


_ERROR_DOCS: dict[RpcErrorCode, str] = {
    RpcErrorCode.UNKNOWN_METHOD: "No method of that name: a typo, or a newer method than this igni.",
    RpcErrorCode.INVALID: "The arguments do not fit the method: one is missing, unknown, "
    "or of the wrong type.",
    RpcErrorCode.FORBIDDEN: "This connection may not make the call: its standing, or the "
    "extension it speaks for, does not reach it.",
    RpcErrorCode.NOT_FOUND: "What the call names does not exist: a file, a task, a run.",
    RpcErrorCode.REFUSED: "The call was understood and not done: a rule, a policy or the "
    "state of things said no. Nothing changed.",
    RpcErrorCode.UNAVAILABLE: "Nothing in this igni answers the call: the feature is not set "
    "up here (no extension support, not a cloud VM, no token).",
    RpcErrorCode.CANCELLED: "The work the call started was cancelled before it finished.",
    RpcErrorCode.FAILED: "It was allowed and it went wrong. The sentence says what happened.",
}


class RPCRequest(Message):
    """A call to one of igni's methods. igni answers with an ``rpc_response`` carrying the same ``id``."""

    type: Literal["rpc_request"] = "rpc_request"
    direction: ClassVar[Direction] = Direction.TO_IGNI
    #: The method the caller asks for. A plain string, not an enum:
    #: the platform's own methods are a known list, but an extension
    #: answers under its own ``ext.*`` namespace, and the set of those
    #: is not known here.
    method: str = Field(default="", description="The name of the method to call.")
    args: dict[str, WireJson] = Field(
        default_factory=dict, description="The method's arguments, by name."
    )
    #: Which extension's frame this came from, if any.
    #:
    #: ``None`` is igni's own UI, and igni's own UI may not call the
    #: ``ext.*`` namespace — an extension reaches its own service and
    #: no other, and without a caller there is nothing to check that
    #: against. The field is optional so a client that predates it
    #: keeps working: it simply cannot make extension calls, which is
    #: what it could do before.
    caller: str | None = Field(
        default=None,
        description=(
            "The id of the extension whose page is making the call; null for igni's own "
            "interface. Methods under `ext.` need it: an extension may reach only its own "
            "service."
        ),
    )


class RPCResponse(Message):
    """The answer to an ``rpc_request``, with the request's ``id``.

    It carries either ``result`` (the call succeeded) or ``error`` and
    ``code`` (it failed), never both. The two factories build the two
    shapes; a frame that carries both is refused on the way in.
    """

    # Construct with the ``ok`` / ``fail`` factories; they enforce the
    # either-or shape at the callsite.

    type: Literal["rpc_response"] = "rpc_response"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    result: WireJson = None
    error: str | None = Field(
        default=None, description="What went wrong, as a sentence for a person; null on success."
    )
    code: RpcErrorCode | None = Field(
        default=None, description="Why the call failed, for a client to branch on; null on success."
    )

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
    def fail(cls, request_id: str, error: str, code: RpcErrorCode) -> RPCResponse:
        """Build a failure response. Sets ``result=None``
        explicitly and takes the error message and its code as
        required positionals so a callsite can't build a failure
        without saying what kind it is."""
        return cls(id=request_id, result=None, error=error, code=code)


__all__ = [
    "RpcErrorCode",
    "RPCRequest",
    "RPCResponse",
]
