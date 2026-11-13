"""``create_reasoning_tools`` and ``create_guardrails``: every flag reaches Agno.

``test_agent_factory.py`` pins None-versus-built and the flags that are
easy to see. These pin the ones its comments skip: the few-shot flag
(visible in the instructions Agno builds), each single guardrail, and
the order of the hooks.
"""

from __future__ import annotations

from agno.guardrails.openai import OpenAIModerationGuardrail
from agno.guardrails.pii import PIIDetectionGuardrail
from agno.guardrails.prompt_injection import PromptInjectionGuardrail
from agno.tools.reasoning import ReasoningTools

from igni_core.core.config.schemas import GuardrailsConfig, ReasoningConfig
from igni_core.core.config.settings import Settings
from igni_core.core.session.agent_factory import create_guardrails, create_reasoning_tools


def _guardrails(**flags: bool) -> list | None:
    cfg = {"pii_detection": False, "prompt_injection": False, "moderation": False, **flags}
    return create_guardrails(Settings(guardrails=GuardrailsConfig(**cfg)))


def _reasoning(**flags: bool) -> ReasoningTools:
    tools = create_reasoning_tools(Settings(reasoning=ReasoningConfig(enabled=True, **flags)))
    assert isinstance(tools, ReasoningTools)
    return tools


def test_the_few_shot_flag_puts_the_examples_in_the_instructions() -> None:
    with_examples = _reasoning(add_few_shot=True)
    without = _reasoning(add_few_shot=False)

    assert ReasoningTools.FEW_SHOT_EXAMPLES in with_examples.instructions
    assert ReasoningTools.FEW_SHOT_EXAMPLES not in without.instructions


def test_the_add_instructions_flag_is_forwarded_both_ways() -> None:
    assert _reasoning(add_instructions=True).add_instructions is True
    assert _reasoning(add_instructions=False).add_instructions is False


def test_each_guardrail_flag_alone_builds_exactly_its_hook() -> None:
    (pii,) = _guardrails(pii_detection=True)
    (injection,) = _guardrails(prompt_injection=True)
    (moderation,) = _guardrails(moderation=True)

    assert type(pii) is PIIDetectionGuardrail
    assert type(injection) is PromptInjectionGuardrail
    assert type(moderation) is OpenAIModerationGuardrail


def test_every_guardrail_runs_in_the_order_pii_injection_moderation() -> None:
    hooks = _guardrails(pii_detection=True, prompt_injection=True, moderation=True)

    assert [type(h) for h in hooks or []] == [
        PIIDetectionGuardrail,
        PromptInjectionGuardrail,
        OpenAIModerationGuardrail,
    ]


def test_two_flags_keep_the_same_relative_order() -> None:
    hooks = _guardrails(prompt_injection=True, moderation=True)

    assert [type(h) for h in hooks or []] == [PromptInjectionGuardrail, OpenAIModerationGuardrail]
