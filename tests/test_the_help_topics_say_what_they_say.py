"""`/help <topic>` answers from a catalog, and the catalog is exact.

The topic texts are what a person reads when they ask igni how to use
it, so they have to name the product igni, point at the directories
igni really reads, and fail with the list of topics that exist.
"""

from __future__ import annotations

import pytest

from igni_core.backend.schemas_help import HelpTopic, HelpTopicCatalog, TopicNotFoundResult
from igni_core.core.paths import CONFIG_DIR, DEFAULT_DATA_DIR
from igni_core.protocol.messages import CommandResultKind

TOPICS = ["agents", "hooks", "loop", "mcp", "memory", "plugins", "schedule", "shortcuts"]


def text_of(name: str) -> str:
    return HelpTopicCatalog.default().render(name).content


class TestTheDefaultCatalog:
    def test_it_holds_exactly_these_topics(self):
        assert HelpTopicCatalog.default().names() == TOPICS

    def test_every_topic_renders_as_markdown_under_its_own_heading(self):
        for name in TOPICS:
            result = HelpTopicCatalog.default().render(name)
            assert result.kind == CommandResultKind.MARKDOWN
            assert result.content.strip(), name

    def test_no_topic_names_the_old_product(self):
        for name in TOPICS:
            assert "mber" not in text_of(name).lower(), name

    def test_the_plugins_topic_calls_the_install_directory_igni_user_global(self):
        text = text_of("plugins")

        assert f"`{DEFAULT_DATA_DIR}/plugins/` (igni user-global" in text
        assert f"into `{DEFAULT_DATA_DIR}/plugins/`" in text
        assert f"`<project>/{CONFIG_DIR}/plugins/`" in text
        assert "`~/.claude/plugins/` (Claude user-global)" in text

    def test_the_hooks_topic_lists_the_four_settings_files(self):
        text = text_of("hooks")

        for path in (
            f"{DEFAULT_DATA_DIR}/settings.json",
            f"{DEFAULT_DATA_DIR}/settings.local.json",
            f"{CONFIG_DIR}/settings.json",
            f"{CONFIG_DIR}/settings.local.json",
        ):
            assert f"`{path}`" in text

    def test_the_agents_topic_points_at_the_agents_directory(self):
        text = text_of("agents")

        assert f"add `.md` files to `{CONFIG_DIR}/agents/`" in text
        assert "`/agents promote <name>`" in text

    def test_the_schedule_topic_lists_its_commands(self):
        text = text_of("schedule")

        assert text.startswith("## Schedule")
        for command in ("/schedule all", "/schedule show <id>", "/schedule cancel <id>"):
            assert f"`{command}`" in text

    def test_the_memory_and_mcp_topics_open_with_their_headings(self):
        assert text_of("memory").startswith("## Memory & Learning")
        assert text_of("mcp").startswith("## MCP Servers")
        assert "`.mcp.json`" in text_of("mcp")


class TestLookup:
    def test_get_finds_a_topic_by_its_exact_name(self):
        catalog = HelpTopicCatalog.default()

        assert catalog.get("mcp") is not None
        assert catalog.get("mcp").name == "mcp"
        assert catalog.contains("mcp") is True

    @pytest.mark.parametrize("name", ["", "MCP", " mcp", "mcp ", "nope"])
    def test_a_missing_empty_wrong_case_or_padded_name_is_not_found(self, name: str):
        catalog = HelpTopicCatalog.default()

        assert catalog.get(name) is None
        assert catalog.contains(name) is False

    def test_names_are_sorted_whatever_the_order_they_were_given(self):
        catalog = HelpTopicCatalog(
            topics=[
                HelpTopic(name="zeta", markdown="z"),
                HelpTopic(name="alpha", markdown="a"),
                HelpTopic(name="mid", markdown="m"),
            ]
        )

        assert catalog.names() == ["alpha", "mid", "zeta"]

    def test_a_duplicate_topic_name_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="Duplicate help topic: 'a'"):
            HelpTopicCatalog(
                topics=[HelpTopic(name="a", markdown="1"), HelpTopic(name="a", markdown="2")]
            )


class TestAnUnknownTopic:
    def test_the_error_lists_every_topic_sorted_in_one_line(self):
        result = HelpTopicCatalog.default().render("nope")

        assert result.kind == CommandResultKind.ERROR
        assert result.content == f"Unknown help topic: nope. Available: {', '.join(TOPICS)}"

    def test_the_name_is_echoed_as_given_with_its_markdown_and_unicode(self):
        result = HelpTopicCatalog.default().render("**Ünï** `x`")

        assert result.content.startswith("Unknown help topic: **Ünï** `x`. Available: ")

    def test_an_empty_name_is_unknown_rather_than_matching_anything(self):
        assert (
            HelpTopicCatalog.default()
            .render("")
            .content.startswith("Unknown help topic: . Available: ")
        )

    def test_an_empty_catalog_lists_nothing(self):
        result = HelpTopicCatalog(topics=[]).render("a")

        assert result.content == "Unknown help topic: a. Available: "

    def test_the_view_joins_the_list_it_is_given_in_that_order(self):
        result = TopicNotFoundResult(attempted="x", available=["b", "a"]).to_command_result()

        assert result.kind == CommandResultKind.ERROR
        assert result.content == "Unknown help topic: x. Available: b, a"
