"""Which member a method implements: its tag, read and written in one place."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, SkipValidation

from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.wire import Op

Impl = Callable[[Call], Awaitable[Any]]

_F = TypeVar("_F", bound=Callable[..., Any])


class MemberKey(BaseModel):
    """One member as a request names it: the op, and the member's path."""

    model_config = ConfigDict(frozen=True)

    op: Op
    path: str


class MemberBinding(BaseModel):
    """A member, and the bound method that implements it."""

    model_config = ConfigDict(frozen=True)

    key: MemberKey
    impl: SkipValidation[Impl]


class MemberTag:
    """The tag a member method carries. The only reader and writer of it."""

    _ATTRIBUTE = "_host_member"

    @classmethod
    def tag(cls, op: Op, path: str) -> Callable[[_F], _F]:
        def decorate(fn: _F) -> _F:
            setattr(fn, cls._ATTRIBUTE, MemberKey(op=op, path=path))
            return fn

        return decorate

    @classmethod
    def of(cls, fn: Any) -> MemberKey | None:
        """The member ``fn`` implements, when it is tagged."""
        key = getattr(fn, cls._ATTRIBUTE, None)
        return key if isinstance(key, MemberKey) else None


def get(path: str) -> Callable[[_F], _F]:
    return MemberTag.tag(Op.GET, path)


def put(path: str) -> Callable[[_F], _F]:
    return MemberTag.tag(Op.SET, path)


def call(path: str) -> Callable[[_F], _F]:
    return MemberTag.tag(Op.CALL, path)


__all__ = ["Impl", "MemberBinding", "MemberKey", "MemberTag", "call", "get", "put"]
