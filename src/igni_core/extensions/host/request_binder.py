"""A request checked against the schema and the grants, and bound to its call.

Everything between "an extension asked" and "a member runs": the member
exists and is built, the grant allows it, the op fits what it is, the
window and session are resolved, and the parameters or the value are of
the schema's types. What comes out is a :class:`BoundRequest`, whose
:class:`~.members.base.Call` the member is handed.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, SkipValidation, ValidationError

from igni_core.extensions.host.call_scope import CallScope
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.members.base import Call
from igni_core.extensions.host.spec import (
    ErrorCode,
    HostSchema,
    MethodSpec,
    ObjectSpec,
    PropertySpec,
    SignalSpec,
)
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.wire import Op, Request

Member = PropertySpec | SignalSpec | MethodSpec


class Arguments(BaseModel):
    """A method's parameters, or a set's value, validated."""

    params: dict[str, Any] = {}
    value: Any = None


class BoundRequest(BaseModel):
    """A request that passed every check: its object, its member, and the call."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec: ObjectSpec
    member: Member
    call: SkipValidation[Call]


class RequestBinder:
    """Checks one request and builds the call its member runs with."""

    def __init__(self, schema: HostSchema, types: SchemaTypes, scope: CallScope) -> None:
        #: Public: a test swaps the schema for one with a stage-2 member.
        self.schema = schema
        self._types = types
        self._scope = scope

    async def bind(
        self,
        extension_id: str,
        grants: HostGrants,
        request: Request,
        home_window: str | None,
    ) -> BoundRequest:
        spec, member = self._resolve(request)
        path = request.path
        if member.stage != 1:
            raise HostModelError.stage_two(path)
        permission = spec.permission_of(member)
        if not grants.allows(permission):
            raise HostModelError.not_granted(extension_id, f"'{path}'", permission or "")
        window = self._scope.window(spec, request, home_window)
        target = await self._scope.session(
            grants=grants, spec=spec, member=member, request=request, window=window
        )
        arguments = self._arguments(member, request)
        call = Call(
            extension_id=extension_id,
            grants=grants,
            window=window,
            session_id=target.session_id,
            params=arguments.params,
            value=arguments.value,
            on_screen=target.on_screen,
        )
        return BoundRequest(spec=spec, member=member, call=call)

    def _resolve(self, request: Request) -> tuple[ObjectSpec, Member]:
        try:
            return self.schema.resolve(request.path)
        except KeyError:
            raise HostModelError(
                ErrorCode.NOT_FOUND, f"the host model has no member '{request.path}'"
            ) from None

    def _arguments(self, member: Member, request: Request) -> Arguments:
        path = request.path
        if isinstance(member, MethodSpec):
            if request.op is not Op.CALL:
                raise HostModelError(ErrorCode.INVALID, f"'{path}' is a method: call it")
            return Arguments(params=self._params(path, member, request.params))
        if isinstance(member, PropertySpec):
            if request.op is Op.CALL:
                raise HostModelError(ErrorCode.INVALID, f"'{path}' is a property: get or set it")
            if request.op is not Op.SET:
                return Arguments()
            if not member.writable:
                raise HostModelError(ErrorCode.READ_ONLY, f"'{path}' cannot be set")
            return Arguments(value=self._check(f"{path} =", member.type, request.value))
        raise HostModelError(
            ErrorCode.INVALID, f"'{path}' is a signal: subscribe to it with its wire name"
        )

    def _params(self, path: str, spec: MethodSpec, given: dict[str, Any]) -> dict[str, Any]:
        known = {p.name: p for p in spec.params}
        unknown = sorted(set(given) - set(known))
        if unknown:
            raise HostModelError(ErrorCode.INVALID, f"{path} takes no {', '.join(unknown)}")
        out: dict[str, Any] = {}
        for name, param in known.items():
            if given.get(name) is None:
                if not param.optional:
                    raise HostModelError(ErrorCode.INVALID, f"{path}: '{name}' is required")
                out[name] = None
                continue
            out[name] = self._check(f"{path}({name})", param.type, given[name])
        return out

    def _check(self, where: str, expression: str, value: Any) -> Any:
        try:
            return self._types.validate_plain(expression, value)
        except ValidationError as exc:
            raise HostModelError(
                ErrorCode.INVALID, f"{where}: expected {expression}: {exc}"
            ) from None


__all__ = ["Arguments", "BoundRequest", "RequestBinder"]
