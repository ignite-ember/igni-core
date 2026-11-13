"""What the two stream states do with the events they are fed.

A specialist's state and a team's state are driven by the stream
handlers; these tests drive them directly, on a clock they control, so
the throttles and the dedup are read at their boundaries rather than
inferred from a whole run.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.core.tools import subagent_stream
from igni_core.core.tools.orchestrate_stream_state import StreamState
from igni_core.core.tools.subagent_stream import SubAgentStreamState, TeamStreamState


class _Clock:
    """A stand-in for the ``time`` module the states read."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(subagent_stream, "time", clock)
    return clock


def _event(**fields):
    return SimpleNamespace(**fields)


class TestSubAgentLatchIds:
    def test_the_first_run_id_reports_that_it_was_latched(self):
        state = SubAgentStreamState(agent_path_id="root")

        assert state.latch_ids(_event(run_id="r1", session_id="s1")) is True
        assert (state.current_run_id, state.current_session_id) == ("r1", "s1")

    def test_a_later_event_neither_reports_nor_overwrites(self):
        state = SubAgentStreamState(agent_path_id="root")
        state.latch_ids(_event(run_id="r1", session_id="s1"))

        assert state.latch_ids(_event(run_id="r2", session_id="s2")) is False
        assert (state.current_run_id, state.current_session_id) == ("r1", "s1")

    def test_an_event_without_ids_latches_nothing(self):
        state = SubAgentStreamState(agent_path_id="root")

        assert state.latch_ids(_event(run_id="", session_id=None)) is False
        assert state.latch_ids(object()) is False
        assert state.current_run_id is None
        assert state.current_session_id is None

    def test_the_parent_run_may_arrive_after_the_run_id(self):
        state = SubAgentStreamState(agent_path_id="root")
        state.latch_ids(_event(run_id="r1", parent_run_id=""))
        assert state.parent_top_run_id is None

        state.latch_ids(_event(run_id="r1", parent_run_id="top"))

        assert state.parent_top_run_id == "top"

    def test_the_first_parent_run_wins(self):
        state = SubAgentStreamState(agent_path_id="root")
        state.latch_ids(_event(run_id="r1", parent_run_id="top"))

        state.latch_ids(_event(run_id="r1", parent_run_id="other"))

        assert state.parent_top_run_id == "top"

    def test_a_late_run_id_still_reports_once_when_the_session_came_first(self):
        state = SubAgentStreamState(agent_path_id="root")
        assert state.latch_ids(_event(session_id="s1")) is False

        assert state.latch_ids(_event(run_id="r1")) is True
        assert state.latch_ids(_event(run_id="r1")) is False


class TestSharedBase:
    def test_both_states_extend_the_shared_stream_state(self):
        assert issubclass(SubAgentStreamState, StreamState)
        assert issubclass(TeamStreamState, StreamState)

    def test_a_team_latches_run_and_session_ids_like_a_specialist(self):
        state = TeamStreamState(team_path_id="team")

        assert state.latch_ids(_event(run_id="r1", session_id="s1")) is True
        assert state.latch_ids(_event(run_id="r2", session_id="s2")) is False
        assert (state.current_run_id, state.current_session_id) == ("r1", "s1")

    def test_a_specialist_reports_its_own_completion_and_a_team_does_not(self):
        specialist = SubAgentStreamState(agent_path_id="root")
        team = TeamStreamState(team_path_id="team")

        specialist.record_completion("done")
        team.record_completion("team done")

        assert specialist.agent_completed_emitted is True
        assert specialist.completed_content == "done"
        assert team.emits_agent_completed is False
        assert team.agent_completed_emitted is False
        # The team still keeps the streamed answer as a fallback.
        assert team.completed_content == "team done"


class TestVisualizerThrottle:
    def test_the_first_delta_always_emits(self):
        state = SubAgentStreamState(agent_path_id="root")

        assert state.can_emit_vis_delta(0.0) is True

    def test_a_delta_within_fifty_ms_is_held_back(self):
        state = SubAgentStreamState(agent_path_id="root")
        state.record_vis_emission(10.0, partial_len=5)

        assert state.can_emit_vis_delta(10.049) is False

    def test_a_delta_fifty_ms_later_emits(self):
        state = SubAgentStreamState(agent_path_id="root")
        state.record_vis_emission(10.0, partial_len=5)

        assert state.can_emit_vis_delta(10.05) is True

    def test_recording_an_emission_stores_its_length_and_time(self):
        state = SubAgentStreamState(agent_path_id="root")

        state.record_vis_emission(12.5, partial_len=42)

        assert (state.vis_last_emitted_len, state.vis_last_emit_at) == (42, 12.5)

    def test_an_emission_of_length_zero_leaves_the_next_delta_unthrottled(self):
        state = SubAgentStreamState(agent_path_id="root")
        state.record_vis_emission(10.0, partial_len=0)

        assert state.can_emit_vis_delta(10.0) is True


