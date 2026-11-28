"""The matrix sees bugs: with a known bug shape put back into the backend, its cells go red."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.session_lifecycle.constants import RIG_CONFIG
from tests.session_lifecycle.enums.matrix_action import MatrixAction
from tests.session_lifecycle.enums.matrix_state import MatrixState
from tests.session_lifecycle.lifecycle_rig import LifecycleRig
from tests.session_lifecycle.matrix_cell import MatrixCell

pytestmark = [pytest.mark.session_lifecycle]

# mutant → the cells that must catch it, each with the invariants it must report.
CATCHES: dict[str, list[tuple[MatrixState, MatrixAction, set[str]]]] = {
    "stop_ignored": [
        (MatrixState.STREAMING, MatrixAction.CANCEL, {"cancel-clears"}),
        (MatrixState.TOOL, MatrixAction.CANCEL, {"quiet-after-cancel"}),
    ],
    "parallel_follow_up": [
        (MatrixState.STREAMING, MatrixAction.FOLLOW_UP, {"single-run", "history"}),
    ],
    "history_forgets": [
        (MatrixState.IDLE, MatrixAction.FOLLOW_UP, {"history"}),
    ],
}


@pytest.mark.parametrize("mutant", list(CATCHES))
async def test_the_matrix_catches_an_injected_bug(mutant: str, tmp_path: Path) -> None:
    rig = LifecycleRig(tmp_path, extra_config=RIG_CONFIG, mutant=mutant).start()
    try:
        for state, action, invariants in CATCHES[mutant]:
            violations = await MatrixCell(rig, state, action).run()
            broken = {v.invariant for v in violations}
            assert invariants <= broken, (
                f"with {mutant} injected, {state.value}.{action.value} reported "
                f"{sorted(broken) or 'nothing'}; it must report {sorted(invariants)}"
            )
    finally:
        rig.stop()
