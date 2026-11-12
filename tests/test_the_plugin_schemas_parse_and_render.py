"""The plugin schemas' parsing, rendering and defaults.

``/plugin ...`` verbs hand their raw tokens to one ``*Args.parse`` each and
branch on whether they got the args or the usage line; the marketplace panel
and the slash command render a bulk refresh from one result; a previewed
plugin is shown as ``<url> [<subdir>]`` and read back from it. What a person
sees (the usage lines, the refresh summary and its markdown) and what the
verbs receive (target, ref, name) are pinned here, through the public
classmethods.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.backend.plugin_schemas import (
    AddMarketplaceResult,
    ArgsParseError,
    InstallArgs,
    InstallResult,
    MarketplaceAddArgs,
    MarketplaceRefreshArgs,
    MarketplaceRefreshFailure,
    MarketplaceRefreshResult,
    MarketplaceRemoveArgs,
    PluginAgentInfo,
    PluginContents,
    PluginHookInfo,
    PluginMCPServerInfo,
    PluginPreviewKey,
    PluginPreviewSource,
    PluginSkillInfo,
    PluginsToggleArgs,
    PluginToolInfo,
    RefreshOneResult,
    RemoveArgs,
    RemoveResult,
    ResolvedInstallRef,
    UpdateArgs,
    UpdateResult,
)

# ── The preview source ──────────────────────────────────────────────


def test_a_url_with_a_bracketed_folder_is_split_into_url_and_subdir():
    source = PluginPreviewSource.parse("https://git.example/p.git [plugins/one]")

    assert (source.url, source.subdir) == ("https://git.example/p.git", "plugins/one")


def test_a_bare_url_has_no_subdir():
    source = PluginPreviewSource.parse("https://git.example/p.git")

    assert (source.url, source.subdir) == ("https://git.example/p.git", "")


def test_whitespace_around_the_source_and_inside_the_brackets_is_trimmed():
    source = PluginPreviewSource.parse("  https://git.example/p.git    [ plugins/one ]   ")

    assert (source.url, source.subdir) == ("https://git.example/p.git", "plugins/one")


def test_empty_brackets_are_part_of_the_url_not_a_subdir():
    source = PluginPreviewSource.parse("https://git.example/p.git []")

    assert (source.url, source.subdir) == ("https://git.example/p.git []", "")


def test_a_bracket_glued_to_the_url_is_not_a_subdir():
    source = PluginPreviewSource.parse("https://git.example/p.git[sub]")

    assert (source.url, source.subdir) == ("https://git.example/p.git[sub]", "")


def test_a_subdir_with_spaces_and_unicode_survives():
    source = PluginPreviewSource.parse("https://git.example/p.git [my plugins/übung]")

    assert source.subdir == "my plugins/übung"


@pytest.mark.parametrize(
    ("url", "subdir", "shown"),
    [
        ("https://git.example/p.git", "plugins/one", "https://git.example/p.git [plugins/one]"),
        ("https://git.example/p.git", "", "https://git.example/p.git"),
    ],
)
def test_a_source_is_shown_as_url_and_bracket_and_read_back_the_same(url, subdir, shown):
    source = PluginPreviewSource(url=url, subdir=subdir)

    assert source.display() == shown
    assert PluginPreviewSource.parse(shown) == source


def test_a_preview_key_is_a_value_usable_as_a_dict_key_and_cannot_change():
    key = PluginPreviewKey(clone_url="https://git.example/p.git", branch="main", subdir="a")
    same = PluginPreviewKey(clone_url="https://git.example/p.git", branch="main", subdir="a")
    cache = {key: "cached"}

    assert cache[same] == "cached"
    assert PluginPreviewKey(clone_url="https://git.example/p.git") != key
    assert (PluginPreviewKey(clone_url="u").branch, PluginPreviewKey(clone_url="u").subdir) == (
        "",
        "",
    )
    with pytest.raises(ValidationError):
        key.branch = "dev"  # type: ignore[misc]


# ── The inventory ───────────────────────────────────────────────────


def test_an_error_inventory_has_the_message_and_nothing_else():
    contents = PluginContents.error_result("git clone failed")

    assert contents.error == "git clone failed"
    assert (contents.skills, contents.agents, contents.hooks) == ([], [], [])
    assert (contents.mcp_servers, contents.tools) == ([], [])
    assert contents.readme == ""
    assert contents.preview_source is None


def test_an_empty_inventory_defaults_to_no_error_and_no_items():
    contents = PluginContents()

    assert contents.error == ""
    assert contents.model_dump(exclude={"error"}) == PluginContents.error_result("").model_dump(
        exclude={"error"}
    )


# ── The bulk refresh result ─────────────────────────────────────────


def _failed(name: str, reason: str) -> MarketplaceRefreshFailure:
    return MarketplaceRefreshFailure(name=name, reason=reason)


def test_a_refresh_of_nothing_says_there_was_nothing_to_refresh():
    result = MarketplaceRefreshResult()

    assert result.is_empty()
    assert result.summary_line() == "No marketplaces to refresh."


def test_a_refresh_with_only_successes_counts_them():
    result = MarketplaceRefreshResult(ok=["a", "b"])

    assert not result.is_empty()
    assert result.summary_line() == "Refreshed 2 ok"


def test_a_refresh_with_failures_names_each_one_with_its_reason():
    result = MarketplaceRefreshResult(
        ok=["a"], failed=[_failed("b", "timeout"), _failed("c", "404, gone")]
    )

    assert result.summary_line() == "Refreshed 1 ok; 2 failed: b (timeout), c (404, gone)"


def test_a_refresh_with_only_failures_is_not_empty_and_counts_zero_ok():
    result = MarketplaceRefreshResult(failed=[_failed("b", "boom")])

    assert not result.is_empty()
    assert result.summary_line() == "Refreshed 0 ok; 1 failed: b (boom)"


def test_the_refresh_markdown_lists_successes_first_then_failures():
    result = MarketplaceRefreshResult(ok=["a", "b"], failed=[_failed("c", "boom")])

    assert result.to_markdown() == ("## Marketplace refresh\n- a: ok\n- b: ok\n- c: failed (boom)")


def test_the_refresh_markdown_of_nothing_is_just_the_header():
    assert MarketplaceRefreshResult().to_markdown() == "## Marketplace refresh"


# ── Slash-command arguments ─────────────────────────────────────────

INSTALL_USAGE = "Usage: /plugin install <git-url|@marketplace/plugin> [--ref <ref>]"


def test_install_takes_a_target_and_an_optional_ref_in_either_order():
    assert InstallArgs.parse(["@m/p"]) == InstallArgs(target="@m/p", ref=None)
    assert InstallArgs.parse(["@m/p", "--ref", "v2"]) == InstallArgs(target="@m/p", ref="v2")
    assert InstallArgs.parse(["--ref", "v2", "@m/p"]) == InstallArgs(target="@m/p", ref="v2")


def test_install_with_the_ref_given_twice_keeps_the_last():
    parsed = InstallArgs.parse(["--ref", "a", "t", "--ref", "b"])

    assert parsed == InstallArgs(target="t", ref="b")


@pytest.mark.parametrize(
    "tokens",
    [[], ["a", "b"], ["--ref", "v2"], ["t", "--ref"]],
    ids=["nothing", "two-targets", "ref-without-target", "dangling-ref-counts-as-positional"],
)
def test_install_without_exactly_one_target_is_the_usage_line(tokens):
    assert InstallArgs.parse(tokens) == ArgsParseError(message=INSTALL_USAGE)


def test_update_takes_a_name_and_an_optional_ref():
    assert UpdateArgs.parse(["p"]) == UpdateArgs(name="p", ref=None)
    assert UpdateArgs.parse(["p", "--ref", "main"]) == UpdateArgs(name="p", ref="main")


@pytest.mark.parametrize("tokens", [[], ["a", "b"], ["--ref", "x"], ["p", "--ref"]])
def test_update_without_exactly_one_name_is_the_usage_line(tokens):
    assert UpdateArgs.parse(tokens) == ArgsParseError(
        message="Usage: /plugin update <name> [--ref <ref>]"
    )


def test_remove_takes_one_name_and_drops_a_ref_without_error():
    assert RemoveArgs.parse(["p"]) == RemoveArgs(name="p")
    assert RemoveArgs.parse(["p", "--ref", "v1"]) == RemoveArgs(name="p")


@pytest.mark.parametrize("tokens", [[], ["a", "b"], ["p", "--ref"]])
def test_remove_without_exactly_one_name_is_the_usage_line(tokens):
    assert RemoveArgs.parse(tokens) == ArgsParseError(message="Usage: /plugin remove <name>")


def test_marketplace_add_takes_exactly_one_url():
    assert MarketplaceAddArgs.parse(["https://x/y.git"]) == MarketplaceAddArgs(
        url="https://x/y.git"
    )
    for tokens in ([], ["a", "b"]):
        assert MarketplaceAddArgs.parse(tokens) == ArgsParseError(
            message="Usage: /plugin marketplace add <git-url>"
        )


def test_marketplace_remove_takes_exactly_one_name():
    assert MarketplaceRemoveArgs.parse(["m"]) == MarketplaceRemoveArgs(name="m")
    for tokens in ([], ["a", "b"]):
        assert MarketplaceRemoveArgs.parse(tokens) == ArgsParseError(
            message="Usage: /plugin marketplace remove <name>"
        )


def test_marketplace_refresh_with_no_name_means_all_and_with_one_names_it():
    assert MarketplaceRefreshArgs.parse([]) == MarketplaceRefreshArgs(name="")
    assert MarketplaceRefreshArgs.parse(["m"]) == MarketplaceRefreshArgs(name="m")
    assert MarketplaceRefreshArgs.parse(["a", "b"]) == ArgsParseError(
        message="Usage: /plugin marketplace refresh [<name>]"
    )


def test_a_plugins_toggle_name_is_trimmed():
    assert PluginsToggleArgs.parse("enable", "  my-plugin \n") == PluginsToggleArgs(
        name="my-plugin"
    )


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_a_plugins_toggle_without_a_name_is_the_usage_line_for_that_subcommand(raw):
    assert PluginsToggleArgs.parse("disable", raw) == ArgsParseError(
        message="Usage: /plugins disable <plugin-name>"
    )


# ── Gateway outcomes ────────────────────────────────────────────────


def test_a_gateway_outcome_carries_empty_defaults_beside_its_ok_flag():
    assert InstallResult(ok=True) == InstallResult(ok=True, name="", version="", error="")
    assert UpdateResult(ok=True).sha == ""
    assert RemoveResult(ok=False, error="gone").error == "gone"
    assert AddMarketplaceResult(ok=True).plugin_count == 0
    assert ResolvedInstallRef(url="u") == ResolvedInstallRef(url="u", subdir=None, ref=None)


def test_a_missing_marketplace_is_told_apart_from_a_failed_refresh():
    missing = RefreshOneResult(ok=False, not_found=True, error="no such marketplace")
    failed = RefreshOneResult(ok=False, error="git error: timeout")

    assert missing.not_found and not failed.not_found
    assert RefreshOneResult(ok=True).not_found is False


@pytest.mark.parametrize(
    "model",
    [
        PluginPreviewSource,
        PluginSkillInfo,
        PluginAgentInfo,
        PluginHookInfo,
        PluginMCPServerInfo,
        PluginToolInfo,
        PluginContents,
    ],
)
def test_every_field_an_outside_developer_reads_in_the_inventory_is_described(model):
    undescribed = [name for name, f in model.model_fields.items() if not f.description]

    assert undescribed == []
    assert model.__doc__


def test_the_inventory_wire_shape_is_what_the_sdks_read():
    assert list(PluginContents.model_fields) == [
        "name",
        "root_path",
        "skills",
        "agents",
        "hooks",
        "mcp_servers",
        "tools",
        "readme",
        "error",
        "preview_source",
    ]
    assert list(PluginHookInfo.model_fields) == ["event", "count"]
    assert list(PluginMCPServerInfo.model_fields) == ["name", "transport", "command"]
