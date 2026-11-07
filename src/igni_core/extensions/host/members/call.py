"""The call a member is handed: who asks, about which window and session,
with what — and what a member asks of it."""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.members.params import MemberParams
from igni_core.extensions.host.session_target import SessionTarget
from igni_core.extensions.host.spec import ErrorCode

_P = TypeVar("_P", bound=MemberParams)


class Arguments(BaseModel):
    """A method's parameters, or a set's value, validated."""

    params: dict[str, Any] = {}
    value: Any = None


class Call(BaseModel):
    """One request, resolved: who asks, about which window and session."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    extension_id: str
    grants: HostGrants
    #: The window a per-window member is about, or the caller's window.
    window: str | None
    #: The session the caller's chat is: its window's, else the default —
    #: or the one ``host.sessions.chat(id)`` named.
    target: SessionTarget
    arguments: Arguments

    @property
    def session_id(self) -> str:
        return self.target.session_id

    @property
    def on_screen(self) -> bool:
        """Whether :attr:`session_id` is the session on screen. ``False``
        only for ``host.sessions.chat(id)`` of another session."""
        return self.target.on_screen

    @property
    def params(self) -> dict[str, Any]:
        return self.arguments.params

    @property
    def value(self) -> Any:
        return self.arguments.value

    @property
    def own_client_id(self) -> str:
        """The caller's own client id: what its keys are stored under, so
        no window and no other extension reads or writes them, and the
        agent path its views are shown under."""
        return f"ext:{self.extension_id}"

    def params_as(self, model: type[_P]) -> _P:
        """The parameters, as the member's own model of them."""
        try:
            return model.model_validate(self.params)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            raise HostModelError(
                ErrorCode.INVALID, f"{model.PATH}({where}): {first['msg']}"
            ) from None

    def require_window(self) -> str:
        """The window the call is about; a member that needs one has none
        to act on when no igni window is open."""
        if self.window is None:
            raise HostModelError(ErrorCode.NOT_FOUND, "no igni window is open")
        return self.window

    def not_granted(self, what: str, permission: str) -> HostModelError:
        """The refusal of ``what``, which ``permission`` would allow."""
        return HostModelError.not_granted(self.extension_id, what, permission)


__all__ = ["Arguments", "Call"]
