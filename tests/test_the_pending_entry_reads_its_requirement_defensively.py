"""A pending question reads the requirement it holds without trusting its shape.

``PendingEntry`` keeps Agno's ``RunRequirement`` or a safety question as it is, and the
accessors, ``explain`` and ``request`` are the one place that knows what each carries.
"""

from __future__ import annotations

from types import SimpleNamespace

from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from igni_core.core.hitl.models import DetailUpdate, PendingEntry, PendingHandle, RestoredRun


def _run_requirement(name: str = "run_shell_command", args: dict | None = None) -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_call_id="c1", tool_name=name, tool_args=args or {"command": "ls"}
        )
    )


def test_a_requirement_without_a_tool_call_reads_as_empty():
    entry = PendingEntry(requirement=object(), run_id="r1")

    assert entry.tool_name == ""
    assert entry.tool_args == {}
    assert entry.details == ""
    assert entry.details_pending is False


def test_a_tool_call_with_no_name_or_arguments_reads_as_empty():
    execution = SimpleNamespace(tool_name=None, tool_args=None)
    entry = PendingEntry(requirement=SimpleNamespace(tool_execution=execution), run_id="r1")

    assert entry.tool_name == ""
    assert entry.tool_args == {}


def test_the_arguments_are_a_copy_the_caller_may_change():
    requirement = _run_requirement(args={"command": "ls"})
    entry = PendingEntry(requirement=requirement, run_id="r1")

    entry.tool_args["command"] = "rm -rf /"

    assert requirement.tool_execution.tool_args == {"command": "ls"}


def test_explain_sets_the_text_and_clears_the_pending_flag_a_safety_question_has():
    requirement = SimpleNamespace(
        tool_execution=SimpleNamespace(tool_name="t", tool_args={}),
        details="",
        details_pending=True,
    )
    entry = PendingEntry(requirement=requirement, run_id="r1")

    entry.explain("it deletes files")

    assert (entry.details, entry.details_pending) == ("it deletes files", False)


def test_explain_on_an_agno_requirement_sets_the_text_and_adds_no_flag():
    requirement = SimpleNamespace(tool_execution=None)
    entry = PendingEntry(requirement=requirement, run_id="r1")

    entry.explain("why")

    assert entry.details == "why"
    assert not hasattr(requirement, "details_pending")
    assert entry.details_pending is False


def test_the_request_a_window_sees_carries_the_raw_and_friendly_name_and_its_agent_path():
    entry = PendingEntry(
        requirement=_run_requirement(), run_id="r1", agent_path=["architect", "reviewer"]
    )
    entry.explain("because")

    request = entry.request("req-9")

    assert request.requirement_id == "req-9"
    assert request.tool_name == "run_shell_command"
    assert request.friendly_name == "Bash"
    assert request.tool_args == {"command": "ls"}
    assert request.details == "because"
    assert request.details_pending is False
    assert request.agent_path == ["architect", "reviewer"]


def test_an_unknown_tool_is_shown_under_its_own_name_and_an_empty_requirement_as_blank():
    unknown = PendingEntry(requirement=_run_requirement("my_custom_tool"), run_id="r1")
    blank = PendingEntry(requirement=object(), run_id="r1")

    assert unknown.request("a").friendly_name == "my_custom_tool"
    assert (blank.request("b").tool_name, blank.request("b").friendly_name) == ("", "")


def test_a_new_entry_is_unsurfaced_unanswered_and_has_its_own_path_list():
    first = PendingEntry(requirement=object(), run_id="r1")
    second = PendingEntry(requirement=object(), run_id="r2")

    first.agent_path.append("a")

    assert (first.surfaced, first.event.is_set()) == (False, False)
    assert second.agent_path == []
    assert first.event is not second.event


def test_the_handle_update_and_restored_run_carry_what_they_are_given():
    entry = PendingEntry(requirement=object(), run_id="r1")
    handle = PendingHandle(requirement_id="q1", entry=entry)
    update = DetailUpdate(requirement_id="q1", details="text")
    restored = RestoredRun(run_id="r1", requirements=[])

    assert handle.entry is entry
    assert (update.requirement_id, update.details) == ("q1", "text")
    assert restored.link is None
