"""Team modes fall back to coordinate; isolation modes refuse."""

from __future__ import annotations

import pytest

from igni_core.core.tools.orchestrate_modes import IsolationMode, TeamMode


class TestTeamMode:
    @pytest.mark.parametrize("name", ["route", "coordinate", "broadcast", "tasks"])
    def test_a_named_mode_is_that_mode(self, name):
        assert TeamMode.parse(name).value == name

    @pytest.mark.parametrize("raw", [None, "", "swarm", "ROUTE", "Tasks"])
    def test_anything_else_is_coordinate(self, raw):
        assert TeamMode.parse(raw) is TeamMode.COORDINATE

    def test_the_valid_names_are_the_four_modes(self):
        assert TeamMode.valid_names() == ["route", "coordinate", "broadcast", "tasks"]


class TestIsolationMode:
    def test_none_and_the_empty_string_mean_no_isolation(self):
        assert IsolationMode.parse(None) is IsolationMode.NONE
        assert IsolationMode.parse("") is IsolationMode.NONE

    def test_worktree_is_named(self):
        assert IsolationMode.parse("worktree") is IsolationMode.WORKTREE

    @pytest.mark.parametrize("raw", ["docker", "Worktree", " worktree"])
    def test_an_unknown_mode_is_refused_not_defaulted(self, raw):
        assert IsolationMode.parse(raw) is None

    def test_the_valid_names_leave_out_the_empty_one(self):
        assert IsolationMode.valid_names() == ["worktree"]

    def test_the_refusal_message_quotes_the_value_and_lists_the_valid_names(self):
        assert (
            IsolationMode.unknown_message("docker")
            == "Error: unknown isolation mode 'docker'. Valid: ['worktree']."
        )
        assert "Valid: ['worktree']" in IsolationMode.unknown_message(None)
        assert "mode None." in IsolationMode.unknown_message(None)
