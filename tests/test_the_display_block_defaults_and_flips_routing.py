"""``display``: what the conversation shows by default, and the verbose toggle."""

from __future__ import annotations

from igni_core.core.config.schemas import DisplayConfig


def test_a_fresh_display_block_renders_markdown_and_tool_calls_but_hides_routing_and_reasoning():
    display = DisplayConfig()
    assert display.model_dump() == {
        "markdown": True,
        "show_tool_calls": True,
        "show_routing": False,
        "show_reasoning": False,
        "color_theme": "auto",
        "tool_result_preview_lines": 4,
        "message_truncate_lines": 10,
    }


def test_every_display_field_carries_a_description_for_the_generated_sdks():
    schema = DisplayConfig.model_json_schema()["properties"]
    assert set(schema) == set(DisplayConfig.model_fields)
    for name, prop in schema.items():
        assert prop.get("description"), name


def test_toggling_routing_returns_the_new_value_and_twice_restores_it():
    display = DisplayConfig()
    assert display.toggle_show_routing() is True
    assert display.show_routing is True
    assert display.toggle_show_routing() is False
    assert display.show_routing is False


def test_toggling_one_block_leaves_another_alone():
    first, second = DisplayConfig(), DisplayConfig()
    first.toggle_show_routing()
    assert second.show_routing is False
