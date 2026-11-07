"""A closed set of names a member takes, refused in its own words."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.spec import ErrorCode


class Choice(StrEnum):
    """Base: one of a member's names, or an ``INVALID`` refusal that says
    which names there are — each set in its own sentence."""

    @classmethod
    def of(cls, value: str) -> Self:
        if value not in {c.value for c in cls}:
            raise HostModelError(ErrorCode.INVALID, cls.refusal(value))
        return cls(value)

    @classmethod
    def refusal(cls, value: str) -> str:
        """The sentence that refuses ``value``."""
        raise NotImplementedError(cls.__name__)


__all__ = ["Choice"]
