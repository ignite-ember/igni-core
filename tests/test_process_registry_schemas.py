"""The registry's typed contracts: row models and the collaborator bundle.

``RunningProcess`` and ``KnownProcess`` are what the watcher and panel
RPCs serialise, so their field names, defaults and dump shape are the
wire contract. ``RegistryCollaborators`` is wired once and then read.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.core.tools.process_bus import ProcessEventBus
from igni_core.core.tools.process_log import ProcessLogStore
from igni_core.core.tools.process_registry_schemas import (
    KnownProcess,
    RegistryCollaborators,
    RunningProcess,
)
from igni_core.core.tools.sleep_blocker import SleepBlocker


def _collaborators() -> RegistryCollaborators:
    return RegistryCollaborators(
        bus=ProcessEventBus(), log_store=ProcessLogStore(None), sleep_blocker=SleepBlocker()
    )


class TestTheRowShapes:
    def test_a_running_row_dumps_its_three_fields(self) -> None:
        row = RunningProcess(pid=1, cmd="sleep 9", elapsed_seconds=2.5)
        assert row.model_dump() == {"pid": 1, "cmd": "sleep 9", "elapsed_seconds": 2.5}

    def test_a_known_row_has_no_exit_code_unless_given_one(self) -> None:
        row = KnownProcess(pid=1, cmd="c", elapsed_seconds=0.0, is_running=True)
        assert row.exit_code is None
        assert row.model_dump() == {
            "pid": 1,
            "cmd": "c",
            "elapsed_seconds": 0.0,
            "is_running": True,
            "exit_code": None,
        }

    def test_a_finished_row_carries_its_exit_code(self) -> None:
        row = KnownProcess(pid=1, cmd="c", elapsed_seconds=1.0, is_running=False, exit_code=-9)
        assert row.exit_code == -9

    def test_a_missing_field_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            KnownProcess(pid=1, cmd="c", elapsed_seconds=1.0)  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            RunningProcess(pid=1, cmd="c")  # type: ignore[call-arg]

    def test_a_pid_that_is_not_an_integer_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RunningProcess(pid="abc", cmd="c", elapsed_seconds=1.0)  # type: ignore[arg-type]


class TestTheCollaboratorBundle:
    def test_it_holds_what_it_was_given(self) -> None:
        bus, store, blocker = ProcessEventBus(), ProcessLogStore(None), SleepBlocker()
        bundle = RegistryCollaborators(bus=bus, log_store=store, sleep_blocker=blocker)
        assert (bundle.bus, bundle.log_store, bundle.sleep_blocker) == (bus, store, blocker)
        assert bundle.bus is bus

    def test_it_is_frozen(self) -> None:
        bundle = _collaborators()
        with pytest.raises(ValidationError):
            bundle.bus = ProcessEventBus()  # type: ignore[misc]

    @pytest.mark.parametrize("field", ["bus", "log_store", "sleep_blocker"])
    def test_a_collaborator_of_the_wrong_type_is_refused(self, field: str) -> None:
        good = {
            "bus": ProcessEventBus(),
            "log_store": ProcessLogStore(None),
            "sleep_blocker": SleepBlocker(),
        }
        good[field] = object()
        with pytest.raises(ValidationError):
            RegistryCollaborators(**good)  # type: ignore[arg-type]
