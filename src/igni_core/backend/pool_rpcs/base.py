"""One pool-level call's handler, and the placeholder for one nothing has mounted."""

from __future__ import annotations

import inspect
import logging
from abc import ABC
from typing import Any, ClassVar

from pydantic import BaseModel

from igni_core.backend.pool_replier import PoolReplier
from igni_core.backend.rpc_binding import RpcArgsError, RpcBinding
from igni_core.backend.rpc_errors import code_of
from igni_core.backend.schemas_pool import PoolAnswer
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.rpc import RpcErrorCode

logger = logging.getLogger(__name__)


class PoolRefusal(Exception):
    """A pool-level call that is not done, the sentence that says why, and
    its code (``refused`` unless said otherwise).

    Raised by a tagged method; :meth:`PoolRpc.answer` turns it into the
    call's error."""

    def __init__(self, reason: str, code: RpcErrorCode = RpcErrorCode.REFUSED) -> None:
        super().__init__(reason)
        self.code = code


class PoolRpc(ABC):
    """Answers the pool-level methods in :attr:`methods`, holding only what it needs.

    Each method is a method of the subclass tagged with
    :func:`~igni_core.backend.rpc_binding.rpc`, called as
    ``(args, request)``: the arguments its binding validated, and the
    request for what the transport set on it (the connection, the
    ``caller``). It returns its result or raises :class:`PoolRefusal`.
    """

    methods: frozenset[RpcMethod]

    #: The methods whose result goes on the wire dumped as JSON rather
    #: than as Python values — each keeps the mode it always had.
    JSON_DUMPED: ClassVar[frozenset[RpcMethod]] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        bound = RpcBinding.on(cls)
        if bound:
            cls.methods = frozenset(bound)

    async def answer(self, request: msg.RPCRequest) -> PoolAnswer:
        """The result, or the reason there is none."""
        method = RpcMethod(request.method)
        name, binding = RpcBinding.on(type(self))[method]
        try:
            args = binding.validate(request.args)
        except RpcArgsError as exc:
            return PoolAnswer.failed(self.refused_args(exc), RpcErrorCode.INVALID)
        try:
            result = getattr(self, name)(args, request)
            if inspect.isawaitable(result):
                result = await result
        except PoolRefusal as exc:
            return PoolAnswer.failed(str(exc), exc.code)
        except Exception as exc:
            # A call that broke is still answered: unanswered, the caller
            # would wait out its timeout while every window was told
            # "session routing failed".
            logger.error("%s failed: %s", method.value, exc, exc_info=True)
            return PoolAnswer.failed(f"{method.value}: {exc}", code_of(exc))
        if not isinstance(result, BaseModel):
            raise TypeError(f"{method.value} answered {type(result).__name__}, not a model")
        return PoolAnswer.ok(result, dump_mode="json" if method in self.JSON_DUMPED else "python")

    def refused_args(self, exc: RpcArgsError) -> str:
        """The error for a call whose arguments do not fit its model."""
        return str(exc)

    async def handle(self, request: msg.RPCRequest, replier: PoolReplier) -> None:
        """Answer ``request`` on the connection it came on."""
        await replier.reply(request, await self.answer(request))


class UnavailableRpc(PoolRpc):
    """A method this backend has nothing to answer with (yet), and why."""

    def __init__(self, method: RpcMethod, reason: str) -> None:
        self.methods = frozenset({method})
        self._reason = reason

    async def answer(self, request: msg.RPCRequest) -> PoolAnswer:
        return PoolAnswer.failed(self._reason, RpcErrorCode.UNAVAILABLE)
