"""The verdict call is answered.

The client reports what a card drew over one RPC, and the backend
answers with the verdict it kept. The answer is built by
``VisualizationVerdictResult.of(...)``, and that name has to exist at
runtime: a class imported only under ``TYPE_CHECKING`` is there for
the type checker and gone for the caller, so the answer the client
received was a ``NameError`` on every single verdict — the verdict was
kept (the book is written before the answer is built), but the client
was told the call failed, once per draw.

This is pinned against the real :class:`BackendServer`, not against a
test double: a double of the backend answers with whatever the double
is written to answer, so it cannot see a name that is missing at
runtime.
"""

from __future__ import annotations

from igni_core.backend.schemas_visualization import VisualizationVerdictResult
from igni_core.backend.server import BackendServer
from igni_core.core.tools.visualization_verdicts import (
    VERDICTS,
    ElementFailure,
    VisualizationVerdict,
)


def _backend() -> BackendServer:
    """The real backend, without its collaborators.

    ``report_visualization_verdict`` writes to the verdict book and
    builds the answer; it reads nothing of the session, so the
    collaborators are not needed to call it.
    """
    return object.__new__(BackendServer)


def _verdict() -> VisualizationVerdict:
    return VisualizationVerdict(
        spec_id="card-1",
        revision=2,
        total=3,
        failed=(ElementFailure(type="BarGraph", message="its data is not a list"),),
        issues=("its child 'valuesCard' is not an element of it",),
    )


def test_the_call_is_answered_with_the_verdict_that_was_kept() -> None:
    verdict = _verdict()

    result = _backend().report_visualization_verdict(verdict)

    assert result == VisualizationVerdictResult(
        ok=True, spec_id="card-1", revision=2, drawn=2, total=3
    )


def test_the_verdict_reaches_the_book_the_recap_reads() -> None:
    verdict = _verdict()

    _backend().report_visualization_verdict(verdict)

    kept = VERDICTS.get("card-1", 2)
    assert kept is not None
    assert kept.failed == (ElementFailure(type="BarGraph", message="its data is not a list"),)
    assert kept.issues == ("its child 'valuesCard' is not an element of it",)
