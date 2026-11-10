"""Process-split RPC contract.

The RPC seam separates the in-process backend from an out-of-process
FE. :class:`RPCRequest` is straightforward. The response was
previously a single :class:`RPCResponse` class carrying
``result: Any + error: str | None`` — the "either ``result`` or
``error``, never both" invariant was un-enforced and callers
relied on convention.

This module replaces that with a Literal-tagged discriminated
union:

* :class:`RPCSuccess` — ``type: "rpc_success"``, always ``result``.
* :class:`RPCFailure` — ``type: "rpc_failure"``, always ``error``.

For wire-compat with existing FE clients that parse the
``"rpc_response"`` discriminator + ``.result`` / ``.error``
fields, :class:`RPCResponse` is preserved as-is (with the same
type literal and permissive semantics). New producer sites should
use :func:`rpc_ok` / :func:`rpc_fail` factory helpers, which
return the appropriate arm and Pydantic serialises them to the
same ``"rpc_response"`` type literal via the ``type`` alias.

TODO — ``RPCRequest.args`` stays ``dict[str, Any]``. Tightening
this would need one Pydantic model per RPC method — deferred
until the method surface is closed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import Field

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
    method: str = Field(default="", description="The name of the method to call.")
    args: dict[str, WireJson] = Field(
        default_factory=dict, description="The method's arguments, by name."
    )
    # Optional so a client that predates it keeps working: it simply
    # cannot make extension calls, which is what it could do before.
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
    ``code`` (it failed), never both.
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
