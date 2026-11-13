"""Monitor value types: status strings, decision union, control result."""

from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from igni_core.core.monitors import (
    MonitorControlResult,
    MonitorSnapshot,
    MonitorStatus,
    RestartDecision,
)
from igni_core.core.monitors.models import BackoffDecision, GiveUpDecision, StopDecision


def test_status_values_are_plain_strings():
    assert MonitorStatus.STOPPED == "stopped"
    assert MonitorStatus.RUNNING == "running"
    assert MonitorStatus.FAILED == "failed"
    assert {s.value for s in MonitorStatus} == {"stopped", "running", "failed"}


def test_a_snapshot_dumps_to_json_with_string_status_and_null_pid():
    snap = MonitorSnapshot(
        name="n",
        command="c",
        status=MonitorStatus.FAILED,
        pid=None,
        uptime_seconds=0.0,
        exit_code=None,
        crash_count=2,
        restart="always",
    )
    assert json.loads(snap.model_dump_json()) == {
        "name": "n",
        "command": "c",
        "status": "failed",
        "pid": None,
        "uptime_seconds": 0.0,
        "exit_code": None,
        "crash_count": 2,
        "restart": "always",
    }


def test_decisions_parse_by_their_action():
    adapter = TypeAdapter(RestartDecision)
    assert isinstance(adapter.validate_python({"action": "stop"}), StopDecision)
    backoff = adapter.validate_python({"action": "backoff", "delay_seconds": 2, "attempt": 1})
    assert isinstance(backoff, BackoffDecision)
    assert backoff.delay_seconds == 2.0
    assert isinstance(adapter.validate_python({"action": "give_up", "reason": "r"}), GiveUpDecision)


@pytest.mark.parametrize(
    "raw",
    [
        {"action": "explode"},
        {"action": "backoff", "attempt": 1},
        {"action": "backoff", "delay_seconds": 1},
        {"action": "give_up"},
        {},
    ],
)
def test_a_decision_with_an_unknown_action_or_missing_field_is_rejected(raw):
    with pytest.raises(ValidationError):
        TypeAdapter(RestartDecision).validate_python(raw)


def test_decision_defaults():
    assert StopDecision().action == "stop"
    assert StopDecision().reason == ""


def test_a_control_result_stringifies_to_its_reason():
    result = MonitorControlResult(ok=True, name="x", action="stop", reason="Stopped x.")
    assert str(result) == "Stopped x."
    assert f"{result}" == "Stopped x."


def test_a_control_result_only_knows_restart_and_stop():
    with pytest.raises(ValidationError):
        MonitorControlResult(ok=True, name="x", action="start", reason="r")  # type: ignore[arg-type]
