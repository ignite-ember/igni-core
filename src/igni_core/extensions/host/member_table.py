"""Every built member, by its :class:`MemberKey`, and running the one a request names."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.tag import Impl, MemberKey
from igni_core.extensions.host.plain import plain
from igni_core.extensions.host.request_binder import BoundRequest
from igni_core.extensions.host.spec import ErrorCode, MethodSpec
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.wire import Op, Request


class MemberTable:
    """The implementations, and the schema check of their answers in strict mode."""

    def __init__(self, impls: dict[MemberKey, Impl], types: SchemaTypes, strict: bool) -> None:
        self._impls = impls
        self._types = types
        self._strict = strict

    @classmethod
    def of(cls, members: Iterable[Members], types: SchemaTypes, strict: bool) -> MemberTable:
        """Every built members object's members in one table. A member built
        twice — in two classes, or under two names in one — is a bug, refused
        here alone."""
        impls: dict[MemberKey, Impl] = {}
        for built in members:
            for binding in built.table():
                key = binding.key
                if key in impls:
                    raise RuntimeError(
                        f"host member ({key.op.value}, {key.path}) implemented twice"
                    )
                impls[key] = binding.impl
        return cls(impls, types, strict)

    def implemented(self) -> set[MemberKey]:
        """Every built member, for the coverage test."""
        return set(self._impls)

    async def run(self, request: Request, bound: BoundRequest) -> Any:
        """The member's answer. Strict: one outside its schema type fails loudly
        — for tests; in production an answer is sent as the backend gave it."""
        path = request.path
        impl = self._impls.get(MemberKey(op=request.op, path=path))
        if impl is None:  # pragma: no cover — the coverage test forbids it
            raise HostModelError(ErrorCode.FAILED, f"'{path}' has no implementation")
        result = await impl(bound.call)
        if self._strict and request.op is not Op.SET:
            member = bound.member
            expected = member.result if isinstance(member, MethodSpec) else member.type
            problem = self._types.check(expected, plain(result))
            if problem is not None:
                raise AssertionError(
                    f"{path} answered outside its schema type {expected}: {problem}"
                )
        return result


__all__ = ["MemberTable"]
