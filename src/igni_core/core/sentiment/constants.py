"""The question igni asks Laya about a person's message, and its limits."""

from __future__ import annotations

from igni_core.core.decisions.schemas import DecisionQuestion, QuestionKind

MESSAGE_FIELD = "message"
"""The context field the question names."""

MAX_MESSAGE_CHARS = 280
"""The longest message, stripped, worth asking about: thanks and insults are short, and Laya's queue stays clear."""

APPRECIATION_THRESHOLD = 0.6
"""The least probability of ``gratitude`` and ``compliment`` together that counts as either."""

INSULT_THRESHOLD = 0.6
"""The least probability of ``insult`` that counts."""

ANSWER_TIMEOUT_S = 60.0
"""How long an answer may take, a first checkpoint load included; a later one would be stale."""

SENTIMENT_QUESTION = DecisionQuestion(
    id="sentiment",
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
"""Asked of Laya's general checkpoints, which route English and other languages themselves."""
