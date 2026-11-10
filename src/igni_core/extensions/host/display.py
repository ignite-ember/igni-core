"""``app.display``: how igni shows a chat, and reading it for a session.

Outside the members package so the broadcasts read it the same way
``app.display`` answers it, without loading every member module.
"""

from __future__ import annotations

from typing import Any, ClassVar

from igni_core.extensions.host.backend_gateway import HostBackendGateway
from igni_core.extensions.host.rpc_wire.base import NO_ARGS
from igni_core.extensions.host.signal_values import Signal
from igni_core.protocol.rpc import RpcMethod


class Display(Signal):
    """``Display``: how igni shows a chat."""

    WIRE: ClassVar[str] = "app.display_changed"

    verbose: bool
    output_style: str
    output_styles: list[str]
    color_theme: str
    show_reasoning: bool
    show_tool_calls: bool

    @classmethod
    def from_rpc(cls, config: dict[str, Any], styles: dict[str, Any]) -> Display:
        """``get_display_config`` and ``get_output_styles``, together."""
        return cls(
            verbose=bool(config.get("show_routing", False)),
            output_style=str(styles.get("active") or ""),
            output_styles=[str(s.get("name", "")) for s in styles.get("styles") or []],
            color_theme=str(config.get("color_theme", "auto")),
            show_reasoning=bool(config.get("show_reasoning", False)),
            show_tool_calls=bool(config.get("show_tool_calls", True)),
        )


class DisplayReader:
    """Reads ``app.display`` for a session, through the backend gateway."""

    def __init__(self, gateway: HostBackendGateway) -> None:
        self._gateway = gateway

    async def read(self, session_id: str) -> Display:
        config = await self._gateway.rpc(
            RpcMethod.GET_DISPLAY_CONFIG, NO_ARGS.model_dump(), session_id=session_id
        )
        styles = await self._gateway.rpc(
            RpcMethod.GET_OUTPUT_STYLES, NO_ARGS.model_dump(), session_id=session_id
        )
        return Display.from_rpc(config, styles)


__all__ = ["Display", "DisplayReader"]
