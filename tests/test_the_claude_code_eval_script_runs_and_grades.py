"""``scripts/run_claude_code_eval.py`` runs an eval suite through ``claude -p``.

It shells out to the ``claude`` CLI and to an LLM judge, so nothing ran
it: what a timeout, a failing exit and an unparsable answer look like in
the result, which model name ends up in the metadata, how a verdict is
combined with the response into pass or fail, and what the output file
and the exit code say were all unpinned.

``claude`` is a small shell script placed first on ``PATH`` that prints
what the test prepared, so the script's real subprocess handling runs.
The judge's ``AccuracyEval`` is replaced where the grading is tested; the
script runs from a copy placed in ``<tmp>/scripts`` so its ``.env`` and
``evals`` directory are the test's, not the maintainer's.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import shutil
import stat
import sys
import types

import pytest

_SOURCE = pathlib.Path(__file__).resolve().parent.parent / "scripts/run_claude_code_eval.py"

_FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CLAUDE_ARGV"
pwd > "$FAKE_CLAUDE_CWD"
[ -n "$FAKE_CLAUDE_SLEEP" ] && sleep "$FAKE_CLAUDE_SLEEP"
[ -f "$FAKE_CLAUDE_STDERR" ] && cat "$FAKE_CLAUDE_STDERR" >&2
[ -f "$FAKE_CLAUDE_STDOUT" ] && cat "$FAKE_CLAUDE_STDOUT"
exit "${FAKE_CLAUDE_EXIT:-0}"
"""


