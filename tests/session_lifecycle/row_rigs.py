"""One fresh backend per row of the matrix."""

from __future__ import annotations

from pathlib import Path

from tests.session_lifecycle.constants import RIG_CONFIG
from tests.session_lifecycle.enums.matrix_state import MatrixState
from tests.session_lifecycle.lifecycle_rig import LifecycleRig


class RowRigs:
    """One fresh backend per state row, so a row's leftovers never reach the next row."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state: MatrixState | None = None
        self.rig: LifecycleRig | None = None

    def for_state(self, state: MatrixState) -> LifecycleRig:
        if self.rig is None or state is not self.state or not self._alive():
            self.close()
            self.state = state
            self.rig = LifecycleRig(self.root / state.value, extra_config=RIG_CONFIG).start()
        return self.rig

    def _alive(self) -> bool:
        return self.rig is not None and self.rig.backend is not None and self.rig.backend.alive()

    def close(self) -> None:
        if self.rig is not None:
            self.rig.stop()
        self.rig = None
