"""``AgnoHistoryPlanScanner``: the most recent ``exit_plan_mode`` call in a session."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

from igni_core.backend.agno_history_plan_scanner import AgnoHistoryPlanScanner, PlanArgs


def _call(arguments, name: str = "exit_plan_mode") -> dict:
    return {"function": {"name": name, "arguments": arguments}}


def _plan(plan: str, **extra) -> dict:
    return _call(json.dumps({"plan": plan, **extra}))


def _run(run_id: str, *messages) -> NS:
    return NS(run_id=run_id, messages=list(messages))


def _assistant(*calls) -> NS:
    return NS(role="assistant", tool_calls=list(calls))


def _find(*runs):
    return AgnoHistoryPlanScanner(NS(runs=list(runs))).find_latest_plan()


def test_the_latest_of_several_plans_wins_with_its_run_id():
    found = _find(
        _run("r1", _assistant(_plan("old plan"))),
        _run("r2", _assistant(_plan("first of r2")), _assistant(_plan("newest"))),
    )

    assert found == (PlanArgs(plan="newest"), "r2")


def test_arguments_may_be_an_already_decoded_dict():
    found = _find(_run("r1", _assistant(_call({"plan": " do it ", "tasks": [{"a": 1}]}))))

    assert found == (PlanArgs(plan="do it", tasks=[{"a": 1}]), "r1")


def test_tasks_that_are_not_a_list_become_none_and_extras_are_ignored():
    found = _find(_run("r1", _assistant(_plan("p", tasks="nope", confidence=9))))

    assert found == (PlanArgs(plan="p", tasks=None), "r1")


def test_broken_calls_are_skipped_and_the_scan_goes_on_to_an_older_plan():
    broken = _assistant(
        _call("{not json"),
        _call(json.dumps(["a", "list"])),
        _call(None),
        _plan("   "),
        _call(json.dumps({"tasks": []})),
        _plan(
            "other tool",
        )
        | {"function": {"name": "edit_file", "arguments": "{}"}},
        "not a dict",
        {"function": "not a dict"},
    )
    found = _find(_run("r1", _assistant(_plan("the good one"))), _run("r2", broken))

    assert found == (PlanArgs(plan="the good one"), "r1")


def test_only_assistant_messages_count():
    user = NS(role="user", tool_calls=[_plan("not mine")])

    assert _find(_run("r1", user)) is None


def test_a_session_with_no_runs_or_no_messages_or_no_calls_has_no_plan():
    assert AgnoHistoryPlanScanner(NS(runs=None)).find_latest_plan() is None
    assert _find() is None
    assert _find(NS(run_id="r", messages=None)) is None
    assert _find(_run("r", NS(role="assistant", tool_calls=None))) is None


def test_a_run_without_an_id_gives_an_empty_run_id():
    found = _find(NS(messages=[_assistant(_plan("p"))]))

    assert found is not None and found[1] == ""
