"""The /schedule clause parsers split free text into description, time, recurrence."""

from datetime import datetime, timedelta

import pytest

from igni_core.core.scheduler.clause_parsers import (
    OneShotClauseParser,
    ParsedSchedule,
    RecurringClauseParser,
)

RECURRING = RecurringClauseParser()
ONE_SHOT = OneShotClauseParser()


class TestRecurring:
    def test_the_separators_are_these_four_in_order(self):
        assert RECURRING.separators == (" every ", " daily", " hourly", " weekly")

    def test_every_n_hours_splits_the_task_from_the_recurrence(self):
        got = RECURRING.try_parse("run tests every 2 hours")
        assert got.description == "run tests"
        assert got.recurrence == "every 2 hours"
        delta = got.scheduled_at - datetime.now()
        assert timedelta(hours=1, minutes=55) < delta < timedelta(hours=2, minutes=5)

    def test_a_recurrence_wins_over_a_later_time_of_day(self):
        got = RECURRING.try_parse("run tests every 2 hours at 5pm")
        assert got.description == "run tests"
        assert got.recurrence == "every 2 hours"
        assert (got.scheduled_at.hour, got.scheduled_at.minute) == (17, 0)

    def test_the_right_most_marker_decides(self):
        got = RECURRING.try_parse("review the daily digest every 2 hours")
        assert got.description == "review the daily digest"
        assert got.recurrence == "every 2 hours"

    def test_a_second_marker_stays_in_the_description(self):
        got = RECURRING.try_parse("sync every team every 2 hours")
        assert got.description == "sync every team"
        assert got.recurrence == "every 2 hours"

    def test_daily_and_hourly_are_one_unit_each(self):
        assert RECURRING.try_parse("check daily").recurrence == "every 1 days"
        assert RECURRING.try_parse("check daily").description == "check"
        assert RECURRING.try_parse("x hourly").recurrence == "every 1 hours"

    def test_the_marker_is_found_in_any_case_and_the_description_keeps_its_own(self):
        got = RECURRING.try_parse("Backup EVERY 2 hours")
        assert got.description == "Backup"
        assert got.recurrence == "every 2 hours"

    @pytest.mark.parametrize(
        "text", ["every hour", "nothing here", "a every banana", "", "ping the on-call at 5pm"]
    )
    def test_text_with_no_usable_recurrence_defers(self, text):
        assert RECURRING.try_parse(text) is None

    def test_a_marker_at_the_very_start_is_no_description(self):
        # idx must be > 0: nothing precedes the marker, so there is no task
        assert RECURRING.try_parse(" every 2 hours") is None


class TestOneShot:
    def test_the_separators_are_these_four_in_order(self):
        assert ONE_SHOT.separators == (" at ", " in ", " on ", " tomorrow")

    def test_a_time_of_day_has_no_recurrence(self):
        got = ONE_SHOT.try_parse("ping the on-call at 5pm")
        assert got.description == "ping the on-call"
        assert got.recurrence == ""
        assert (got.scheduled_at.hour, got.scheduled_at.minute) == (17, 0)
        assert got.scheduled_at > datetime.now()

    def test_a_second_separator_stays_in_the_description(self):
        got = ONE_SHOT.try_parse("meet at the office at 5pm")
        assert got.description == "meet at the office"
        assert (got.scheduled_at.hour, got.scheduled_at.minute) == (17, 0)

    def test_a_relative_delay(self):
        got = ONE_SHOT.try_parse("do x in 2 hours")
        assert got.description == "do x"
        delta = got.scheduled_at - datetime.now()
        assert timedelta(hours=1, minutes=55) < delta < timedelta(hours=2, minutes=5)

    def test_tomorrow_is_the_next_calendar_day(self):
        got = ONE_SHOT.try_parse("audit tomorrow")
        assert got.description == "audit"
        assert got.scheduled_at.date() == (datetime.now() + timedelta(days=1)).date()

    @pytest.mark.parametrize("text", ["nothing here", "", "at 5pm", "review in the morning ish"])
    def test_text_with_no_usable_time_defers(self, text):
        assert ONE_SHOT.try_parse(text) is None

    def test_a_recurrence_phrase_is_left_to_the_other_parser(self):
        got = ONE_SHOT.try_parse("run tests every 2 hours at 5pm")
        assert got.description == "run tests every 2 hours"
        assert got.recurrence == ""


def test_a_parsed_schedule_defaults_to_no_recurrence():
    assert ParsedSchedule(description="x", scheduled_at=datetime(2030, 1, 1)).recurrence == ""
