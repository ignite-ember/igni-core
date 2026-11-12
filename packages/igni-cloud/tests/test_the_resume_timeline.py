"""The resume timeline's own behaviour: when it logs, what it leaves out, what it records.

``docs/CLOUD_RESUME_TODO.md`` item 1.
"""

from __future__ import annotations

import gc
import logging
import time
from pathlib import Path

import pytest
from igni_cloud.guest.timeline import ResumeTimeline

from igni_core.core.log_fields import LogFields

LOGGER = "igni_cloud.guest.timeline"


def logged(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "resume timeline"]


@pytest.fixture(autouse=True)
def _info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)


def test_without_what_the_guest_agent_wrote_there_is_no_breakdown() -> None:
    record = ResumeTimeline(3).record
    assert record.generation == 3
    assert record.noticed_after_ms is None
    assert record.written_after_ms is None
    assert record.woke_after_write_ms is None
    assert record.answered is False
    assert record.steps == []


def test_a_write_time_without_the_agents_clock_says_nothing_of_the_write() -> None:
    record = ResumeTimeline(1, written_ns=time.time_ns(), woken_at=time.perf_counter()).record
    assert record.noticed_after_ms is None
    assert record.written_after_ms is None


def test_the_breakdown_follows_the_agents_clock() -> None:
    at_ms = int(time.time() * 1000) - 50
    written_ns = (at_ms + 20) * 1_000_000
    record = ResumeTimeline(
        1, at_ms=at_ms, written_ns=written_ns, woken_at=time.perf_counter()
    ).record
    assert record.written_after_ms == 20.0
    assert record.noticed_after_ms is not None and record.noticed_after_ms >= 50
    assert record.woke_after_write_ms is not None and record.woke_after_write_ms >= 30


def test_it_logs_once_both_the_steps_and_the_answer_are_in(
    caplog: pytest.LogCaptureFixture,
) -> None:
    timeline = ResumeTimeline(1)
    timeline.answered()
    assert logged(caplog) == [] and not timeline.done
    timeline.steps_done()
    assert len(logged(caplog)) == 1 and timeline.done

    other = ResumeTimeline(2)
    other.steps_done()
    assert len(logged(caplog)) == 1
    other.answered()
    assert len(logged(caplog)) == 2


def test_a_second_answer_marks_nothing(caplog: pytest.LogCaptureFixture) -> None:
    timeline = ResumeTimeline(1)
    timeline.answered()
    timeline.answered()
    assert [s.step for s in timeline.record.steps] == ["first answer"]
    assert timeline.record.answered is True


def test_finishing_twice_logs_once_and_leaves_the_collector_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    timeline = ResumeTimeline(1)
    assert timeline._collected in gc.callbacks
    timeline.finish()
    timeline.finish()
    assert len(logged(caplog)) == 1
    assert timeline._collected not in gc.callbacks


def test_a_step_after_the_timeline_is_done_is_not_recorded() -> None:
    timeline = ResumeTimeline(1)
    timeline.mark("one")
    timeline.finish()
    timeline.mark("late")
    assert [s.step for s in timeline.record.steps] == ["one"]


def test_a_collection_during_the_timeline_is_two_steps_of_its_own() -> None:
    timeline = ResumeTimeline(1)
    gc.collect()
    timeline.finish()
    steps = [s.step for s in timeline.record.steps]
    assert steps == ["before a collection", "collection of generation 2"]


def test_a_collection_after_it_finished_is_not_recorded() -> None:
    timeline = ResumeTimeline(1)
    timeline.finish()
    gc.collect()
    assert timeline.record.steps == []


def test_with_no_event_loop_there_is_no_deadline_and_it_still_works() -> None:
    timeline = ResumeTimeline(1)
    assert timeline._deadline is None
    timeline.steps_done()
    timeline.answered()
    assert timeline.done


async def test_the_deadline_is_cancelled_when_it_finishes_first() -> None:
    timeline = ResumeTimeline(1)
    handle = timeline._deadline
    assert handle is not None and not handle.cancelled()
    timeline.finish()
    assert handle.cancelled()


@pytest.fixture
def proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    smaps, refs = tmp_path / "smaps_rollup", tmp_path / "clear_refs"
    smaps.write_text("Rss:  100 kB\nReferenced:   4242 kB\nAnonymous: 1 kB\n")
    monkeypatch.setattr(ResumeTimeline, "_SMAPS", smaps)
    monkeypatch.setattr(ResumeTimeline, "_CLEAR_REFS", refs)
    return smaps, refs


def test_memory_touched_is_not_read_unless_asked_for(
    proc: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IGNI_RESUME_PAGES", raising=False)
    timeline = ResumeTimeline(1)
    timeline.mark("step")
    assert timeline.record.steps[0].touched_kb is None
    assert not proc[1].exists(), "the referenced bits are left alone"


def test_asked_for_it_clears_the_referenced_bits_and_reads_what_was_touched(
    proc: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IGNI_RESUME_PAGES", "1")
    timeline = ResumeTimeline(1)
    assert proc[1].read_text() == "1"
    timeline.mark("step")
    assert timeline.record.steps[0].touched_kb == 4242


@pytest.mark.parametrize("content", [None, "Rss: 1 kB\n", "Referenced: lots kB\n"])
def test_what_cannot_be_read_is_none(proc: tuple[Path, Path], content: str | None) -> None:
    if content is None:
        proc[0].unlink()
    else:
        proc[0].write_text(content)
    assert ResumeTimeline._touched_kb() is None


def test_referenced_bits_that_cannot_be_cleared_do_not_stop_the_timeline(
    proc: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ResumeTimeline, "_CLEAR_REFS", tmp_path)  # a directory: not writable
    monkeypatch.setenv("IGNI_RESUME_PAGES", "1")
    timeline = ResumeTimeline(1)
    timeline.mark("step")
    assert timeline.record.steps[0].touched_kb == 4242


def test_the_line_carries_the_record_as_typed_log_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    timeline = ResumeTimeline(7, at_ms=0, clock_step_ms=12.5)
    timeline.mark("reseed")
    timeline.finish()
    (line,) = logged(caplog)
    fields = LogFields.carried_by(line)
    assert fields is not None
    assert set(fields.sections) == {"timeline"}
    assert fields.sections["timeline"] == timeline.record.model_dump(mode="json")
    assert fields.sections["timeline"]["generation"] == 7
    assert fields.sections["timeline"]["clock_step_ms"] == 12.5
    assert not hasattr(line, "timeline"), "nothing loose beside the typed fields"
