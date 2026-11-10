"""What every RPC exchange shares: the argument base, no arguments, and
the ``info`` answer a handler refuses with."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RpcArgs(BaseModel):
    """One handler's arguments. Dumped whole — a key that is ``None`` is
    still sent, as the handler expects."""

    model_config = ConfigDict(frozen=True)


class NoArgs(RpcArgs):
    """A handler that takes nothing: dumps to ``{}``."""


#: The one :class:`NoArgs`; a handler without arguments is handed it.
NO_ARGS = NoArgs()


class RpcInfo(BaseModel):
    """An RPC handler's ``info`` answer, which says a refusal rather than raising it."""

    type: str = ""
    text: str = ""

    @classmethod
    def from_rpc(cls, info: Any) -> RpcInfo:
        if not isinstance(info, dict):
            return cls()
        kind = info.get("type")
        return cls(type=kind if isinstance(kind, str) else "", text=str(info.get("text") or ""))

    def refusal(self) -> str | None:
        """The refusal's sentence, when the answer is one."""
        if self.type != "error":
            return None
        return self.text or "refused"


__all__ = ["NO_ARGS", "NoArgs", "RpcArgs", "RpcInfo"]
