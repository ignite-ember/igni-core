"""``orchestration``, ``agents``, ``skills``, ``rules``, ``hooks``, ``scheduler``, ``context``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.core.config.schemas import (
    AgentsConfig,
    ContextConfig,
    HooksConfig,
    OrchestrationConfig,
    RulesConfig,
    SchedulerConfig,
    SkillsConfig,
)
from igni_core.core.config.schemas.orchestration import GROUP_HOOK_MODES
from igni_core.core.paths import PROJECT_CONTEXT_FILE


def test_orchestration_limits_default_to_thirty_minutes_per_sub_team():
    assert OrchestrationConfig().model_dump() == {
        "max_nesting_depth": 5,
        "max_total_agents": 20,
        "sub_team_timeout": 1800,
        "max_task_iterations": 10,
        "generate_ephemeral": True,
        "max_ephemeral_per_session": 5,
        "auto_cleanup": True,
    }


def test_cross_tool_support_is_on_and_skills_trigger_the_editor_agent_by_default():
    assert AgentsConfig().cross_tool_support is True
    assert RulesConfig().cross_tool_support is True
    skills = SkillsConfig()
    assert (skills.cross_tool_support, skills.auto_trigger, skills.default_agent) == (
        True,
        True,
        "editor",
    )


def test_the_scheduler_polls_every_thirty_seconds_runs_one_task_at_a_time_for_five_minutes():
    scheduler = SchedulerConfig()
    assert (scheduler.poll_interval, scheduler.task_timeout, scheduler.max_concurrent) == (
        30,
        300,
        1,
    )


def test_group_hooks_run_by_default_and_the_modes_are_ordered_least_to_most_strict():
    assert HooksConfig().group == "run"
    assert HooksConfig().cross_tool_support is True
    assert GROUP_HOOK_MODES == ("run", "ask", "refuse")


@pytest.mark.parametrize("mode", GROUP_HOOK_MODES)
def test_each_group_hook_mode_is_accepted(mode):
    assert HooksConfig(group=mode).group == mode


@pytest.mark.parametrize("mode", ["", "Run", "deny", "never"])
def test_any_other_group_hook_mode_is_refused(mode):
    with pytest.raises(ValidationError):
        HooksConfig(group=mode)  # type: ignore[arg-type]


def test_the_context_block_reads_the_project_file_and_ignores_build_and_vcs_directories():
    context = ContextConfig()
    assert context.project_file == PROJECT_CONTEXT_FILE
    assert context.ignore_patterns == [
        "node_modules/",
        ".git/",
        "__pycache__/",
        "*.pyc",
        ".venv/",
        "dist/",
        "build/",
    ]


def test_ignore_patterns_are_not_shared_between_instances():
    first, second = ContextConfig(), ContextConfig()
    first.ignore_patterns.append("extra/")
    assert "extra/" not in second.ignore_patterns
    assert "extra/" not in ContextConfig().ignore_patterns
