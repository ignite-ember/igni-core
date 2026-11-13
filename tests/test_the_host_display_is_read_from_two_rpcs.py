"""``app.display``: how igni shows a chat, read for one session.

:class:`DisplayReader` asks the backend for the display config and the
output styles of the session, and :meth:`Display.from_rpc` maps the two
answers to what an extension sees, with a default for what is missing.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.extensions.host.display import Display, DisplayReader
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.spec import ErrorCode
from igni_core.protocol.rpc import RpcMethod


class Gateway:
    """The backend gateway: answers each RPC from ``answers``, and keeps what was asked."""

    def __init__(self, answers: dict[RpcMethod, Any]) -> None:
        self.answers = answers
        self.asked: list[tuple[RpcMethod, dict[str, Any], str]] = []

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        self.asked.append((method, args, session_id))
        answer = self.answers[method]
        if isinstance(answer, Exception):
            raise answer
        return answer


class TestFromRpc:
    def test_each_field_is_mapped_from_the_answer_that_carries_it(self) -> None:
        shown = Display.from_rpc(
            {
                "show_routing": True,
                "color_theme": "dark",
                "show_reasoning": True,
                "show_tool_calls": False,
            },
            {"active": "concise", "styles": [{"name": "concise"}, {"name": "verbose"}]},
        )
        assert shown == Display(
            verbose=True,
            output_style="concise",
            output_styles=["concise", "verbose"],
            color_theme="dark",
            show_reasoning=True,
            show_tool_calls=False,
        )

    def test_what_is_missing_reads_as_the_default(self) -> None:
        shown = Display.from_rpc({}, {})
        assert shown == Display(
            verbose=False,
            output_style="",
            output_styles=[],
            color_theme="auto",
            show_reasoning=False,
            show_tool_calls=True,
        )

    def test_no_active_style_and_no_style_list_read_as_empty(self) -> None:
        shown = Display.from_rpc({}, {"active": None, "styles": None})
        assert shown.output_style == "" and shown.output_styles == []

    def test_a_style_without_a_name_is_an_empty_name(self) -> None:
        assert Display.from_rpc({}, {"styles": [{"name": "a"}, {}]}).output_styles == ["a", ""]

    def test_it_is_the_value_of_app_display_changed(self) -> None:
        assert Display.WIRE == "app.display_changed"


class TestReader:
    async def test_it_asks_the_config_then_the_styles_for_the_session_it_is_given(self) -> None:
        gateway = Gateway(
            {
                RpcMethod.GET_DISPLAY_CONFIG: {"show_routing": True, "color_theme": "light"},
                RpcMethod.GET_OUTPUT_STYLES: {"active": "terse", "styles": [{"name": "terse"}]},
            }
        )
        shown = await DisplayReader(gateway).read("s-7")  # type: ignore[arg-type]
        assert gateway.asked == [
            (RpcMethod.GET_DISPLAY_CONFIG, {}, "s-7"),
            (RpcMethod.GET_OUTPUT_STYLES, {}, "s-7"),
        ]
        assert shown.verbose is True and shown.color_theme == "light"
        assert shown.output_style == "terse" and shown.output_styles == ["terse"]

    async def test_a_backend_refusal_is_not_swallowed(self) -> None:
        gateway = Gateway(
            {
                RpcMethod.GET_DISPLAY_CONFIG: HostModelError(ErrorCode.REFUSED, "no config"),
                RpcMethod.GET_OUTPUT_STYLES: {},
            }
        )
        with pytest.raises(HostModelError, match="no config"):
            await DisplayReader(gateway).read("s-1")  # type: ignore[arg-type]
        assert [m for m, _, _ in gateway.asked] == [RpcMethod.GET_DISPLAY_CONFIG]
