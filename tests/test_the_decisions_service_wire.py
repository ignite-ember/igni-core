"""What the backend sends the decisions service and what it answers.

These pin the call shapes (what they take and refuse), the answer
shapes, the failure body the client reads, and the package's exports.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

import igni_core.core.decisions as decisions
from igni_core.core.decisions import schemas
from igni_core.core.decisions.laya import Laya
from igni_core.core.decisions.laya_wire import LayaQuestions
from igni_core.core.decisions.service_wire import (
    DecideAnswer,
    DecideCall,
    DecisionsFailure,
    PredictAnswer,
    PredictCall,
)

NOUL = {"id": "urgent", "kind": "noul", "question": "Is `text` urgent?"}


def predict(**over: Any) -> dict[str, Any]:
    return {"state": {"text": "x"}, "questions": [NOUL], "model": "typed-decisions"} | over


class TestPredictCall:
    def test_it_carries_state_questions_model_and_an_approval_priority(self) -> None:
        call = PredictCall.model_validate(predict())
        assert call.state == {"text": "x"}
        assert call.model == "typed-decisions"
        assert call.priority is schemas.Priority.APPROVAL
        assert call.questions[0].id == "urgent"

    def test_a_priority_may_be_named_by_its_number(self) -> None:
        assert (
            PredictCall.model_validate(predict(priority=1)).priority is schemas.Priority.EXTENSION
        )
        with pytest.raises(ValidationError):
            PredictCall.model_validate(predict(priority=7))

    def test_one_to_sixteen_questions(self) -> None:
        many = [NOUL | {"id": f"q{i}"} for i in range(16)]
        assert len(PredictCall.model_validate(predict(questions=many)).questions) == 16
        for bad in ([], [*many, NOUL | {"id": "q16"}]):
            with pytest.raises(ValidationError):
                PredictCall.model_validate(predict(questions=bad))

    @pytest.mark.parametrize("missing", ["state", "questions", "model"])
    def test_what_it_needs_is_required(self, missing: str) -> None:
        data = predict()
        del data[missing]
        with pytest.raises(ValidationError):
            PredictCall.model_validate(data)

    def test_an_unknown_field_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            PredictCall.model_validate(predict(extra=1))

    def test_its_questions_go_to_the_router_keyed_by_id(self) -> None:
        call = PredictCall.model_validate(predict(questions=[NOUL, NOUL | {"id": "other"}]))
        assert isinstance(call.laya_questions(), LayaQuestions)
        assert list(call.laya_questions().wire()) == ["urgent", "other"]


class TestDecideCall:
    def test_it_wraps_one_request_and_refuses_anything_else(self) -> None:
        request = {"contexts": [{"text": "x"}], "questions": [NOUL]}
        assert DecideCall.model_validate({"request": request}).request.questions[0].id == "urgent"
        with pytest.raises(ValidationError):
            DecideCall.model_validate({"request": request, "extra": 1})
        with pytest.raises(ValidationError):
            DecideCall.model_validate({})


class TestTheAnswers:
    def test_a_predict_answer_is_one_result(self) -> None:
        answer = PredictAnswer.model_validate({"result": {"answers": {"urgent": {"noul": 0.8}}}})
        assert answer.result.answers["urgent"].noul == 0.8

    def test_a_decide_answer_is_a_result_per_context_and_may_have_none(self) -> None:
        two = DecideAnswer.model_validate({"results": [{"answers": {}}, {"answers": {}}]})
        assert len(two.results) == 2
        assert DecideAnswer.model_validate({"results": []}).results == []

    def test_an_answer_without_its_results_is_not_one(self) -> None:
        with pytest.raises(ValidationError):
            DecideAnswer.model_validate({})
        with pytest.raises(ValidationError):
            PredictAnswer.model_validate({})


class TestAFailure:
    @pytest.mark.parametrize("error", ["unavailable", "unauthorized"])
    def test_the_two_errors_the_service_gives(self, error: str) -> None:
        failure = DecisionsFailure.model_validate({"error": error, "detail": "why"})
        assert (failure.error, failure.detail) == (error, "why")

    @pytest.mark.parametrize("body", [{"error": "teapot", "detail": "x"}, {"error": "unavailable"}])
    def test_any_other_body_is_not_a_failure(self, body: dict[str, str]) -> None:
        with pytest.raises(ValidationError):
            DecisionsFailure.model_validate(body)


class TestThePackageRoot:
    def test_it_exports_the_client_and_igni_s_decision_types(self) -> None:
        assert sorted(decisions.__all__) == sorted(
            [
                "Decision",
                "DecisionAnswer",
                "DecisionLimits",
                "DecisionQuestion",
                "DecisionRequest",
                "DecisionTooLong",
                "DecisionsUnavailable",
                "Laya",
                "Priority",
                "QuestionKind",
            ]
        )
        assert decisions.Laya is Laya
        for name in decisions.__all__:
            if name != "Laya":
                assert getattr(decisions, name) is getattr(schemas, name)
