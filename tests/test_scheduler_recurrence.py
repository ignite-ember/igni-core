"""Recurrence: the phrases it reads, the string it stores, the steps it takes."""

from datetime import datetime, timedelta

import pytest

from igni_core.core.scheduler.recurrence import Recurrence, RecurrenceUnit


class TestUnitTokens:
    @pytest.mark.parametrize(
        ("token", "unit"),
        [
            ("min", RecurrenceUnit.MINUTES),
            ("Minutes", RecurrenceUnit.MINUTES),
            ("HOUR", RecurrenceUnit.HOURS),
            ("hours", RecurrenceUnit.HOURS),
            ("day", RecurrenceUnit.DAYS),
            ("Days", RecurrenceUnit.DAYS),
            ("week", RecurrenceUnit.WEEKS),
            ("WEEKS", RecurrenceUnit.WEEKS),
        ],
    )
    def test_a_token_names_its_unit_in_any_case(self, token, unit):
        assert RecurrenceUnit.from_token(token) is unit

    @pytest.mark.parametrize("token", ["", "fortnight", "month", "sec"])
    def test_an_unknown_token_names_no_unit(self, token):
        assert RecurrenceUnit.from_token(token) is None


class TestTheStoredForm:
    @pytest.mark.parametrize(
        ("amount", "unit", "delta"),
        [
            (5, RecurrenceUnit.MINUTES, timedelta(minutes=5)),
            (2, RecurrenceUnit.HOURS, timedelta(hours=2)),
            (3, RecurrenceUnit.DAYS, timedelta(days=3)),
            (2, RecurrenceUnit.WEEKS, timedelta(weeks=2)),
        ],
    )
    def test_the_canonical_string_and_the_interval_agree(self, amount, unit, delta):
        r = Recurrence(amount=amount, unit=unit)

        assert r.canonical() == f"every {amount} {unit.value}"
        assert r.to_delta() == delta
        assert Recurrence.from_canonical(r.canonical()) == r

    def test_the_first_occurrence_is_one_interval_after_the_given_now(self):
        now = datetime(2026, 3, 19, 14, 0)
        r = Recurrence(amount=2, unit=RecurrenceUnit.HOURS)
        assert r.first_occurrence(now) == datetime(2026, 3, 19, 16, 0)

    @pytest.mark.parametrize(
        "text", ["", "daily", "every minute", "every 0 days", "every 2 fortnights", "every 2"]
    )
    def test_only_the_stored_form_is_read_back(self, text):
        assert Recurrence.from_canonical(text) is None

    def test_a_singular_unit_and_surrounding_space_are_read(self):
        r = Recurrence.from_canonical("  every 1 day ")
        assert r == Recurrence(amount=1, unit=RecurrenceUnit.DAYS)

    def test_an_interval_too_large_for_a_timedelta_is_not_a_recurrence(self):
        """It used to be accepted, and the rescheduler then raised
        OverflowError after the task had already run."""
        assert Recurrence.from_canonical("every 99999999999 weeks") is None


class TestParsingAPhrase:
    def test_every_defaults_to_one(self):
        result = Recurrence.parse("every minute")
        assert result is not None
        assert result.recurrence.canonical() == "every 1 minutes"

    def test_case_and_surrounding_space_do_not_matter(self):
        result = Recurrence.parse("  EVERY 3 Hours ")
        assert result is not None
        assert result.recurrence.canonical() == "every 3 hours"

    def test_abbreviated_units(self):
        result = Recurrence.parse("every 15 mins")
        assert result is not None
        assert result.recurrence.canonical() == "every 15 minutes"

    def test_weekly_is_seven_days(self):
        result = Recurrence.parse("weekly")
        assert result is not None
        assert result.recurrence.canonical() == "every 7 days"

    def test_the_first_run_of_a_bare_phrase_is_one_interval_from_now(self):
        before = datetime.now()
        result = Recurrence.parse("every 30 minutes")
        assert result is not None
        assert (
            before + timedelta(minutes=30)
            <= result.first_scheduled
            <= datetime.now() + timedelta(minutes=30)
        )

    def test_at_pins_the_clock_time_of_the_first_run(self):
        result = Recurrence.parse("every 30 minutes at 9am")
        assert result is not None
        assert result.recurrence.canonical() == "every 30 minutes"
        assert (result.first_scheduled.hour, result.first_scheduled.minute) == (9, 0)

    @pytest.mark.parametrize(
        "text",
        [
            "daily foo",
            "daily at",
            "daily at gibberish nonsense",
            "dailyfoo",
            "every 2 fortnights",
            "every day at",
            "sometimes",
            "",
        ],
    )
    def test_anything_else_is_not_a_recurrence(self, text):
        assert Recurrence.parse(text) is None

    def test_a_zero_amount_is_not_a_recurrence(self):
        """``/schedule x every 0 hours`` used to raise ValidationError out of
        the parser instead of being refused like any other bad phrase."""
        assert Recurrence.parse("every 0 hours") is None

    def test_an_absurd_amount_is_not_a_recurrence(self):
        """A number with thousands of digits cannot become an int; it raised
        ValueError out of the parser."""
        assert Recurrence.parse(f"every {'9' * 5000} days") is None

    def test_an_amount_too_large_for_a_timedelta_is_not_a_recurrence(self):
        assert Recurrence.parse("every 99999999999 weeks") is None


class TestNextAfterOverALongGap:
    def test_a_week_of_one_minute_steps_is_arithmetic_not_a_loop(self):
        r = Recurrence(amount=1, unit=RecurrenceUnit.MINUTES)
        anchor = datetime(2026, 3, 1, 0, 0)
        now = anchor + timedelta(days=7, seconds=30)

        assert r.next_after(anchor, not_before=now) == anchor + timedelta(days=7, minutes=1)
