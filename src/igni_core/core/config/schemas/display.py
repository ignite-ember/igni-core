"""``DisplayConfig`` — the ``display`` block of ``Settings``.

Solo file because display is a natural growth surface — color
themes, per-panel toggles, preview lines — and callers will
inevitably want to extend it independently of the other schemas.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DisplayConfig(BaseModel):
    """How the conversation is shown: the ``display`` section of igni's settings."""

    markdown: bool = Field(
        default=True, description="Render agent replies as Markdown rather than plain text."
    )
    show_tool_calls: bool = Field(
        default=True, description="Show the agent's tool calls in the conversation."
    )
    show_routing: bool = Field(
        default=False,
        description="Verbose mode: show which agent handles each step and how work is routed.",
    )
    show_reasoning: bool = Field(
        default=False, description="Show the model's reasoning, when it produces any."
    )
    color_theme: str = Field(
        default="auto",
        description="Color theme: `auto` (follow the terminal or system), `light` or `dark`.",
    )
    tool_result_preview_lines: int = Field(
        default=4, description="Number of lines of a tool's output shown before it is collapsed."
    )
    message_truncate_lines: int = Field(
        default=10, description="Number of lines of a long message shown before it is truncated."
    )

    def toggle_show_routing(self) -> bool:
        """Flip :attr:`show_routing` and return the new value.

        Read-mutate-return that belongs on the settings type rather
        than on ``BackendServer``. Callers use the returned bool to
        surface the new state to the user without a re-read.
        """
        self.show_routing = not self.show_routing
        return self.show_routing
