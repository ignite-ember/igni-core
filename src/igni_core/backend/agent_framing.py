"""What the agent is told around a message it did not get from the person's keyboard.

Two framings, both ``<system-context>`` blocks the windows strip from
display, so the chat bubble shows only the text itself:

* :class:`ExtensionMessage` — a message an extension posted.
* :class:`PlanResearchHint` — the one-shot nudge after ``/plan``.
"""

from __future__ import annotations

from html import escape


class ExtensionMessage:
    """How a message an extension posts reads to the agent.

    The agent must never take it for the person's: it says who sent it,
    in a block the windows strip from display (``<system-context>``),
    around the text wrapped in a tag the web client reads back on
    reload to label the bubble (``chat/model.ts``).
    """

    TAG = "extension-message"

    @classmethod
    def wrap(cls, extension_id: str, text: str) -> str:
        # A manifest id never holds a quote or a bracket, so for a real id
        # this changes nothing; an id that does could otherwise close the
        # attribute or the block and write the agent a context of its own.
        sender = escape(extension_id, quote=True)
        return (
            "<system-context>\n"
            f"The message below was posted by the igni extension '{sender}', "
            "not typed by the person. Answer it in the chat as you would answer them.\n"
            "</system-context>\n"
            f'<{cls.TAG} from="{sender}">\n{text}\n</{cls.TAG}>'
        )


class PlanResearchHint:
    """One-shot ``/plan`` → ``enter_plan_mode`` nudge.

    When the person just typed ``/plan`` (the slash command armed
    :meth:`PlanCoordinator.arm_research`), the next request is
    prefixed with an instruction to spawn the ``plan_researcher``
    sub-agent on that exact request. The caller consumes the armed
    flag, so later turns in the same plan-mode session do not get it.
    """

    TEXT = (
        "<system-context>\n"
        "Plan mode was just entered via the user's /plan slash "
        "command. BEFORE doing any other work, call "
        "`enter_plan_mode(task=...)` with the user's request "
        "below as the ``task`` argument so the plan_researcher "
        "sub-agent runs first and produces a structured "
        "Findings + Proposed Plan + Open Questions report. "
        "Do not skip this step — the researcher's report is "
        "what makes the eventual ``exit_plan_mode`` call "
        "grounded enough to pass the confidence validator.\n"
        "</system-context>\n\n"
    )

    @classmethod
    def wrap(cls, text: str) -> str:
        return cls.TEXT + text
