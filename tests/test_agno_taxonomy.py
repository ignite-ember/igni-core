"""Tests for ``protocol.agno_taxonomy`` — which Agno events form each group.

The serializer and the stream mux sort events with ``isinstance`` against
these tuples. A class missing from a tuple makes that event kind silently
unhandled, so what is pinned is membership: every group holds the agent and
the team variant of its event, the task groups hold team events only, and
the run-completed group also accepts the run outputs.
"""

from __future__ import annotations

import pytest
from agno.run import agent as agent_events
from agno.run import team as team_events

from igni_core.protocol import agno_taxonomy as tax
from igni_core.protocol import messages as msg
from igni_core.protocol.agno_taxonomy import AgnoEventTaxonomy
from igni_core.protocol.serializer import serialize_event

# group attribute -> (agent class name or None, team class name)
_BOTH = {
    "CONTENT": "RunContentEvent",
    "TOOL_STARTED": "ToolCallStartedEvent",
    "TOOL_COMPLETED": "ToolCallCompletedEvent",
    "TOOL_ERROR": "ToolCallErrorEvent",
    "MODEL_COMPLETED": "ModelRequestCompletedEvent",
    "RUN_CONTENT_COMPLETED": "RunContentCompletedEvent",
    "RUN_STARTED": "RunStartedEvent",
    "RUN_ERROR": "RunErrorEvent",
    "REASONING": "ReasoningStartedEvent",
    "REASONING_CONTENT": "ReasoningContentDeltaEvent",
    "RUN_PAUSED": "RunPausedEvent",
}
_TEAM_ONLY = {
    "TASK_CREATED": "TaskCreatedEvent",
    "TASK_UPDATED": "TaskUpdatedEvent",
    "TASK_ITERATION": "TaskIterationStartedEvent",
    "TASK_STATE_UPDATED": "TaskStateUpdatedEvent",
}


class TestGroups:
    @pytest.mark.parametrize(("group", "name"), sorted(_BOTH.items()))
    def test_a_group_holds_the_agent_and_the_team_variant(self, group, name):
        members = getattr(AgnoEventTaxonomy, group)
        assert set(members) == {getattr(agent_events, name), getattr(team_events, name)}

    @pytest.mark.parametrize(("group", "name"), sorted(_TEAM_ONLY.items()))
    def test_a_task_group_holds_only_the_team_event(self, group, name):
        assert getattr(AgnoEventTaxonomy, group) == (getattr(team_events, name),)

    def test_run_completed_also_accepts_the_run_outputs(self):
        assert set(AgnoEventTaxonomy.RUN_COMPLETED) == {
            agent_events.RunCompletedEvent,
            team_events.RunCompletedEvent,
            agent_events.RunOutput,
            team_events.TeamRunOutput,
        }

    def test_an_agent_event_matches_the_agent_and_team_groups_alike(self):
        agent = agent_events.RunStartedEvent(run_id="r")
        team = team_events.RunStartedEvent(run_id="r")
        assert isinstance(agent, tax.RUN_STARTED_EVENTS)
        assert isinstance(team, tax.RUN_STARTED_EVENTS)

    def test_a_task_event_is_matched_for_a_team_only(self):
        team = team_events.TaskCreatedEvent(run_id="r")
        assert isinstance(team, tax.TASK_CREATED_EVENTS)
        assert not hasattr(agent_events, "TaskCreatedEvent")

    def test_a_run_output_is_a_completed_run(self):
        assert isinstance(agent_events.RunOutput(run_id="r"), tax.RUN_COMPLETED_EVENTS)
        assert isinstance(team_events.TeamRunOutput(run_id="r"), tax.RUN_COMPLETED_EVENTS)

    def test_a_finished_team_run_is_serialized_as_a_completed_run(self):
        # The team's final output is a TeamRunOutput; it must reach the
        # run-completed handler like the agent's does.
        proto = serialize_event(team_events.TeamRunOutput(run_id="r1", team_name="t"))
        assert isinstance(proto, msg.RunCompleted)
        assert proto.run_id == "r1"

    def test_other_objects_match_no_group(self):
        for value in (object(), "x", None, agent_events.RunStartedEvent(run_id="r")):
            for attr in _TEAM_ONLY:
                assert not isinstance(value, getattr(AgnoEventTaxonomy, attr))
        assert not isinstance(object(), tax.RUN_STARTED_EVENTS)

    def test_distinct_event_kinds_do_not_overlap(self):
        started = agent_events.RunStartedEvent(run_id="r")
        assert not isinstance(started, tax.RUN_COMPLETED_EVENTS)
        assert not isinstance(started, tax.RUN_ERROR_EVENTS)
        assert not isinstance(started, tax.CONTENT_EVENTS)


class TestModuleConstants:
    def test_each_constant_is_the_class_attribute(self):
        for attr in list(_BOTH) + list(_TEAM_ONLY) + ["RUN_COMPLETED"]:
            assert getattr(tax, f"{attr}_EVENTS") is getattr(AgnoEventTaxonomy, attr)

    def test_all_lists_the_class_and_every_constant(self):
        expected = {"AgnoEventTaxonomy"} | {
            f"{attr}_EVENTS" for attr in list(_BOTH) + list(_TEAM_ONLY) + ["RUN_COMPLETED"]
        }
        assert set(tax.__all__) == expected
