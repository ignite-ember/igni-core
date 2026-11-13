"""What a decision may ask: the limits on questions, contexts and requests.

The limits are the host's, not Laya's: they keep one extension from
holding the model for long and a question inside what the model can
read. These pin each boundary from both sides, and that anything unknown
is refused.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.core.decisions.schemas import (
    DecisionContext,
    DecisionQuestion,
    DecisionRequest,
    Priority,
)


def choice(**over: Any) -> dict[str, Any]:
    return {
        "id": "kind",
        "kind": "choice",
        "question": "What is `text`?",
        "options": {"a": "", "b": "second"},
    } | over


def score(**over: Any) -> dict[str, Any]:
    return {
        "id": "sev",
        "kind": "score",
        "question": "How bad is `text`?",
        "levels": ["low", "high"],
    } | over


def noul(**over: Any) -> dict[str, Any]:
    return {"id": "urgent", "kind": "noul", "question": "Is `text` urgent?"} | over


def refused(shape: type, data: Any, match: str | None = None) -> None:
    with pytest.raises(ValidationError, match=match):
        shape.model_validate(data)


class TestAQuestionsIdAndText:
    @pytest.mark.parametrize("good", ["a", "a1_b", "x" * 64])
    def test_a_lowercase_id_up_to_64_characters_is_taken(self, good: str) -> None:
        assert DecisionQuestion.model_validate(noul(id=good)).id == good

    @pytest.mark.parametrize("bad", ["", "Upper", "1lead", "_lead", "a-b", "x" * 65])
    def test_any_other_id_is_refused(self, bad: str) -> None:
        refused(DecisionQuestion, noul(id=bad))

    def test_a_question_is_one_to_500_characters(self) -> None:
        assert DecisionQuestion.model_validate(noul(question="q" * 500))
        refused(DecisionQuestion, noul(question=""))
        refused(DecisionQuestion, noul(question="q" * 501))

    def test_an_unknown_field_is_refused(self) -> None:
        refused(DecisionQuestion, noul(extra=1), "extra")


class TestAChoice:
    def test_two_to_twenty_options_are_taken(self) -> None:
        for n in (2, 20):
            options = {f"o{i}": "" for i in range(n)}
            assert len(DecisionQuestion.model_validate(choice(options=options)).options or {}) == n

    @pytest.mark.parametrize("n", [0, 1, 21])
    def test_fewer_or_more_are_refused(self, n: int) -> None:
        refused(DecisionQuestion, choice(options={f"o{i}": "" for i in range(n)}), "2 to 20")

    def test_options_missing_are_refused_like_none(self) -> None:
        refused(DecisionQuestion, choice(options=None), "2 to 20")

    def test_it_has_options_not_levels(self) -> None:
        refused(DecisionQuestion, choice(levels=["x", "y"]), "options, not levels")

    @pytest.mark.parametrize("label", ["", "   ", "\t", "x" * 65])
    def test_a_blank_or_over_long_label_is_refused(self, label: str) -> None:
        refused(DecisionQuestion, choice(options={label: "", "b": ""}), "label")

    def test_a_label_of_64_characters_counts_characters_not_bytes(self) -> None:
        assert DecisionQuestion.model_validate(choice(options={"é" * 64: "", "b": ""}))

    def test_a_description_is_at_most_200_characters(self) -> None:
        assert DecisionQuestion.model_validate(choice(options={"a": "d" * 200, "b": ""}))
        refused(DecisionQuestion, choice(options={"a": "d" * 201, "b": ""}), "description")


class TestAScore:
    def test_two_to_ten_levels_are_taken(self) -> None:
        for n in (2, 10):
            levels = [f"l{i}" for i in range(n)]
            assert DecisionQuestion.model_validate(score(levels=levels)).levels == levels

    @pytest.mark.parametrize("n", [0, 1, 11])
    def test_fewer_or_more_are_refused(self, n: int) -> None:
        refused(DecisionQuestion, score(levels=[f"l{i}" for i in range(n)]), "2 to 10")

    def test_it_has_levels_not_options(self) -> None:
        refused(DecisionQuestion, score(options={"a": ""}), "levels, not options")

    def test_every_level_is_named_differently(self) -> None:
        refused(DecisionQuestion, score(levels=["low", "low", "high"]), "differently")

    def test_a_level_is_a_valid_label(self) -> None:
        refused(DecisionQuestion, score(levels=["low", " "]), "label")
        refused(DecisionQuestion, score(levels=["low", "x" * 65]), "label")


class TestAYesNo:
    def test_it_carries_neither_options_nor_levels(self) -> None:
        assert DecisionQuestion.model_validate(noul())
        refused(DecisionQuestion, noul(options={"a": ""}), "no options or levels")
        refused(DecisionQuestion, noul(levels=["a", "b"]), "no options or levels")


class TestAContext:
    def test_one_to_eight_lowercase_named_fields(self) -> None:
        assert DecisionContext.model_validate({"a": "x"})
        assert DecisionContext.model_validate({f"f{i}": "x" for i in range(8)})
        refused(DecisionContext, {}, "1 to 8")
        refused(DecisionContext, {f"f{i}": "x" for i in range(9)}, "1 to 8")
        refused(DecisionContext, {"Diff": "x"}, "lowercase")

    def test_at_most_6000_characters_across_the_fields(self) -> None:
        assert DecisionContext.model_validate({"a": "x" * 3000, "b": "y" * 3000})
        refused(DecisionContext, {"a": "x" * 3000, "b": "y" * 3001}, "6000")

    def test_unicode_counts_by_characters(self) -> None:
        assert DecisionContext.model_validate({"a": "é" * 6000})


class TestARequest:
    def request(self, contexts: int = 1, questions: list[dict[str, Any]] | None = None) -> Any:
        return {
            "contexts": [{"text": "t"} for _ in range(contexts)],
            "questions": questions if questions is not None else [noul()],
        }

    def test_one_to_sixteen_contexts(self) -> None:
        for n in (1, 16):
            assert len(DecisionRequest.model_validate(self.request(n)).contexts) == n
        refused(DecisionRequest, self.request(0))
        refused(DecisionRequest, self.request(17))

    def test_one_to_sixteen_questions(self) -> None:
        many = [noul(id=f"q{i}") for i in range(16)]
        assert len(DecisionRequest.model_validate(self.request(questions=many)).questions) == 16
        refused(DecisionRequest, self.request(questions=[]))
        refused(DecisionRequest, self.request(questions=[*many, noul(id="q16")]))

    def test_every_question_has_its_own_id(self) -> None:
        refused(DecisionRequest, self.request(questions=[noul(), noul()]), "own id")

    def test_an_unknown_field_is_refused(self) -> None:
        refused(DecisionRequest, self.request() | {"extra": 1}, "extra")


def test_approvals_go_before_extensions() -> None:
    assert Priority.APPROVAL == 0
    assert Priority.EXTENSION == 1
    assert Priority.APPROVAL < Priority.EXTENSION


def test_the_schemas_hold_igni_s_decision_types_and_leave_laya_s_shapes_to_the_wire() -> None:
    from igni_core.core.decisions import laya_wire, schemas

    assert {"Strict", "DecisionContext", "DecisionQuestion", "Priority"} <= set(schemas.__all__)
    assert not [name for name in dir(schemas) if name.startswith("Laya")]
    assert laya_wire.LayaQuestion.__module__ == "igni_core.core.decisions.laya_wire"
    assert issubclass(laya_wire.LayaQuestion, schemas.Strict)
    assert laya_wire.LayaBatchItem.model_fields["state"].annotation is schemas.DecisionContext
