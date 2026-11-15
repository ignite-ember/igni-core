"""Agno-agent construction helpers.

Small factory functions that turn igni ``Settings`` into
Agno constructor arguments. Extracted from
:mod:`igni_core.core.session.core` so the god-file has fewer
top-level defs and these building blocks can be tested and
imported independently.

Both helpers return ``None`` when the corresponding feature is
switched off in config — the caller passes the ``None`` through to
Agno's ``Agent(reasoning_tools=None, ...)`` / ``Agent(pre_hooks=None,
...)`` unchanged, and Agno treats it as "feature disabled."

They used to claim to degrade the same way when an Agno sub-package
was missing, via four ``except ImportError`` arms. None of them could
ever run: the four classes are imported at module scope below, so an
absent sub-package fails the *import of this module* long before any
handler is reached, and the constructors they wrapped import nothing
that can fail (``PIIDetectionGuardrail`` imports stdlib ``re``;
``PromptInjectionGuardrail`` and ``ReasoningTools`` import nothing;
``OpenAIModerationGuardrail`` defers ``from openai import OpenAI`` to
``check()``, so a missing OpenAI SDK raises at request time, inside
the guardrail, not here). Agno is a hard dependency
(``pyproject.toml``), so there is no partial-install case to degrade
for, and the handlers were only advertising a safety net that was not
attached.
"""

from __future__ import annotations

from agno.exceptions import CheckTrigger, InputCheckError
from agno.guardrails.openai import OpenAIModerationGuardrail
from agno.guardrails.pii import PIIDetectionGuardrail
from agno.guardrails.prompt_injection import PromptInjectionGuardrail
from agno.tools.reasoning import ReasoningTools

from igni_core.core.config.settings import Settings


def create_reasoning_tools(settings: Settings) -> ReasoningTools | None:
    """Return an Agno ``ReasoningTools`` instance, or ``None`` when
    reasoning is disabled."""
    if not settings.reasoning.enabled:
        return None
    return ReasoningTools(
        add_instructions=settings.reasoning.add_instructions,
        add_few_shot=settings.reasoning.add_few_shot,
    )


class FailClosedModerationGuardrail(OpenAIModerationGuardrail):
    """OpenAI moderation that refuses the turn when it cannot moderate it.

    Agno's pre-hook runner logs any exception other than
    ``InputCheckError`` and runs the turn anyway, so a missing OpenAI key
    or SDK, or an unreachable moderation API, let every turn through
    unmoderated with a traceback on stderr (campaign R086, R157). A
    person who switched moderation on asked for nothing unmoderated to
    reach the model: such a failure is an ``InputCheckError`` here.
    """

    def check(self, run_input):  # type: ignore[no-untyped-def]
        try:
            super().check(run_input)
        except InputCheckError:
            raise
        except Exception as exc:  # noqa: BLE001 — every failure to moderate refuses
            raise self._could_not_moderate(exc) from exc

    async def async_check(self, run_input):  # type: ignore[no-untyped-def]
        try:
            await super().async_check(run_input)
        except InputCheckError:
            raise
        except Exception as exc:  # noqa: BLE001 — every failure to moderate refuses
            raise self._could_not_moderate(exc) from exc

    @staticmethod
    def _could_not_moderate(exc: Exception) -> InputCheckError:
        return InputCheckError(
            "guardrails.moderation is on and the message could not be moderated "
            f"({type(exc).__name__}: {exc}); it was not sent. Set OPENAI_API_KEY "
            "(and install the openai package), or turn guardrails.moderation off.",
            check_trigger=CheckTrigger.INPUT_NOT_ALLOWED,
        )


def create_guardrails(settings: Settings) -> list | None:
    """Return a list of Agno guardrail ``pre_hooks`` from config, or
    ``None`` when no guardrail flag is enabled.

    Moderation is the one with an external dependency (the OpenAI SDK
    and an OpenAI key), and it does not surface here: the guardrail
    imports the SDK inside ``check()``, so an env without it builds a
    session fine and refuses the first moderated turn instead
    (:class:`FailClosedModerationGuardrail`).
    """
    hooks: list = []
    cfg = settings.guardrails

    if cfg.pii_detection:
        hooks.append(PIIDetectionGuardrail())

    if cfg.prompt_injection:
        hooks.append(PromptInjectionGuardrail())

    if cfg.moderation:
        hooks.append(FailClosedModerationGuardrail())

    return hooks if hooks else None
