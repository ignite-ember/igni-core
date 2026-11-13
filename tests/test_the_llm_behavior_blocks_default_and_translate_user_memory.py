"""``memory``, ``learning``, ``reasoning``, ``guardrails`` and ``evals`` defaults."""

from __future__ import annotations

from agno.learn.config import LearningMode, UserMemoryConfig

from igni_core.core.config.schemas import (
    EvalsConfig,
    GuardrailsConfig,
    LearningConfig,
    MemoryConfig,
    ReasoningConfig,
)


def test_memories_are_added_to_context_by_default():
    assert MemoryConfig().add_memories_to_context is True


def test_learning_is_on_with_only_the_agentic_user_memory_and_every_auto_extraction_off():
    learning = LearningConfig()
    assert learning.enabled is True
    assert learning.user_memory is True
    assert learning.user_profile is False
    assert learning.session_context is False
    assert learning.entity_memory is False
    assert learning.learned_knowledge is False


def test_user_memory_off_translates_to_exactly_false():
    assert LearningConfig(user_memory=False).to_user_memory_input() is False


def test_user_memory_on_translates_to_an_agent_driven_config_not_a_per_turn_one():
    translated = LearningConfig().to_user_memory_input()
    assert isinstance(translated, UserMemoryConfig)
    assert translated.mode == LearningMode.AGENTIC
    assert translated.enable_agent_tools is True
    assert translated.agent_can_update_memories is True


def test_reasoning_is_off_with_instructions_but_no_few_shot():
    reasoning = ReasoningConfig()
    assert (reasoning.enabled, reasoning.add_instructions, reasoning.add_few_shot) == (
        False,
        True,
        False,
    )


def test_only_pii_detection_is_on_among_the_guardrails():
    guardrails = GuardrailsConfig()
    assert (guardrails.pii_detection, guardrails.prompt_injection, guardrails.moderation) == (
        True,
        False,
        False,
    )


def test_evals_default_to_the_judge_three_iterations_threshold_seven_and_a_ten_minute_case():
    evals = EvalsConfig()
    assert evals.judge_model == "MiniMax-M2.7"
    assert evals.num_iterations == 3
    assert evals.accuracy_threshold == 7.0
    assert evals.timeout_per_case == 600


def test_an_integer_threshold_is_read_as_a_float():
    threshold = EvalsConfig(accuracy_threshold=8).accuracy_threshold
    assert threshold == 8.0
    assert isinstance(threshold, float)
