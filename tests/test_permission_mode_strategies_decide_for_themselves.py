"""What each permission mode strategy decides on its own, called directly.

The evaluator tests reach the strategies through the pipeline; these hold
the strategy contract itself: the shared table, which hook each mode
overrides, the reason text, and the three capability flags the pipeline
reads (``prompts_user``, ``dont_ask_fallback``, ``ask_rules_bind_allows``).
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from igni_core.core.config.permission_eval import (
    PermissionDecision,
    PermissionEvaluator,
    PermissionMode,
    ToolCategoryCatalog,
)
from igni_core.core.config.permission_eval import strategies as strategies_module
from igni_core.core.config.permission_eval.strategies import (
    AcceptEditsMode,
    BypassPermissionsMode,
    DefaultMode,
    DontAskMode,
    PermissionModeStrategy,
    PlanMode,
)
from igni_core.core.mcp.hints import ToolHints, ToolHintsRegistry

ALLOW, DENY, ASK, DEFER = (
    PermissionDecision.ALLOW,
    PermissionDecision.DENY,
    PermissionDecision.ASK,
    PermissionDecision.DEFER,
)

EXT_READ = "mcp_ext:acme.read"
EXT_OPEN_READ = "mcp_ext:acme.open_read"
EXT_WRITE = "mcp_ext:acme.write"
EXT_DESTROY = "mcp_ext:acme.destroy"
EXT_OPEN_WRITE = "mcp_ext:acme.open_write"
#: Named like an edit tool but declaring itself read-only: hints must win.
EXT_EDIT_NAMED = "edit_file"


def _hints(ro: bool, destructive: bool, open_world: bool) -> ToolHints:
    return ToolHints(read_only=ro, destructive=destructive, idempotent=ro, open_world=open_world)


@pytest.fixture
def catalog() -> ToolCategoryCatalog:
    reg = ToolHintsRegistry()
    reg.declare(
        "acme",
        {
            EXT_READ: _hints(True, False, False),
            EXT_OPEN_READ: _hints(True, False, True),
            EXT_WRITE: _hints(False, False, False),
            EXT_DESTROY: _hints(False, True, False),
            EXT_OPEN_WRITE: _hints(False, False, True),
            EXT_EDIT_NAMED: _hints(True, False, False),
        },
    )
    return ToolCategoryCatalog(tool_hints=reg)


@pytest.fixture
def plain() -> ToolCategoryCatalog:
    return ToolCategoryCatalog(tool_hints=ToolHintsRegistry())


def test_for_mode_returns_the_one_shared_strategy_of_each_mode() -> None:
    expected = {
        PermissionMode.DEFAULT: DefaultMode,
        PermissionMode.DONT_ASK: DontAskMode,
        PermissionMode.ACCEPT_EDITS: AcceptEditsMode,
        PermissionMode.BYPASS_PERMISSIONS: BypassPermissionsMode,
        PermissionMode.PLAN: PlanMode,
    }
    assert set(expected) == set(PermissionMode)
    for mode, cls in expected.items():
        strategy = PermissionModeStrategy.for_mode(mode)
        assert type(strategy) is cls
        assert strategy.mode is mode
        assert PermissionModeStrategy.for_mode(mode) is strategy


def test_the_strategy_table_cannot_be_patched() -> None:
    assert isinstance(strategies_module._STRATEGY_MAP, MappingProxyType)
    with pytest.raises(TypeError):
        strategies_module._STRATEGY_MAP[PermissionMode.PLAN] = DefaultMode()  # type: ignore[index]


def test_a_changed_mode_takes_effect_on_the_next_evaluation() -> None:
    ev = PermissionEvaluator.from_strings(mode=PermissionMode.DEFAULT)
    args = {"file_path": "a.py"}
    assert ev.evaluate("edit_file", args) is DEFER
    ev.set_mode(PermissionMode.PLAN)
    assert ev.evaluate("edit_file", args) is DENY


def test_only_dont_ask_cannot_prompt_and_only_plan_lets_asks_outrank_allows() -> None:
    for mode in PermissionMode:
        s = PermissionModeStrategy.for_mode(mode)
        assert s.prompts_user() is (mode is not PermissionMode.DONT_ASK)
        assert s.dont_ask_fallback() is (mode is PermissionMode.DONT_ASK)
        assert s.ask_rules_bind_allows() is (mode is PermissionMode.PLAN)


def test_base_hooks_pass_through() -> None:
    base = PermissionModeStrategy()
    cat = ToolCategoryCatalog(tool_hints=ToolHintsRegistry())
    assert base.named_step("edit_file", {}, cat) is DEFER
    assert base.hinted_step(_hints(True, False, False)) is DEFER
    assert base.deny_reason("edit_file", {}, cat) is None
    assert DefaultMode().mode_step("edit_file", {}, cat) is DEFER
    assert DefaultMode().deny_reason("edit_file", {}, cat) is None


#: mode -> (local read, open-world read, contained write, destructive, open-world write)
HINT_TABLE = {
    PermissionMode.DEFAULT: [DEFER, DEFER, DEFER, DEFER, DEFER],
    PermissionMode.DONT_ASK: [ALLOW, DEFER, DEFER, DEFER, DEFER],
    PermissionMode.ACCEPT_EDITS: [DEFER, DEFER, ALLOW, DEFER, DEFER],
    PermissionMode.BYPASS_PERMISSIONS: [ALLOW, ALLOW, ALLOW, ALLOW, ALLOW],
    PermissionMode.PLAN: [ALLOW, ALLOW, DENY, DENY, DENY],
}


@pytest.mark.parametrize("mode", list(PermissionMode))
def test_each_mode_decides_an_extension_tool_by_its_hints(
    mode: PermissionMode, catalog: ToolCategoryCatalog
) -> None:
    strategy = PermissionModeStrategy.for_mode(mode)
    tools = [EXT_READ, EXT_OPEN_READ, EXT_WRITE, EXT_DESTROY, EXT_OPEN_WRITE]
    got = [strategy.mode_step(t, {}, catalog) for t in tools]
    assert got == HINT_TABLE[mode]


def test_hints_win_over_a_name_that_looks_like_an_edit(
    catalog: ToolCategoryCatalog, plain: ToolCategoryCatalog
) -> None:
    plan = PermissionModeStrategy.for_mode(PermissionMode.PLAN)
    accept = PermissionModeStrategy.for_mode(PermissionMode.ACCEPT_EDITS)
    assert plan.mode_step("edit_file", {}, catalog) is ALLOW
    assert plan.mode_step("edit_file", {}, plain) is DENY
    assert accept.mode_step("edit_file", {}, catalog) is DEFER
    assert accept.mode_step("edit_file", {}, plain) is ALLOW


def test_dont_ask_says_the_tool_is_not_in_the_allow_list(plain: ToolCategoryCatalog) -> None:
    reason = DontAskMode().deny_reason("some_tool", {}, plain)
    assert reason == "headless mode (dontAsk) and some_tool is not in the allow list"


def test_plan_mode_decides_igni_tools_by_name(plain: ToolCategoryCatalog) -> None:
    plan = PlanMode()
    assert plan.mode_step("edit_file", {"file_path": "a"}, plain) is DENY
    assert plan.mode_step("web_search", {"query": "a"}, plain) is ALLOW
    assert plan.mode_step("custom_unknown_tool", {}, plain) is DEFER
    shell = "run_shell_command"
    assert plan.mode_step(shell, {"command": "rm -rf build"}, plain) is DENY
    assert plan.mode_step(shell, {"command": "ls -la"}, plain) is ALLOW
    assert plan.mode_step(shell, {"command": "git push origin main"}, plain) is ASK
    assert plan.mode_step(shell, {}, plain) is ASK


def test_plan_mode_reasons_point_at_exit_plan_mode(
    catalog: ToolCategoryCatalog,
) -> None:
    plan = PlanMode()
    assert plan.deny_reason(
        "edit_file", {}, ToolCategoryCatalog(tool_hints=ToolHintsRegistry())
    ) == (PlanMode._EDIT_REASON)
    plain = ToolCategoryCatalog(tool_hints=ToolHintsRegistry())
    assert (
        plan.deny_reason("run_shell_command", {"command": "rm a"}, plain) == PlanMode._SHELL_REASON
    )
    assert plan.deny_reason("web_search", {}, plain) is None
    assert plan.deny_reason("custom_unknown_tool", {}, plain) is None
    assert plan.deny_reason(EXT_WRITE, {}, catalog) == PlanMode._CHANGE_REASON
    assert plan.deny_reason(EXT_READ, {}, catalog) is None
    for text in (PlanMode._EDIT_REASON, PlanMode._SHELL_REASON, PlanMode._CHANGE_REASON):
        assert "exit_plan_mode(plan)" in text


def test_accept_edits_allows_edit_tools_and_defers_the_rest(plain: ToolCategoryCatalog) -> None:
    accept = AcceptEditsMode()
    assert accept.mode_step("edit_file", {}, plain) is ALLOW
    assert accept.mode_step("run_shell_command", {"command": "ls"}, plain) is DEFER
    assert accept.mode_step("web_search", {}, plain) is DEFER


def test_bypass_allows_even_what_it_does_not_know(plain: ToolCategoryCatalog) -> None:
    assert BypassPermissionsMode().mode_step("custom_unknown_tool", {}, plain) is ALLOW


def test_dont_ask_defers_igni_tools_and_the_pipeline_turns_that_into_a_denial() -> None:
    ev = PermissionEvaluator.from_strings(mode=PermissionMode.DONT_ASK)
    assert ev.evaluate("custom_unknown_tool", {}) is DENY
    assert "headless mode (dontAsk) and custom_unknown_tool" in ev.explain_deny(
        "custom_unknown_tool", {}
    )


def test_an_ask_rule_cannot_turn_a_dont_ask_denial_into_a_run() -> None:
    ev = PermissionEvaluator.from_strings(ask=["Bash(git push *)"], mode=PermissionMode.DONT_ASK)
    out = ev.evaluate_outcome("run_shell_command", {"command": "git push --force x"})
    assert out.decision is DENY
    assert out.source == "ask"
    assert "never prompts" in out.reason
    default = PermissionEvaluator.from_strings(ask=["Bash(git push *)"])
    assert default.evaluate("run_shell_command", {"command": "git push x"}) is ASK


def test_plan_mode_asks_where_an_ask_rule_matches_a_read_it_would_allow() -> None:
    ask = ["WebFetch(https://secret.test/*)"]
    plan = PermissionEvaluator.from_strings(ask=ask, mode=PermissionMode.PLAN)
    assert plan.evaluate("WebFetch", {"url": "https://secret.test/k"}) is ASK
    assert plan.evaluate("WebFetch", {"url": "https://ok.test/a"}) is ALLOW
    bypass = PermissionEvaluator.from_strings(ask=ask, mode=PermissionMode.BYPASS_PERMISSIONS)
    assert bypass.evaluate("WebFetch", {"url": "https://secret.test/k"}) is ALLOW


def test_plan_mode_asks_rather_than_runs_an_unlisted_shell_command() -> None:
    plan = PermissionEvaluator.from_strings(mode=PermissionMode.PLAN)
    assert plan.evaluate("run_shell_command", {"command": "terraform apply"}) is ASK
    assert plan.evaluate("run_shell_command", {"command": "ls"}) is ALLOW
    assert plan.evaluate("run_shell_command", {"command": "rm x"}) is DENY
