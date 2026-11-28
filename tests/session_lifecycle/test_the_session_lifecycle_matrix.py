"""Every session state × every action a person takes, against the real backend."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.session_lifecycle.enums.matrix_action import MatrixAction
from tests.session_lifecycle.enums.matrix_state import MatrixState
from tests.session_lifecycle.matrix_cell import MatrixCell
from tests.session_lifecycle.row_rigs import RowRigs
from tests.session_lifecycle.verdict import Verdict

pytestmark = [pytest.mark.session_lifecycle]


@pytest.fixture(scope="module")
def rigs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RowRigs]:
    rows = RowRigs(tmp_path_factory.mktemp("lifecycle"))
    try:
        yield rows
    finally:
        rows.close()


@pytest.mark.parametrize("action", list(MatrixAction), ids=lambda a: a.value)
@pytest.mark.parametrize("state", list(MatrixState), ids=lambda s: s.value)
async def test_a_session_keeps_its_invariants(
    rigs: RowRigs, state: MatrixState, action: MatrixAction, request: pytest.FixtureRequest
) -> None:
    cell = f"{state.value}.{action.value}"
    Verdict.expect_known(request, cell)
    rig = rigs.for_state(state)
    Verdict.settle(cell, await MatrixCell(rig, state, action).run())
