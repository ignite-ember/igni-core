"""The questions igni asks Laya about a person's message alone, and their limits."""

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

NEGATIVE_REASON_QUESTION = DecisionQuestion(
    id="negative_reason",
    kind=QuestionKind.CHOICE,
    question=f"If the user in `{MESSAGE_FIELD}` is displeased, what is wrong with the assistant's work?",
    options={
        "wrong_or_incorrect": "the answer or code is wrong",
        "misunderstood_request": "the assistant misunderstood what was asked",
        "incomplete": "the work is unfinished or missing parts",
        "ignored_instruction": "the assistant ignored an instruction",
        "unwanted_action": "the assistant deleted, overwrote or ran something unwanted",
        "too_slow": "it is too slow",
        "wrong_length": "the answer is too long or too short",
        "tone": "the tone or manner is wrong",
        "refused_or_gave_up": "the assistant refused or gave up",
        "other": "something else, or the user is not displeased",
    },
)
"""Read on English messages; the multilingual checkpoint answers it poorly."""

POSITIVE_REASON_QUESTION = DecisionQuestion(
    id="positive_reason",
    kind=QuestionKind.CHOICE,
    question=f"If the user in `{MESSAGE_FIELD}` is pleased, what pleased them?",
    options={
        "solved_or_correct": "the problem is solved or the answer is right",
        "fast": "it was fast",
        "clear_explanation": "the explanation was clear",
        "beyond_expectations": "it went beyond what was asked",
        "other": "something else, or the user is not pleased",
    },
)
"""Read on English messages; bare thanks lean to ``beyond_expectations``."""

REPEAT_QUESTION = DecisionQuestion(
    id="repeat",
    kind=QuestionKind.NOUL,
    question=f"Does `{MESSAGE_FIELD}` say the same problem happened again?",
)
"""A weak signal: it also says yes to some complaints that are not repeats."""

REDO_QUESTION = DecisionQuestion(
    id="redo_requested",
    kind=QuestionKind.NOUL,
    question=f"Does `{MESSAGE_FIELD}` ask the assistant to redo or undo something?",
)
"""Whether the person asks for a redo or an undo."""

TASK_DONE_QUESTION = DecisionQuestion(
    id="task_done",
    kind=QuestionKind.NOUL,
    question=f"Does `{MESSAGE_FIELD}` say the task is finished or works?",
)
"""Whether the person says the task is done."""

INTENSITY_QUESTION = DecisionQuestion(
    id="intensity",
    kind=QuestionKind.SCORE,
    question=f"How strong is the feeling in `{MESSAGE_FIELD}`?",
    levels=["none", "mild", "strong", "angry"],
)
"""A weak signal, lowest level first."""

ASPECT_QUESTIONS = [
    NEGATIVE_REASON_QUESTION,
    POSITIVE_REASON_QUESTION,
    REPEAT_QUESTION,
    REDO_QUESTION,
    TASK_DONE_QUESTION,
    INTENSITY_QUESTION,
]
"""Asked with the verdict's two questions, in the same call."""
