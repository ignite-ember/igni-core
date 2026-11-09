"""What a method implementing an RPC declares about it.

Every RPC in :class:`~igni_core.protocol.rpc.RpcMethod` is implemented by
one method tagged with :func:`rpc` — on an
:class:`~igni_core.backend.rpc_handlers.base.RpcHandler` for the calls a
session answers, on a :class:`~igni_core.backend.pool_rpcs.base.PoolRpc`
for the ones the session pool answers itself. The tag is an
:class:`RpcBinding`: the model the wire arguments validate into, what the
call acts on, and what it makes igni send while it works. The method's
own return annotation is its result, and its docstring is its
documentation; ``protocol/rpc_contract.py`` publishes all of it.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_core import ErrorDetails

from igni_core.protocol.messages import Message
from igni_core.protocol.rpc import RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel

#: The JSON object an RPC call carries on the wire, before its binding
#: has validated it.
WireArgs = dict[str, Any]

#: One dispatch-table entry: wire arguments in, the handler's value (or
#: its coroutine) out, for :class:`~igni_core.backend.rpc_result.RpcResult`
#: to serialize.
RpcCall = Callable[[WireArgs], Any]

#: Something a call makes igni send to its clients while it works: a
#: message class, or a push channel (sent as a ``push_notification``).
Stream = type[Message] | PushChannel

# Attribute name the decorator tags a method with. Private and
# underscored so a regular attribute cannot collide with it.
_RPC_BINDING_ATTR = "_igni_rpc_binding"

_F = TypeVar("_F", bound=Callable[..., Any])


class RpcArgs(BaseModel):
    """Base for an RPC's arguments: a key the method does not name is refused."""

    model_config = ConfigDict(extra="forbid")


class NoArgs(RpcArgs):
    """The arguments of a call that takes none: ``{}``."""


class RpcArgsError(ValueError):
    """The arguments of a call do not fit its model.

    A ``ValueError``, so every route that answers an RPC reports it the way
    it reports any other refusal; worded for whoever wrote the call.
    """

    @classmethod
    def of(cls, method: RpcMethod, exc: ValidationError, wire: WireArgs) -> RpcArgsError:
        return cls("; ".join(cls._sentence(method, error, wire) for error in exc.errors()))

    @staticmethod
    def _sentence(method: RpcMethod, error: ErrorDetails, wire: WireArgs) -> str:
        field = ".".join(str(part) for part in error["loc"])
        if error["type"] in ("missing", "string_too_short") and len(error["loc"]) == 1:
            return f"{method.value}: '{field}' is required"
        if error["type"] == "extra_forbidden":
            return f"{method.value}: '{field}' is not an argument of this call"
        if error["type"] == "dict_type" and len(error["loc"]) == 1:
            got = type(wire.get(field)).__name__
            return f"{method.value}: '{field}' must be a dict, got {got}"
        if not field:
            return f"{method.value}: {error['msg']}"
        return f"{method.value}: '{field}': {error['msg']}"


@dataclass(frozen=True)
class RpcBinding:
    """Which :class:`RpcMethod` a method implements, the model its wire
    arguments validate into, what it acts on, and what it streams."""

    method: RpcMethod
    args: type[BaseModel]
    scope: RpcScope
    #: What the call makes igni send while it works, besides its answer.
    streams: tuple[Stream, ...] = ()

    def validate(self, wire: WireArgs | None) -> BaseModel:
        """``wire`` as this call's arguments, or :class:`RpcArgsError`."""
        try:
            return self.args.model_validate(wire or {})
        except ValidationError as exc:
            raise RpcArgsError.of(self.method, exc, wire or {}) from exc

    def call(self, bound: Callable[[Any], Any]) -> RpcCall:
        """The table entry for ``bound``: its arguments validated first."""
        return lambda wire: bound(self.validate(wire))

    @classmethod
    def of(cls, attr: object) -> RpcBinding | None:
        """The binding :func:`rpc` put on ``attr``, if any."""
        binding = getattr(attr, _RPC_BINDING_ATTR, None)
        return binding if isinstance(binding, cls) else None

    @classmethod
    def on(cls, owner: type) -> dict[RpcMethod, tuple[str, RpcBinding]]:
        """Every binding on ``owner``'s methods, inherited ones included,
        with the name of the method that carries it."""
        found: dict[RpcMethod, tuple[str, RpcBinding]] = {}
        for name, attr in inspect.getmembers(owner):
            binding = cls.of(attr)
            if binding is None:
                continue
            if binding.method in found:
                raise RuntimeError(
                    f"duplicate @rpc({binding.method!r}) binding in {owner.__name__}"
                )
            found[binding.method] = (name, binding)
        return found


def rpc(
    method: RpcMethod,
    *,
    args: type[BaseModel],
    scope: RpcScope,
    streams: tuple[Stream, ...] = (),
) -> Callable[[_F], _F]:
    """Tag a method as the implementation of ``method``.

    The wire arguments are validated into ``args`` and the method receives
    the model. The decorator returns the function unchanged; the tag lives
    as an attribute on it."""

    binding = RpcBinding(method, args, scope, streams)

    def decorator(fn: _F) -> _F:
        setattr(fn, _RPC_BINDING_ATTR, binding)
        return fn

    return decorator


__all__ = [
    "NoArgs",
    "RpcArgs",
    "RpcArgsError",
    "RpcBinding",
    "RpcCall",
    "Stream",
    "WireArgs",
    "rpc",
]
