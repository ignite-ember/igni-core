"""A method's parameters, as a member's own model of them.

A member reads its parameters with
:meth:`~igni_core.extensions.host.members.call.Call.params_as`. Each
model names the method it is for; its fields are that method's
parameters in the host schema, name for name
(``tests/test_member_params_are_the_schemas.py`` holds them to it).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class MemberParams(BaseModel):
    """Base: one method's parameters, already checked against the schema's types."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The method these are the parameters of, as the schema names it.
    PATH: ClassVar[str]


__all__ = ["MemberParams"]
