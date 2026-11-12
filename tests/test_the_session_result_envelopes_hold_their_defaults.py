"""The session-lifecycle result envelopes and the seams that name the backend."""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from igni_core.backend.schemas_sessions import (
    AutoNameResult,
    BackendLike,
    DispatchedBackend,
    EvictedRuntimeReport,
    EvictionReport,
    ShutdownReport,
)
from igni_core.backend.server import BackendServer


def test_an_auto_name_that_says_nothing_is_a_failure():
    assert AutoNameResult().model_dump() == {"ok": False, "name": "", "reason": "error"}


@pytest.mark.parametrize("reason", ["generated", "already_named", "no_name_produced", "error"])
def test_an_auto_name_reason_is_one_of_the_four_known(reason):
    assert AutoNameResult(ok=reason == "generated", reason=reason).reason == reason


def test_an_unknown_auto_name_reason_is_refused():
    with pytest.raises(ValidationError):
        AutoNameResult(reason="because")  # type: ignore[arg-type]


def test_a_shutdown_report_has_no_error_unless_it_failed():
    assert ShutdownReport(ok=True, session_id="s").model_dump() == {
        "ok": True,
        "session_id": "s",
        "error": "",
    }
    failed = ShutdownReport(ok=False, session_id="s", error="boom")
    assert (failed.ok, failed.error) == (False, "boom")


def test_an_empty_sweep_evicts_nothing_and_keeps_nobody():
    report = EvictionReport()
    assert report.evicted == []
    assert report.kept == 0
    assert report.evicted_ids == []


def test_the_evicted_ids_follow_the_order_of_the_sweep():
    report = EvictionReport(
        evicted=[
            EvictedRuntimeReport(
                session_id=sid,
                idle_seconds=idle,
                shutdown=ShutdownReport(ok=True, session_id=sid),
            )
            for sid, idle in (("b", 30.5), ("a", 10.0), ("c", 99.0))
        ],
        kept=2,
    )
    assert report.evicted_ids == ["b", "a", "c"]
    assert report.evicted[0].idle_seconds == 30.5
    assert report.evicted[0].shutdown.ok is True
    assert report.kept == 2


def test_two_reports_never_share_their_evicted_list():
    first, second = EvictionReport(), EvictionReport()
    first.evicted.append(
        EvictedRuntimeReport(
            session_id="s", idle_seconds=1, shutdown=ShutdownReport(ok=True, session_id="s")
        )
    )
    assert second.evicted == []


@pytest.mark.parametrize("protocol", [BackendLike, DispatchedBackend])
def test_the_backend_has_every_method_the_seams_name_with_the_same_parameters(protocol):
    """The Protocols are type-only, so nothing else would notice a rename on
    one side: ``record_event`` takes a typed draft, ``switch_model`` answers
    with an info or an error."""
    for name, wanted in vars(protocol).items():
        if name.startswith("_") or not callable(wanted) and not isinstance(wanted, property):
            continue
        if isinstance(wanted, property):
            assert isinstance(getattr(BackendServer, name), property), name
            continue
        actual = getattr(BackendServer, name, None)
        assert actual is not None, f"BackendServer has no {name}"
        assert list(inspect.signature(actual).parameters) == list(
            inspect.signature(wanted).parameters
        ), name
        assert inspect.iscoroutinefunction(actual) == inspect.iscoroutinefunction(wanted), name
