"""What an air-gapped machine is told when its copy of the decision model is not complete.

The router builds without its checkpoints and reaches for them at the
first question, so a copy that missed a file is noticed there — after
the load, not at it. The refusal has to say what the empty-cache
refusal says: that this machine is air-gapped, that the copy is
incomplete, and how to make one (campaign R085).

Without the air gap, the same failure keeps its own short message: a
machine that can reach the hub has a download to retry, and the
pre-seed instructions would only be noise.
"""

from __future__ import annotations

import pytest
from huggingface_hub.errors import IncompleteSnapshotError

from igni_core.core.air_gap import AIR_GAPPED_ENV
from igni_core.core.decisions.laya_wire import LayaQuestions
from igni_core.core.decisions.schemas import DecisionsUnavailable, Priority
from igni_core.decisions_server import model as decisions_model


class FailingRouter:
    """Stands in for ``laya.Router`` with a checkpoint the cache does not hold.

    It builds; it fails at the first question, which is where a copy
    that missed a file is found.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error

    def predict(self, state: dict[str, str], questions: dict[str, object], *, model: str) -> None:
        raise self._error


@pytest.fixture
def air_gapped(monkeypatch, tmp_path):
    """An air-gapped process whose Hugging Face cache is ``tmp_path``'s."""
    monkeypatch.setenv(AIR_GAPPED_ENV, "1")
    monkeypatch.setattr(decisions_model.hub, "HF_HUB_CACHE", str(tmp_path / "hub"))
    return tmp_path / "hub"


def _ask(router: FailingRouter, monkeypatch) -> DecisionsUnavailable:
    """The question the guard asks, against a router that cannot answer it."""
    monkeypatch.setattr(decisions_model.LayaModel, "load", lambda _self: router)
    model = decisions_model.LayaModel()
    with pytest.raises(DecisionsUnavailable) as refused:
        model.predict(
            {"tool_call": "run `rm -rf /`"},
            LayaQuestions.of([]),
            "typed-decisions",
            Priority.APPROVAL,
        )
    return refused.value


class TestAnIncompleteCopy:
    def test_the_refusal_says_the_copy_is_incomplete_and_how_to_make_one(
        self, air_gapped, monkeypatch
    ):
        router = FailingRouter(
            IncompleteSnapshotError("a file is missing from the cache", str(air_gapped))
        )
        said = str(_ask(router, monkeypatch))
        assert "air-gapped" in said
        assert "incomplete" in said
        assert str(air_gapped) in said, "says which cache to look at"
        assert "prefetch_models" in said, "says how to pre-seed it"
        assert "copy" in said, "says how to copy it over"

    def test_a_machine_that_can_reach_the_hub_keeps_the_short_message(self, monkeypatch):
        router = FailingRouter(IncompleteSnapshotError("a file is missing", "/tmp/hub"))
        said = str(_ask(router, monkeypatch))
        assert "air-gapped" not in said
        assert "prefetch_models" not in said, "no pre-seed instructions off the air gap"
        assert "IncompleteSnapshotError" in said
