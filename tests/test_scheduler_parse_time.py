"""parse_time beyond the happy path: rollover, explicit dates, other languages."""

from datetime import datetime, timedelta

import pytest

from igni_core.core.scheduler import parser
from igni_core.core.scheduler.parser import parse_time


class TestRollover:
    def test_a_clock_time_read_as_already_past_is_pushed_to_the_next_day(self, monkeypatch):
        past = datetime.now() - timedelta(hours=1)
        monkeypatch.setattr(parser.dateparser, "parse", lambda *_a, **_k: past)

        assert parse_time("at 5pm") == past + timedelta(days=1)

    def test_a_time_in_the_future_is_left_alone(self, monkeypatch):
        future = datetime.now() + timedelta(hours=1)
        monkeypatch.setattr(parser.dateparser, "parse", lambda *_a, **_k: future)

        assert parse_time("at 5pm") == future

    @pytest.mark.parametrize("text", ["2020-01-01", "2020/01/01 10:00", "tomorrow", "TOMORROW"])
    def test_an_explicit_date_or_tomorrow_is_never_pushed(self, monkeypatch, text):
        past = datetime(2020, 1, 1, 10, 0)
        monkeypatch.setattr(parser.dateparser, "parse", lambda *_a, **_k: past)

        assert parse_time(text) == past

    def test_an_explicit_past_date_stays_in_the_past_with_the_real_parser(self):
        assert parse_time("2020-01-01 10:00") == datetime(2020, 1, 1, 10, 0)

    def test_a_slash_date_with_a_year_is_read(self):
        assert parse_time("2999/03/20").date().isoformat() == "2999-03-20"

    def test_a_clock_time_with_the_real_parser_is_always_ahead(self):
        past_clock = (datetime.now() - timedelta(hours=1)).strftime("%H:%M")
        result = parse_time(f"at {past_clock}")

        assert result is not None
        assert datetime.now() < result <= datetime.now() + timedelta(days=1)


class TestInput:
    @pytest.mark.parametrize("text", ["", "   ", "garbage " * 500])
    def test_nothing_readable_gives_none(self, text):
        assert parse_time(text) is None

    def test_surrounding_whitespace_is_ignored(self):
        result = parse_time("  \n in 5 minutes \t")
        assert result is not None
        assert timedelta(minutes=4) < result - datetime.now() <= timedelta(minutes=5)

    def test_the_result_is_a_naive_local_time(self):
        result = parse_time("in 1 hours")
        assert result is not None
        assert result.tzinfo is None


class TestOtherLanguages:
    def test_ukrainian_relative_time(self):
        result = parse_time("через 5 хвилин")
        assert result is not None
        assert timedelta(minutes=4) < result - datetime.now() <= timedelta(minutes=5)

    def test_spanish_relative_time(self):
        result = parse_time("en 30 minutos")
        assert result is not None
        assert timedelta(minutes=29) < result - datetime.now() <= timedelta(minutes=30)

    def test_ukrainian_tomorrow_at_a_time(self):
        result = parse_time("завтра о 9:00")
        assert result is not None
        tomorrow = datetime.now() + timedelta(days=1)
        assert result.date() == tomorrow.date()
        assert (result.hour, result.minute) == (9, 0)

    def test_spanish_tomorrow(self):
        result = parse_time("mañana")
        assert result is not None
        assert result.date() == (datetime.now() + timedelta(days=1)).date()
