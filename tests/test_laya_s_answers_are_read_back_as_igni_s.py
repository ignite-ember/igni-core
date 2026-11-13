"""Laya's own shapes: how a question is put to it and how its result is read back.

Laya says more than igni reads; these pin what igni takes from it for
each kind of question, where the answer is a tie or a boundary, and when
a result Laya cut is refused.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.core.decisions.laya_wire import (
    LayaAnswer,
    LayaBatchItem,
    LayaQuestion,
    LayaQuestions,
    LayaResult,
)
from igni_core.core.decisions.schemas import DecisionQuestion, DecisionTooLong

CHOICE = DecisionQuestion.model_validate(
    {
        "id": "kind",
        "kind": "choice",
        "question": "What is `text`?",
        "options": {"bug": "a bug report", "feature": ""},
    }
)
SCORE = DecisionQuestion.model_validate(
    {"id": "sev", "kind": "score", "question": "How bad?", "levels": ["low", "mid", "high"]}
)
NOUL = DecisionQuestion.model_validate({"id": "urgent", "kind": "noul", "question": "Urgent?"})


def result(answers: dict[str, Any], **rest: Any) -> LayaResult:
    return LayaResult.model_validate({"answers": answers} | rest)


class TestAQuestionInLayaSShape:
    def test_a_choice_carries_label_to_description_and_an_empty_one_is_none(self) -> None:
        wire = LayaQuestion.of(CHOICE)
        assert wire.type == "choice"
        assert wire.instructions == "What is `text`?"
        assert wire.criteria == {"bug": "a bug report", "feature": None}

    def test_a_score_carries_its_levels_in_order(self) -> None:
        assert LayaQuestion.of(SCORE).criteria == ["low", "mid", "high"]

    def test_a_noul_carries_no_criteria(self) -> None:
        assert LayaQuestion.of(NOUL).criteria is None

    def test_the_router_is_handed_questions_by_id_and_a_noul_has_no_criteria(self) -> None:
        wire = LayaQuestions.of([CHOICE, NOUL]).wire()
        assert wire == {
            "kind": {
                "type": "choice",
                "instructions": "What is `text`?",
                "criteria": {"bug": "a bug report", "feature": None},
            },
            "urgent": {"type": "noul", "instructions": "Urgent?"},
        }

    def test_a_batch_item_is_the_state_and_the_questions(self) -> None:
        item = LayaBatchItem(state={"text": "it crashes"}, questions=LayaQuestions.of([NOUL]))
        assert item.wire() == {
            "state": {"text": "it crashes"},
            "questions": {"urgent": {"type": "noul", "instructions": "Urgent?"}},
        }

    def test_an_unknown_field_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            LayaQuestion.model_validate({"type": "noul", "instructions": "x", "extra": 1})
        with pytest.raises(ValidationError):
            LayaBatchItem.model_validate({"state": {"a": "b"}, "questions": {}, "extra": 1})


class TestAYesNoAnswer:
    def test_above_a_half_is_yes_with_its_probability(self) -> None:
        answer = LayaAnswer(noul=0.8).read(NOUL)
        assert answer.answer == "yes"
        assert answer.confidence == pytest.approx(0.8)
        assert answer.probabilities == pytest.approx({"yes": 0.8, "no": 0.2})

    def test_below_a_half_is_no_with_the_other_probability(self) -> None:
        answer = LayaAnswer(noul=0.1).read(NOUL)
        assert (answer.answer, answer.confidence) == ("no", pytest.approx(0.9))

    def test_exactly_a_half_is_yes(self) -> None:
        answer = LayaAnswer(noul=0.5).read(NOUL)
        assert (answer.answer, answer.confidence) == ("yes", 0.5)

    def test_an_answer_without_its_probability_is_not_read(self) -> None:
        with pytest.raises(KeyError, match="urgent"):
            LayaAnswer().read(NOUL)


class TestAScoreAnswer:
    def test_the_likeliest_level_is_the_answer(self) -> None:
        answer = LayaAnswer(score=1.6, probabilities={"0": 0.1, "1": 0.3, "2": 0.6}).read(SCORE)
        assert answer.answer == "high"
        assert answer.confidence == 0.6
        assert answer.probabilities == {"low": 0.1, "mid": 0.3, "high": 0.6}
        assert answer.score == 1.6

    def test_probabilities_may_come_as_a_list_by_level(self) -> None:
        answer = LayaAnswer(score=0.2, probabilities=[0.7, 0.2, 0.1]).read(SCORE)
        assert (answer.answer, answer.confidence) == ("low", 0.7)

    def test_a_level_it_gives_no_probability_for_has_none(self) -> None:
        answer = LayaAnswer(score=1.0, probabilities={"1": 0.9}).read(SCORE)
        assert answer.probabilities == {"low": 0.0, "mid": 0.9, "high": 0.0}
        assert answer.answer == "mid"

    def test_a_tie_goes_to_the_lower_level(self) -> None:
        answer = LayaAnswer(score=0.5, probabilities=[0.4, 0.4, 0.2]).read(SCORE)
        assert answer.answer == "low"

    def test_a_missing_score_is_zero(self) -> None:
        assert LayaAnswer(probabilities=[0.5, 0.3, 0.2]).read(SCORE).score == 0.0


class TestAChoiceAnswer:
    def test_the_label_chosen_with_its_own_probability(self) -> None:
        answer = LayaAnswer(choice="feature", probabilities={"bug": 0.25, "feature": 0.75}).read(
            CHOICE
        )
        assert (answer.answer, answer.confidence) == ("feature", 0.75)
        assert answer.probabilities == {"bug": 0.25, "feature": 0.75}
        assert answer.score is None

    def test_a_label_without_a_probability_is_not_confident(self) -> None:
        answer = LayaAnswer(choice="bug", probabilities={"feature": 1.0}).read(CHOICE)
        assert (answer.answer, answer.confidence) == ("bug", 0.0)


class TestAResultRead:
    def answers(self) -> dict[str, Any]:
        return {
            "kind": {"choice": "bug", "probabilities": {"bug": 0.9, "feature": 0.1}},
            "urgent": {"noul": 0.7},
        }

    def test_each_question_is_answered_by_id_and_the_routed_model_is_named(self) -> None:
        decision = result(self.answers(), routing={"model": "english"}).decision([CHOICE, NOUL])
        assert list(decision.answers) == ["kind", "urgent"]
        assert decision.answers["kind"].answer == "bug"
        assert decision.answers["urgent"].answer == "yes"
        assert decision.model == "english"

    def test_no_routing_is_an_empty_model_name(self) -> None:
        assert result(self.answers()).decision([NOUL]).model == ""
        assert result(self.answers(), routing={"model": None}).decision([NOUL]).model == ""

    def test_what_laya_says_beyond_this_is_ignored(self) -> None:
        answers = {"urgent": {"type": "noul", "noul": 0.9, "novel": 1}}
        decision = result(answers, extra="x", usage={"other": 1}).decision([NOUL])
        assert decision.answers["urgent"].answer == "yes"

    def test_a_missing_answer_is_a_key_error_for_the_asker_to_name(self) -> None:
        with pytest.raises(KeyError, match="kind"):
            result({"urgent": {"noul": 0.7}}).decision([CHOICE, NOUL])


class TestAResultLayaCut:
    @pytest.mark.parametrize(
        "usage",
        [
            {"truncated": 1, "state_tokens_dropped": 40},
            {"truncated": 3},
            {"truncated_questions": ["urgent"]},
        ],
    )
    def test_it_is_refused_naming_what_was_dropped(self, usage: dict[str, Any]) -> None:
        cut = result({"urgent": {"noul": 0.9}}, usage=usage)
        with pytest.raises(DecisionTooLong, match=f"{usage.get('state_tokens_dropped', 0)} tokens"):
            cut.decision([NOUL])

    def test_it_is_answered_when_the_caller_measured_on_what_is_read(self) -> None:
        cut = result({"urgent": {"noul": 0.9}}, usage={"truncated": 1})
        assert cut.decision([NOUL], allow_truncated=True).answers["urgent"].answer == "yes"

    def test_an_uncut_result_is_not_refused(self) -> None:
        uncut = result({"urgent": {"noul": 0.9}}, usage={"truncated": 0, "truncated_questions": []})
        assert uncut.usage.cut is False
        assert uncut.decision([NOUL]).answers["urgent"].answer == "yes"