class TestSubAgentContentPreview:
    def test_an_empty_chunk_is_ignored_and_buffers_nothing(self, clock):
        state = SubAgentStreamState(agent_path_id="root")

        assert state.append_content_delta("") is None
        assert state.content_buf == ""

    def test_the_first_chunk_after_the_window_returns_a_preview(self, clock):
        state = SubAgentStreamState(agent_path_id="root")

        assert state.append_content_delta("hello") == "hello"
        assert state.last_preview == "hello"
        assert state.last_update == clock.now

    def test_a_chunk_inside_the_window_is_buffered_but_shows_nothing(self, clock):
        state = SubAgentStreamState(agent_path_id="root")
        state.append_content_delta("one\n")

        clock.now += 0.5

        assert state.append_content_delta("two") is None
        assert state.content_buf == "one\ntwo"

    def test_the_next_preview_after_the_window_carries_everything_buffered(self, clock):
        state = SubAgentStreamState(agent_path_id="root")
        state.append_content_delta("one\n")
        clock.now += 0.5
        state.append_content_delta("two")

        clock.now += 0.01

        assert state.append_content_delta("") is None
        assert state.append_content_delta("!") == "one\ntwo!"

    def test_an_unchanged_preview_is_not_sent_twice(self, clock):
        state = SubAgentStreamState(agent_path_id="root")
        state.append_content_delta("same")
        clock.now += 1.0

        # Only whitespace advanced the buffer: the preview text is the same.
        assert state.append_content_delta("\n") is None

    def test_a_buffer_with_nothing_to_show_returns_none(self, clock):
        state = SubAgentStreamState(agent_path_id="root")

        assert state.append_content_delta("\n  \n") is None
        assert state.last_preview == ""

    def test_a_chunk_that_is_not_text_is_buffered_as_its_string(self, clock):
        state = SubAgentStreamState(agent_path_id="root")

        assert state.append_content_delta(123) == "123"  # type: ignore[arg-type]


class TestTeamContentPreview:
    def test_it_needs_only_the_team_path(self):
        state = TeamStreamState(team_path_id="team")

        assert state.current_agent == ""
        assert state.last_update_by_agent == {}
        assert state.last_preview_by_agent == {}
        assert state.content_buf_by_agent == {}
        assert state.emits_agent_completed is False

    def test_an_empty_chunk_is_ignored(self, clock):
        state = TeamStreamState(team_path_id="team")

        assert state.append_content_delta("a", "") is None
        assert state.content_buf_by_agent == {}

    def test_a_member_chatting_does_not_starve_another(self, clock):
        state = TeamStreamState(team_path_id="team")

        assert state.append_content_delta("team.a", "from a") == "from a"
        # Same instant, other member: its own window has not been used.
        assert state.append_content_delta("team.b", "from b") == "from b"
        assert state.content_buf_by_agent == {"team.a": "from a", "team.b": "from b"}

    def test_a_members_own_window_holds_its_next_chunk_back(self, clock):
        state = TeamStreamState(team_path_id="team")
        state.append_content_delta("team.a", "one\n")
        clock.now += 0.5

        assert state.append_content_delta("team.a", "two") is None
        assert state.content_buf_by_agent["team.a"] == "one\ntwo"

        clock.now += 0.01
        assert state.append_content_delta("team.a", "!") == "one\ntwo!"

    def test_an_unchanged_preview_is_not_sent_twice_but_is_kept_per_member(self, clock):
        state = TeamStreamState(team_path_id="team")
        state.append_content_delta("team.a", "same")
        clock.now += 1.0

        assert state.append_content_delta("team.a", "\n") is None
        # Another member saying the same words is not a duplicate for it.
        assert state.append_content_delta("team.b", "same") == "same"
        assert state.last_preview_by_agent == {"team.a": "same", "team.b": "same"}

    def test_a_buffer_with_nothing_to_show_returns_none(self, clock):
        state = TeamStreamState(team_path_id="team")

        assert state.append_content_delta("team.a", "\n") is None
