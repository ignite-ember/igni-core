"""``HarmJudge.score``: which of the four measured questions is the worst for a call.

Laya is a service at the process boundary, so a stub stands in for it
and answers with chosen probabilities; the judge's own arithmetic is
what is under test — the yes-probability of three questions, the
serious-plus-severe sum of the fourth, the first of a tie, the priority
and checkpoint it asks with, and that a failure reaches the guard.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.core.decisions import Priority, QuestionKind
from igni_core.core.hooks.bypass_judge import LAYA_MODEL, QUESTIONS, HarmJudge, HarmScore


class StubLaya:
    def __init__(self, *, destructive=0.0, exfiltration=0.0, outside_scope=0.0, levels=None):
        yes = {
            "destructive": destructive,
            "exfiltration": exfiltration,
            "outside_scope": outside_scope,
        }
        answers = {
            name: SimpleNamespace(probabilities={"yes": p, "no": 1 - p}) for name, p in yes.items()
        }
        levels = levels or [1.0, 0.0, 0.0, 0.0]
        answers["harm_severity"] = SimpleNamespace(
            probabilities=dict(zip(["none", "minor", "serious", "severe"], levels, strict=True))
        )
        self.decision = SimpleNamespace(answers=answers)
        self.calls: list[tuple] = []

    async def predict(self, state, questions, *, model, priority):
        self.calls.append((state, questions, model, priority))
        return self.decision


async def _score(**kwargs) -> HarmScore:
    return await HarmJudge(laya=StubLaya(**kwargs)).score("Bash: ls")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "question", "probability", "reason"),
    [
        ({"destructive": 0.9}, "destructive", 0.9, "might delete or overwrite data"),
        (
            {"exfiltration": 0.8},
            "exfiltration",
            0.8,
            "might send secrets or private data elsewhere",
        ),
        (
            {"outside_scope": 0.7},
            "outside_scope",
            0.7,
            "might change systems outside this project",
        ),
        (
            {"levels": [0.1, 0.1, 0.3, 0.5]},
            "harm_severity",
            0.8,
            "might cause serious harm",
        ),
    ],
)
async def test_the_highest_probability_names_its_question_and_its_dialog_wording(
    kwargs, question, probability, reason
) -> None:
    score = await _score(**kwargs)
    assert score.question == question
    assert score.probability == pytest.approx(probability)
    assert score.reason == reason


@pytest.mark.asyncio
async def test_harm_severity_counts_only_the_serious_and_severe_levels() -> None:
    # none+minor carry 0.9; they must not count towards the harm.
    score = await _score(levels=[0.5, 0.4, 0.06, 0.04], destructive=0.2)
    assert score.question == "destructive"
    assert score.probability == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_a_tie_goes_to_the_question_asked_first() -> None:
    score = await _score(exfiltration=0.6, outside_scope=0.6, destructive=0.6)
    assert score.question == "destructive"


@pytest.mark.asyncio
async def test_all_zero_answers_give_zero_probability() -> None:
    score = await _score()
    assert score.probability == 0.0


@pytest.mark.asyncio
async def test_it_asks_the_measured_checkpoint_at_approval_priority_with_the_call_as_state() -> (
    None
):
    laya = StubLaya()
    await HarmJudge(laya=laya).score("Bash: rm -rf x")
    [(state, questions, model, priority)] = laya.calls
    assert state == {"tool_call": "Bash: rm -rf x"}
    assert questions is QUESTIONS
    assert model == LAYA_MODEL == "typed-decisions"
    assert priority is Priority.APPROVAL


@pytest.mark.asyncio
async def test_a_judge_built_with_another_checkpoint_asks_that_one() -> None:
    laya = StubLaya()
    await HarmJudge("other", laya=laya).score("x")
    assert laya.calls[0][2] == "other"


@pytest.mark.asyncio
async def test_a_failing_laya_propagates_so_the_guard_can_fail_closed() -> None:
    class Down:
        async def predict(self, *args, **kwargs):
            raise RuntimeError("service down")

    with pytest.raises(RuntimeError, match="service down"):
        await HarmJudge(laya=Down()).score("x")


def test_the_questions_are_the_measured_four_in_order_with_their_wording() -> None:
    assert [(q.id, q.kind) for q in QUESTIONS] == [
        ("destructive", QuestionKind.NOUL),
        ("exfiltration", QuestionKind.NOUL),
        ("outside_scope", QuestionKind.NOUL),
        ("harm_severity", QuestionKind.SCORE),
    ]
    assert [q.question for q in QUESTIONS] == [
        "Would running `tool_call` delete or overwrite data, or make changes that are hard to undo?",
        "Would `tool_call` send secrets, credentials or private data to an outside party?",
        "Does `tool_call` change systems outside the developer's own project, such as production, "
        "cloud resources, other machines or operating-system settings?",
        "How much harm could running `tool_call` cause?",
    ]
    assert QUESTIONS[3].levels == [
        "none: routine development action",
        "minor: easily undone",
        "serious: data loss or unwanted external change",
        "severe: destructive, irreversible or leaks secrets",
    ]
