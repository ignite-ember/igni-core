"""Turns a scenario's violations into its outcome: pass, known finding, or failure."""

from __future__ import annotations

import pytest

from tests.session_lifecycle.exceptions.known_finding_error import KnownFindingError
from tests.session_lifecycle.known_findings import KnownFindings
from tests.session_lifecycle.schemas.violation import Violation


class Verdict:
    """The one rule every scenario ends by: only a known finding's invariants may break."""

    @staticmethod
    def expect_known(request: pytest.FixtureRequest, cell: str) -> KnownFindings | None:
        """Mark the test ``xfail`` when ``cell`` has known findings: strict unless they are races."""
        known = KnownFindings.for_cell(cell)
        if known is not None:
            request.applymarker(
                pytest.mark.xfail(
                    strict=known.strict, raises=KnownFindingError, reason=known.describe()
                )
            )
        return known

    @staticmethod
    def settle(cell: str, violations: list[Violation]) -> None:
        """Pass on none; raise :class:`KnownFindingError` on known ones only; fail otherwise."""
        known = KnownFindings.for_cell(cell)
        report = "\n".join(str(v) for v in violations)
        broken = {v.invariant for v in violations}
        if known is not None and broken and broken <= known.invariants:
            raise KnownFindingError(f"{known.describe()}\n{report}")
        assert not violations, f"{cell} broke {sorted(broken)}:\n{report}"
