"""The spec's ``instantiate`` hands the Agno agent each field as its own kwarg.

The spec is a field list with one job: turn into the ``Agent(...)`` call.
A kwarg given the wrong field's value (``stream`` for ``stream_events``,
``db`` for ``session_id``) builds an agent that runs, so only a test that
gives every field a value of its own can see it. The instructions are the
exception that is meant: they are rendered as igni's model was trained to
read them, and Agno's ``markdown`` is turned off in return.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.core.agents.trained_layout import MARKDOWN_LINE
from igni_core.core.session.agent_builder.agent_build_spec import AgentBuildSpec


def _spec(**overrides: Any) -> AgentBuildSpec:
    """A spec in which no two fields hold the same value, bools alternating."""
    fields: dict[str, Any] = {
        "name": "the-name",
        "model": object(),
        "tools": ["tool-a"],
        "instructions": ["only instruction"],
        "markdown": True,
        "retries": 3,
        "db": object(),
        "session_id": "sess-1",
        "user_id": "user-1",
        "add_history_to_context": True,
        "num_history_runs": 7,
        "enable_agentic_memory": False,
        "add_memories_to_context": True,
        "compress_tool_results": False,
        "compression_manager": object(),
        "enable_session_summaries": True,
        "add_session_summary_to_context": False,
        "stream": True,
        "stream_events": False,
        "knowledge": object(),
        "search_knowledge": True,
        "pre_hooks": ["guard"],
        "learning": object(),
        "add_learnings_to_context": False,
        "tool_hooks": ["hook-1", "hook-2"],
    }
    fields.update(overrides)
    return AgentBuildSpec(**fields)


def _kwargs(spec: AgentBuildSpec) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def agent_cls(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "the-agent"

    assert spec.instantiate(agent_cls) == "the-agent"
    return seen


def test_every_field_reaches_the_kwarg_of_the_same_name():
    spec = _spec()
    seen = _kwargs(spec)
    untouched = set(AgentBuildSpec.model_fields) - {"instructions", "markdown"}
    for field in untouched:
        assert seen[field] is getattr(spec, field), field
    assert set(seen) == set(AgentBuildSpec.model_fields)


def test_a_single_instruction_and_the_markdown_flag_pass_through_unchanged():
    seen = _kwargs(_spec(instructions=["one"], markdown=True))
    assert seen["instructions"] == ["one"]
    assert seen["markdown"] is True


def test_several_instructions_are_rendered_as_the_model_was_trained_and_markdown_is_off():
    seen = _kwargs(_spec(instructions=["first", "second"], markdown=True))
    assert seen["instructions"] == [
        f"- first\n- second\n<additional_information>\n- {MARKDOWN_LINE}\n</additional_information>"
    ]
    assert seen["markdown"] is False


def test_an_empty_instruction_list_stays_empty():
    seen = _kwargs(_spec(instructions=[], markdown=True))
    assert seen["instructions"] == []
    assert seen["markdown"] is True


def test_several_instructions_without_markdown_are_left_to_agno():
    seen = _kwargs(_spec(instructions=["first", "second"], markdown=False))
    assert seen["instructions"] == ["first", "second"]
    assert seen["markdown"] is False


def test_no_run_is_reported_to_agno_s_telemetry_unless_the_spec_says_so():
    assert _kwargs(_spec())["telemetry"] is False
    assert _kwargs(_spec(telemetry=True))["telemetry"] is True


def test_a_spec_without_a_required_field_is_refused():
    fields = dict(_spec())
    del fields["session_id"]
    with pytest.raises(ValidationError):
        AgentBuildSpec(**fields)


def test_a_failure_in_the_agent_class_reaches_the_caller():
    def agent_cls(**kwargs: Any) -> None:
        raise RuntimeError("agno refused")

    with pytest.raises(RuntimeError, match="agno refused"):
        _spec().instantiate(agent_cls)
