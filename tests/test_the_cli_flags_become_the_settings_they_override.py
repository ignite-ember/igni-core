"""``CliOverrides`` turns one invocation's flags into ``Settings`` overrides.

Only what a flag set reaches the payload, so the deep-merge never
overwrites a lower tier with an unset value, and the stricter of two
permission flags wins.
"""

from __future__ import annotations

from igni_core.cli.options import CliOptions
from igni_core.core.config.cli_overrides import CliOverrides


def _payload(**flags) -> dict:
    return CliOverrides.from_options(CliOptions(**flags)).to_payload()


def test_no_flag_sets_nothing() -> None:
    overrides = CliOverrides.from_options(CliOptions())
    assert overrides.to_payload() == {}
    assert overrides.is_empty()


def test_a_flag_makes_the_overrides_non_empty() -> None:
    assert not CliOverrides.from_options(CliOptions(quiet=True)).is_empty()


def test_model_sets_the_default_model_and_an_empty_one_is_ignored() -> None:
    assert _payload(model="fast") == {"models": {"default": "fast"}}
    assert _payload(model="") == {}


def test_verbose_shows_routing_and_reasoning() -> None:
    assert _payload(verbose=True) == {"display": {"show_routing": True, "show_reasoning": True}}


def test_quiet_hides_tool_calls_and_routing() -> None:
    assert _payload(quiet=True) == {"display": {"show_tool_calls": False, "show_routing": False}}


def test_quiet_beats_verbose_on_routing_but_reasoning_stays_on() -> None:
    assert _payload(verbose=True, quiet=True) == {
        "display": {"show_routing": False, "show_reasoning": True, "show_tool_calls": False}
    }


def test_auto_approve_bypasses_and_allows_every_risky_category() -> None:
    assert _payload(auto_approve=True) == {
        "permissions": {
            "mode": "bypassPermissions",
            "file_write": "allow",
            "shell_execute": "allow",
            "git_push": "allow",
            "git_destructive": "allow",
        }
    }


def test_accept_edits_allows_file_writes_only() -> None:
    assert _payload(accept_edits=True) == {
        "permissions": {"mode": "acceptEdits", "file_write": "allow"}
    }


def test_read_only_plans_denies_writes_and_leaves_the_shell_open() -> None:
    perms = _payload(read_only=True)["permissions"]
    assert perms == {"mode": "plan", "file_write": "deny", "read_only": True}


def test_read_only_wins_over_accept_edits() -> None:
    perms = _payload(accept_edits=True, read_only=True)["permissions"]
    assert perms["mode"] == "plan"
    assert perms["file_write"] == "deny"
    assert perms["read_only"] is True


def test_strict_denies_the_risky_categories_and_asks_before_a_group_hook() -> None:
    assert _payload(strict=True) == {
        "permissions": {
            "mode": "dontAsk",
            "file_write": "deny",
            "shell_execute": "deny",
            "git_push": "deny",
            "git_destructive": "deny",
        },
        "hooks": {"group": "ask"},
    }


def test_strict_wins_over_auto_approve() -> None:
    perms = _payload(auto_approve=True, strict=True)["permissions"]
    assert perms["mode"] == "dontAsk"
    assert {perms[k] for k in ("file_write", "shell_execute", "git_push", "git_destructive")} == {
        "deny"
    }


def test_no_web_denies_search_and_fetch_without_setting_a_mode() -> None:
    assert _payload(no_web=True) == {"permissions": {"web_search": "deny", "web_fetch": "deny"}}


def test_read_only_false_is_not_an_override() -> None:
    assert "permissions" not in _payload(model="x")
