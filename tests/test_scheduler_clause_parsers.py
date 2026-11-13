"""The two phrasing families of ``/schedule``, driven one at a time."""

from datetime import datetime, timedelta

import pytest

from igni_core.core.scheduler.clause_parsers import (
    OneShotClauseParser,
    ParsedSchedule,
    RecurringClauseParser,
)


class TestRecurring:
    def test_an_every_phrase_splits_into_task_first_run_and_canonical_form(self):
        before = datetime.now()
        parsed = RecurringClauseParser().try_parse("run tests every 2 hours")

        assert parsed is not None
        assert parsed.description == "run tests"
        assert parsed.recurrence == "every 2 hours"
        assert (
            before + timedelta(hours=2)
            <= parsed.scheduled_at
            <= datetime.now() + timedelta(hours=2)
        )

    @pytest.mark.parametrize(
        ("text", "recurrence"),
        [
            ("backup daily", "every 1 days"),
            ("check disk hourly", "every 1 hours"),
            ("send report weekly", "every 7 days"),
        ],
    )
    def test_the_one_word_forms(self, text, recurrence):
        parsed = RecurringClauseParser().try_parse(text)
        assert parsed is not None
        assert parsed.recurrence == recurrence

    def test_the_separator_is_matched_in_any_case(self):
        parsed = RecurringClauseParser().try_parse("Run Tests EVERY 3 Hours")
        assert parsed is not None
        assert (parsed.description, parsed.recurrence) == ("Run Tests", "every 3 hours")

    def test_the_right_most_separator_wins(self):
        parsed = RecurringClauseParser().try_parse("review the daily digest every 2 hours")
        assert parsed is not None
        assert parsed.description == "review the daily digest"
        assert parsed.recurrence == "every 2 hours"

    def test_a_repeated_separator_splits_at_the_last_one(self):
        parsed = RecurringClauseParser().try_parse("sync every 2 hours every 3 hours")
        assert parsed is not None
        assert parsed.description == "sync every 2 hours"
        assert parsed.recurrence == "every 3 hours"

    def test_a_separator_that_leads_the_text_leaves_no_description(self):
        assert RecurringClauseParser().try_parse("every 2 hours") is None
        assert RecurringClauseParser().try_parse(" daily") is None

    @pytest.mark.parametrize(
        "text", ["", "   ", "just a task", "run tests every fortnight", "task every 0 hours"]
    )
    def test_text_that_is_not_a_recurrence_defers(self, text):
        assert RecurringClauseParser().try_parse(text) is None

    def test_a_description_with_a_lowercase_length_changing_letter_is_kept_whole(self):
        """``"İ".lower()`` is two characters, so an index found in the
        lowered copy shifted the cut and clipped the description."""
        parsed = RecurringClauseParser().try_parse("İİİİ task every 2 hours")
        assert parsed is not None
        assert parsed.description == "İİİİ task"
        assert parsed.recurrence == "every 2 hours"

    def test_the_separators_are_data_the_instance_can_override(self):
        parser = RecurringClauseParser(separators=(" each ",))
        assert parser.try_parse("run tests each 2 hours") is None  # "each" is no phrase
        assert RecurringClauseParser(separators=(" every ",)).try_parse("a every day") is not None


class TestOneShot:
    def test_an_in_phrase_gives_one_run_and_no_recurrence(self):
        before = datetime.now()
        parsed = OneShotClauseParser().try_parse("water the plants in 10 minutes")

        assert parsed is not None
        assert parsed.description == "water the plants"
        assert parsed.recurrence == ""
        assert (
            before + timedelta(minutes=10) - timedelta(seconds=2)
            <= parsed.scheduled_at
            <= datetime.now() + timedelta(minutes=10)
        )

    def test_at_and_tomorrow_forms(self):
        at = OneShotClauseParser().try_parse("review codebase at 5pm")
        assert at is not None
        assert (at.description, at.scheduled_at.hour) == ("review codebase", 17)

        tomorrow = OneShotClauseParser().try_parse("audit tomorrow")
        assert tomorrow is not None
        assert tomorrow.description == "audit"
        assert tomorrow.scheduled_at.date() == (datetime.now() + timedelta(days=1)).date()

    def test_an_on_phrase_reads_a_date(self):
        parsed = OneShotClauseParser().try_parse("ship it on 2999-01-02")
        assert parsed is not None
        assert parsed.description == "ship it"
        assert parsed.scheduled_at.date().isoformat() == "2999-01-02"

    def test_the_right_most_separator_wins(self):
        parsed = OneShotClauseParser().try_parse("ping the on-call at 5pm")
        assert parsed is not None
        assert parsed.description == "ping the on-call"

    def test_a_repeated_separator_splits_at_the_last_one(self):
        parsed = OneShotClauseParser().try_parse("log in at 5pm at 6pm")
        assert parsed is not None
        assert parsed.description == "log in at 5pm"
        assert parsed.scheduled_at.hour == 18

    def test_the_separator_is_matched_in_any_case(self):
        parsed = OneShotClauseParser().try_parse("Ping The Team AT 5PM")
        assert parsed is not None
        assert parsed.description == "Ping The Team"

    def test_an_unparseable_tail_moves_on_to_the_next_separator_then_defers(self):
        # " in " is tried before " on "; its tail is junk, so " on " gets its turn.
        parsed = OneShotClauseParser().try_parse("deploy in prod on 2999-01-02")
        assert parsed is not None
        assert parsed.description == "deploy in prod"
        assert OneShotClauseParser().try_parse("deploy in prod") is None

    @pytest.mark.parametrize("text", ["", "   ", "at 5pm", " tomorrow", "plain task"])
    def test_no_description_or_no_time_defers(self, text):
        assert OneShotClauseParser().try_parse(text) is None

    def test_a_description_with_a_length_changing_letter_is_kept_whole(self):
        parsed = OneShotClauseParser().try_parse("İİİİ ping in 10 minutes")
        assert parsed is not None
        assert parsed.description == "İİİİ ping"


class TestTheOrderTheCoordinatorRelies:
    def test_a_phrase_with_both_markers_is_recurring_only_when_recurring_goes_first(self):
        text = "run tests every 2 hours at 5pm"
        recurring = RecurringClauseParser().try_parse(text)

        assert recurring is not None
        assert recurring.recurrence == "every 2 hours"
        assert recurring.description == "run tests"
        assert (recurring.scheduled_at.hour, recurring.scheduled_at.minute) == (17, 0)
        # The one-shot parser alone would take it as a single run.
        one_shot = OneShotClauseParser().try_parse(text)
        assert one_shot is not None
        assert one_shot.recurrence == ""


def test_a_parsed_schedule_is_one_shot_unless_told_otherwise():
    assert ParsedSchedule(description="x", scheduled_at=datetime(2030, 1, 1)).recurrence == ""
