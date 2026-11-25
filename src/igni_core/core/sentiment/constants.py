"""The questions igni asks Laya about a person's message, and their limits."""

from __future__ import annotations

from igni_core.core.decisions.schemas import DecisionQuestion, QuestionKind

MESSAGE_FIELD = "message"
"""The context field the questions name."""

MAX_MESSAGE_CHARS = 1200
"""The longest message, stripped, worth asking about: under what Laya reads uncut in every script measured."""

POSITIVE_THRESHOLD = 0.6
"""The least probability of ``gratitude`` and ``compliment`` together that counts as positive."""

NEGATIVE_THRESHOLD = 0.6
"""The least probability of ``negative`` that counts as negative."""

INSULT_THRESHOLD = 0.6
"""The least probability of ``insult`` that counts as negative."""

ANSWER_TIMEOUT_S = 60.0
"""How long an answer may take, a first checkpoint load included; a later one would be stale."""

SENTIMENT_QUESTION = DecisionQuestion(
    id="sentiment",
    kind=QuestionKind.CHOICE,
    question=f"How does the user feel about the assistant in `{MESSAGE_FIELD}`?",
    options={
        "positive": "pleased with the assistant or its work: thanks, praise",
        "negative": (
            "displeased with the assistant or its last answer: an insult, frustration, "
            "saying it is wrong or not what was asked, correcting a mistake"
        ),
        "neutral": "anything else: a new request, a question, an instruction, chit-chat",
    },
)
"""Catches displeasure of any kind; on its own it reads plain thanks as neutral."""

GESTURE_QUESTION = DecisionQuestion(
    id="gesture",
    kind=QuestionKind.CHOICE,
    question=f"What is `{MESSAGE_FIELD}` mainly doing to the assistant?",
    options={
        "gratitude": "the user thanks the assistant",
        "compliment": "the user praises the assistant or its work",
        "insult": "the user insults, mocks or belittles the assistant",
        "neither": (
            "anything else: a request, question, instruction, complaint, "
            "or thanks that comes with a new request"
        ),
    },
)
"""Catches thanks and praise in every language measured, and insults."""
