"""The question igni asks Laya about a person's message, and its limits."""

from __future__ import annotations

from igni_core.core.decisions.schemas import DecisionQuestion, QuestionKind

MESSAGE_FIELD = "message"
"""The context field the question names."""

MAX_MESSAGE_CHARS = 280
"""The longest message, stripped, worth asking about: thanks are short, and Laya's queue stays clear."""

CONFIDENCE_THRESHOLD = 0.6
"""The least confidence in ``gratitude`` or ``compliment`` that counts."""

ANSWER_TIMEOUT_S = 60.0
"""How long an answer may take, a first checkpoint load included; a later one would be stale."""

APPRECIATION_QUESTION = DecisionQuestion(
    id="appreciation",
    kind=QuestionKind.CHOICE,
    question=f"Is `{MESSAGE_FIELD}` the user mainly thanking or praising the assistant?",
    options={
        "gratitude": "the user thanks the assistant",
        "compliment": "the user praises the assistant or its work",
        "neither": (
            "anything else: a request, question, instruction, complaint, "
            "or thanks that comes with a new request"
        ),
    },
)
"""Asked of Laya's general checkpoints, which route English and other languages themselves."""
