"""``get_status``: one reading of igni's status, for every member that asks."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from igni_core.extensions.host.values import Connection, SessionStatus


class StatusAnswer(BaseModel):
    """What ``get_status`` answers, as far as the members read it.

    The coercions are :meth:`SessionStatus.from_status`'s, exactly: a
    value the backend leaves out or sends as ``None`` reads as it always
    has, rather than being refused by a field validator.
    """

    model: str
    model_configured: bool
    permission_mode: str
    max_context: int
    cloud_connected: bool
    input_tokens: int
    output_tokens: int
    context_tokens: int

    @classmethod
    def from_rpc(cls, raw: dict[str, Any]) -> StatusAnswer:
        return cls(
            model=str(raw.get("model", "")),
            model_configured=bool(raw.get("model_configured", True)),
            permission_mode=str(raw.get("permission_mode", "default")),
            max_context=int(raw.get("max_context") or 0),
            cloud_connected=bool(raw.get("cloud_connected")),
            input_tokens=int(raw.get("input_tokens") or 0),
            output_tokens=int(raw.get("output_tokens") or 0),
            context_tokens=int(raw.get("context_tokens") or 0),
        )

    def connection(self) -> Connection:
        """``app.connection``."""
        return Connection(
            cloud_connected=self.cloud_connected, model_configured=self.model_configured
        )

    def session_status(self, session_id: str) -> SessionStatus:
        """``chat.status``, for ``session_id``."""
        return SessionStatus(
            session_id=session_id,
            model=self.model,
            model_configured=self.model_configured,
            permission_mode=self.permission_mode,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            context_tokens=self.context_tokens,
            max_context=self.max_context,
        )


__all__ = ["StatusAnswer"]