@pytest.fixture
def claude(tmp_path, monkeypatch):
    """A ``claude`` on PATH that answers from files the test writes."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(_FAKE_CLAUDE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_ARGV", str(state / "argv"))
    monkeypatch.setenv("FAKE_CLAUDE_CWD", str(state / "cwd"))
    monkeypatch.setenv("FAKE_CLAUDE_STDOUT", str(state / "stdout"))
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", str(state / "stderr"))
    monkeypatch.delenv("FAKE_CLAUDE_EXIT", raising=False)
    monkeypatch.delenv("FAKE_CLAUDE_SLEEP", raising=False)

    class Claude:
        def answers(self, payload=None, *, raw=None, stderr=None, exit_code=0):
            if raw is not None:
                (state / "stdout").write_text(raw)
            elif payload is not None:
                (state / "stdout").write_text(json.dumps(payload))
            if stderr is not None:
                (state / "stderr").write_text(stderr)
            monkeypatch.setenv("FAKE_CLAUDE_EXIT", str(exit_code))

        def argv(self):
            return (state / "argv").read_text().splitlines()

        def cwd(self):
            return pathlib.Path((state / "cwd").read_text().strip())

    return Claude()


def _load(path, monkeypatch, name="claude_code_eval_under_test"):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ev(monkeypatch):
    return _load(_SOURCE, monkeypatch)


# -- _run_claude_code --------------------------------------------------------


def test_claude_is_run_one_shot_in_the_target_dir_with_json_output(ev, claude, tmp_path):
    claude.answers({"result": "hello"})
    target = tmp_path / "target"
    target.mkdir()

    text, elapsed, meta = ev._run_claude_code("what is this?", target, 30)

    assert claude.argv() == [
        "-p",
        "what is this?",
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
    ]
    assert claude.cwd() == target.resolve()
    assert text == "hello"
    assert elapsed >= 0


def test_the_metadata_carries_the_session_turns_durations_cost_and_model(ev, claude, tmp_path):
    claude.answers(
        {
            "result": "ok",
            "session_id": "s-1",
            "num_turns": 3,
            "duration_ms": 1200,
            "duration_api_ms": 900,
            "total_cost_usd": 0.0421,
            "model": "claude-x",
        }
    )

    _, _, meta = ev._run_claude_code("p", tmp_path, 30)

    assert meta == {
        "session_id": "s-1",
        "num_turns": 3,
        "duration_ms": 1200,
        "duration_api_ms": 900,
        "total_cost_usd": 0.0421,
        "model": "claude-x",
    }


def test_the_model_is_read_from_model_usage_when_the_payload_names_none(ev, claude, tmp_path):
    claude.answers({"result": "ok", "modelUsage": {"model": "claude-from-usage"}})

    _, _, meta = ev._run_claude_code("p", tmp_path, 30)

    assert meta["model"] == "claude-from-usage"


def test_the_payload_model_wins_over_model_usage(ev, claude, tmp_path):
    claude.answers({"result": "ok", "model": "top", "modelUsage": {"model": "usage"}})

    _, _, meta = ev._run_claude_code("p", tmp_path, 30)

    assert meta["model"] == "top"


def test_a_payload_model_is_kept_when_model_usage_is_not_a_dict(ev, claude, tmp_path):
    claude.answers({"result": "ok", "model": "claude-x", "modelUsage": ["not", "a", "dict"]})

    _, _, meta = ev._run_claude_code("p", tmp_path, 30)

    assert meta["model"] == "claude-x"


def test_the_model_is_none_when_nothing_names_one(ev, claude, tmp_path):
    claude.answers({"result": "ok", "modelUsage": "nope"})

    _, _, meta = ev._run_claude_code("p", tmp_path, 30)

    assert meta["model"] is None


def test_the_response_falls_back_to_the_response_field_then_to_empty(ev, claude, tmp_path):
    claude.answers({"response": "from response"})
    assert ev._run_claude_code("p", tmp_path, 30)[0] == "from response"

    claude.answers({"result": "", "response": "second"})
    assert ev._run_claude_code("p", tmp_path, 30)[0] == "second"

    claude.answers({"session_id": "only"})
    assert ev._run_claude_code("p", tmp_path, 30)[0] == ""


def test_a_failing_exit_gives_empty_text_and_the_last_2000_chars_of_stderr(ev, claude, tmp_path):
    claude.answers(raw="", stderr="x" * 2500 + "THE END", exit_code=3)

    text, _, meta = ev._run_claude_code("p", tmp_path, 30)

    assert text == ""
    assert meta["error"] == "claude exit 3"
    assert len(meta["stderr"]) == 2000
    assert meta["stderr"].endswith("THE END")


def test_a_failing_exit_with_no_stderr_has_an_empty_stderr_field(ev, claude, tmp_path):
    claude.answers(raw="", exit_code=1)

    _, _, meta = ev._run_claude_code("p", tmp_path, 30)

    assert meta == {"error": "claude exit 1", "stderr": ""}


def test_stdout_that_is_not_json_is_reported_with_the_first_1000_chars(ev, claude, tmp_path):
    claude.answers(raw="not json " + "y" * 1500)

    text, _, meta = ev._run_claude_code("p", tmp_path, 30)

    assert text == ""
    assert meta["error"].startswith("json decode: ")
    assert meta["raw"].startswith("not json ")
    assert len(meta["raw"]) == 1000


def test_a_run_past_its_timeout_gives_empty_text_and_a_timeout_error(ev, claude, tmp_path, monkeypatch):
    claude.answers({"result": "too late"})
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "5")

    text, elapsed, meta = ev._run_claude_code("p", tmp_path, 1)

    assert text == ""
    assert meta == {"error": "timeout", "stderr": ""}
    assert 1 <= elapsed < 5


def test_unicode_in_the_prompt_and_the_answer_survives(ev, claude, tmp_path):
    claude.answers({"result": "héllo ✓"})

    text, _, _ = ev._run_claude_code("pröbe ✓", tmp_path, 30)

    assert text == "héllo ✓"
    assert claude.argv()[1] == "pröbe ✓"


# -- _grade_response ---------------------------------------------------------


def _case(**kw):
    base = dict(
        input="q",
        expected_output="a",
        judge_guidelines="be strict",
        num_iterations=2,
        accuracy_threshold=7,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _fake_accuracy(monkeypatch, result):
    seen = {}

    class Agent:
        def __init__(self, **kw):
            seen["agent"] = kw

    class AccuracyEval:
        def __init__(self, **kw):
            seen["eval"] = kw

        async def arun_with_output(self, **kw):
            seen["run"] = kw
            return result

    import agno.agent
    import agno.eval.accuracy

    monkeypatch.setattr(agno.agent, "Agent", Agent)
    monkeypatch.setattr(agno.eval.accuracy, "AccuracyEval", AccuracyEval)
    return seen


def _verdict(score, *reasons):
    return types.SimpleNamespace(
        avg_score=score, results=[types.SimpleNamespace(reason=r) for r in reasons]
    )


def _grade(ev, case, text="the answer", judge="judge"):
    return asyncio.run(ev._grade_response(case, text, judge))


def test_a_case_without_expected_output_is_not_graded(ev, monkeypatch):
    seen = _fake_accuracy(monkeypatch, _verdict(10))

    assert _grade(ev, _case(expected_output="")) == (None, None, "")
    assert _grade(ev, _case(expected_output=None)) == (None, None, "")
    assert seen == {}


def test_without_a_judge_model_nothing_is_graded(ev, monkeypatch):
    seen = _fake_accuracy(monkeypatch, _verdict(10))

    assert _grade(ev, _case(), judge=None) == (None, None, "")
    assert seen == {}


def test_a_missing_accuracy_eval_skips_grading(ev, monkeypatch):
    monkeypatch.setitem(sys.modules, "agno.eval.accuracy", None)

    assert _grade(ev, _case()) == (None, None, "")


def test_the_score_passes_at_the_threshold_and_fails_below_it(ev, monkeypatch):
    _fake_accuracy(monkeypatch, _verdict(7.0))
    assert _grade(ev, _case(accuracy_threshold=7))[:2] == (True, 7.0)

    _fake_accuracy(monkeypatch, _verdict(6.9))
    assert _grade(ev, _case(accuracy_threshold=7))[:2] == (False, 6.9)


def test_the_reasons_are_joined_and_empty_ones_dropped(ev, monkeypatch):
    _fake_accuracy(monkeypatch, _verdict(9, "first", "", "second"))

    assert _grade(ev, _case()) == (True, 9, "first\n---\nsecond")


def test_a_verdict_without_results_has_an_empty_reason(ev, monkeypatch):
    _fake_accuracy(monkeypatch, types.SimpleNamespace(avg_score=8, results=None))

    assert _grade(ev, _case()) == (True, 8, "")


def test_an_eval_that_returns_nothing_fails_the_case_with_a_reason(ev, monkeypatch):
    _fake_accuracy(monkeypatch, None)

    assert _grade(ev, _case()) == (False, None, "accuracy eval returned None")


def test_the_judge_is_given_the_response_the_guidelines_and_no_telemetry(ev, monkeypatch):
    seen = _fake_accuracy(monkeypatch, _verdict(9))

    _grade(ev, _case(), text="what claude said", judge="J")

    assert seen["agent"] == {"model": "J", "telemetry": False}
    assert seen["eval"]["telemetry"] is False
    assert seen["eval"]["input"] == "q"
    assert seen["eval"]["expected_output"] == "a"
    assert seen["eval"]["additional_guidelines"] == "be strict"
    assert seen["eval"]["num_iterations"] == 2
    assert seen["eval"]["model"] == "J"
    assert seen["run"]["output"] == "what claude said"
