"""SECURITY.md §9 describes the guardrails as they behave.

It said PII detection "flags PII so the agent can redact or warn", that
prompt-injection detection covers tool output, and that "if a guardrail
triggers, the agent is informed and can adjust". Every guardrail is an
agno pre-hook on the person's message: a trigger raises
``InputCheckError``, the turn is refused before any model call, and the
agent never sees it (campaign R086, R157).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agno.exceptions import InputCheckError
from agno.run.agent import RunInput

from igni_core.core.config.schemas import GuardrailsConfig
from igni_core.core.config.settings import Settings
from igni_core.core.session.agent_factory import create_guardrails

_ROOT = Path(__file__).resolve().parent.parent


def test_pii_is_on_by_default_and_refuses_the_message():
    assert GuardrailsConfig().pii_detection is True
    (pii,) = create_guardrails(Settings())
    with pytest.raises(InputCheckError):
        pii.check(RunInput(input_content="mail me at jane.doe@example.com"))


def test_section_nine_says_a_trigger_refuses_and_the_agent_is_not_told():
    text = (_ROOT / "docs/SECURITY.md").read_text()
    section = text.split("### 9. Guardrails", 1)[1].split("\n### ", 1)[0]
    assert "flags PII so the agent can redact or warn" not in section
    assert "the agent is informed and can adjust" not in section
    assert "injection attempts in user input and tool output" not in section
    assert "refuses the whole message" in section


def test_the_config_comments_do_not_say_flag():
    assert "flag PII in prompts" not in (_ROOT / "QUICKSTART.md").read_text()
    assert "# Detect PII in prompts" not in (_ROOT / "docs/CONFIGURATION.md").read_text()
