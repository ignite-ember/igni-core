"""``scripts/run_claude_code_eval.py`` turns a suite into a verdict file and an exit code.

``main`` loads a suite YAML, runs every case through ``claude -p``,
optionally has a judge grade the answer, and writes one JSON file. These
tests run it from a copy placed in ``<tmp>/scripts`` with its own
``evals`` directory and ``.env``, so what it reads is the test's. The
claude call and the judge are replaced per test by recorders: their own
behaviour is pinned in ``test_the_claude_code_eval_script_runs_and_grades``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import shutil
import sys

import pytest

_SOURCE = pathlib.Path(__file__).resolve().parent.parent / "scripts/run_claude_code_eval.py"

_ENV_NAMES = ("IGNI_TEST_LLM_API_KEY", "IGNI_TEST_LLM_BASE_URL", "IGNI_TEST_LLM_MODEL")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch repo: ``scripts/`` holding the script, ``evals/`` and an optional ``.env``."""
    for name in _ENV_NAMES:
        # The script's .env fallback writes os.environ; have it undone afterwards.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "evals").mkdir()
    copy = tmp_path / "scripts/run_claude_code_eval.py"
    shutil.copy(_SOURCE, copy)
    spec = importlib.util.spec_from_file_location("claude_code_eval_main_under_test", copy)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    # the script inserts its own dir into sys.path; keep that undone
    monkeypatch.setattr(sys, "path", list(sys.path))
    target = tmp_path / "target"
    target.mkdir()
    return type("Home", (), {"root": tmp_path, "module": module, "target": target})


def _suite(home, cases, name="s"):
    lines = ["agent: main", "cases:"]
    for case in cases:
        lines.append(f"  - name: {case['name']}")
        lines.append(f"    input: {json.dumps(case.get('input', 'do it'))}")
        if "expected_output" in case:
            lines.append(f"    expected_output: {json.dumps(case['expected_output'])}")
        if "case_timeout" in case:
            lines.append(f"    case_timeout: {case['case_timeout']}")
    (home.root / "evals" / f"{name}.yaml").write_text("\n".join(lines) + "\n")
    return name


def _run(home, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["run_claude_code_eval.py", *argv])
    return asyncio.run(home.module.main())


def _fake_claude(home, monkeypatch, answers):
    """Replace the claude call: ``answers`` maps case input to (text, meta)."""
    calls = []

    def run_claude_code(prompt, cwd, timeout):
        calls.append({"prompt": prompt, "cwd": cwd, "timeout": timeout})
        text, meta = answers.get(prompt, ("an answer", {"num_turns": 2, "total_cost_usd": 0.1}))
        return text, 1.5, dict(meta)

    monkeypatch.setattr(home.module, "_run_claude_code", run_claude_code)
    return calls


def _out(home):
    return home.root / "out" / "result.json"


# -- refusals ----------------------------------------------------------------


def test_a_missing_target_dir_exits_1(home, monkeypatch, capsys):
    suite = _suite(home, [{"name": "a"}])

    code = _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.root / "nope"), "--out", str(_out(home)), "--no-judge")

    assert code == 1
    assert "target dir does not exist" in capsys.readouterr().err


def test_a_missing_suite_exits_1_naming_the_yaml(home, monkeypatch, capsys):
    code = _run(home, monkeypatch, "--suite", "absent", "--target-project-dir", str(home.target), "--out", str(_out(home)), "--no-judge")

    assert code == 1
    err = capsys.readouterr().err
    assert "suite yaml not found" in err
    assert "absent.yaml" in err


def test_a_suite_that_fails_to_load_exits_1(home, monkeypatch, capsys):
    (home.root / "evals/bad.yaml").write_text("just: a mapping\n")

    code = _run(home, monkeypatch, "--suite", "bad", "--target-project-dir", str(home.target), "--out", str(_out(home)), "--no-judge")

    assert code == 1
    assert "failed to load suite" in capsys.readouterr().err
    assert not _out(home).exists()


# -- the per-case timeout ----------------------------------------------------


def test_the_per_case_timeout_is_the_case_timeout_plus_a_minute_never_under_120(home, monkeypatch):
    suite = _suite(
        home,
        [
            {"name": "unset", "input": "unset"},
            {"name": "short", "input": "short", "case_timeout": 30},
            {"name": "long", "input": "long", "case_timeout": 400},
        ],
    )
    calls = _fake_claude(home, monkeypatch, {})

    _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.target), "--out", str(_out(home)), "--no-judge")

    assert {c["prompt"]: c["timeout"] for c in calls} == {"unset": 420, "short": 120, "long": 460}
    assert all(c["cwd"] == home.target.resolve() for c in calls)


# -- pass or fail ------------------------------------------------------------


def test_without_a_judge_a_case_passes_on_a_non_empty_response_and_fails_on_an_empty_one(
    home, monkeypatch
):
    suite = _suite(home, [{"name": "full", "input": "full"}, {"name": "empty", "input": "empty"}])
    _fake_claude(home, monkeypatch, {"empty": ("", {})})
    out = _out(home)

    code = _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.target), "--out", str(out), "--no-judge")

    result = json.loads(out.read_text())
    assert [c["passed"] for c in result["cases"]] == [True, False]
    assert result["totals"]["passed"] == 1
    assert result["totals"]["failed"] == 1
    assert result["totals"]["total"] == 2
    assert code == 1


def test_a_case_that_errored_fails_even_when_a_response_text_exists(home, monkeypatch):
    suite = _suite(home, [{"name": "a", "input": "a"}])
    _fake_claude(home, monkeypatch, {"a": ("partial text", {"error": "claude exit 2", "stderr": "boom"})})
    out = _out(home)

    code = _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.target), "--out", str(out), "--no-judge")

    (case,) = json.loads(out.read_text())["cases"]
    assert case["passed"] is False
    assert case["error"] == "claude exit 2"
    assert case["response_text"] == "partial text"
    assert code == 1


# -- the output --------------------------------------------------------------


def test_the_output_file_has_the_mode_target_cases_and_totals_and_exit_0_when_all_pass(
    home, monkeypatch
):
    suite = _suite(home, [{"name": "a", "input": "ünï", "expected_output": "x"}])
    _fake_claude(home, monkeypatch, {"ünï": ("répons ✓", {"num_turns": 4, "total_cost_usd": 0.5})})
    out = home.root / "deep" / "er" / "result.json"

    code = _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.target), "--out", str(out), "--no-judge")

    result = json.loads(out.read_text())
    assert code == 0
    assert list(result) == ["mode", "target_project_dir", "cases", "totals"]
    assert result["mode"] == "claude-code (no CodeIndex)"
    assert result["target_project_dir"] == str(home.target.resolve())
    (case,) = result["cases"]
    assert case["case"]["name"] == "a"
    assert case["case"]["input"] == "ünï"
    assert case["response_text"] == "répons ✓"
    assert case["elapsed"] == 1.5
    assert case["claude_meta"] == {"num_turns": 4, "total_cost_usd": 0.5}
    assert case["error"] is None
    assert case["accuracy_passed"] is None
    assert case["accuracy_reason"] == ""
    assert set(result["totals"]) == {"passed", "failed", "total", "elapsed"}
    assert (result["totals"]["passed"], result["totals"]["failed"], result["totals"]["total"]) == (1, 0, 1)


def test_an_empty_suite_writes_zero_totals_and_exits_0(home, monkeypatch):
    (home.root / "evals/none.yaml").write_text("agent: main\ncases: []\n")
    out = _out(home)

    code = _run(home, monkeypatch, "--suite", "none", "--target-project-dir", str(home.target), "--out", str(out), "--no-judge")

    result = json.loads(out.read_text())
    assert code == 0
    assert result["cases"] == []
    assert (result["totals"]["passed"], result["totals"]["failed"], result["totals"]["total"]) == (0, 0, 0)


# -- the judge ---------------------------------------------------------------

def _with_judge(home, monkeypatch, grade):
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "key")
    graded = []

    async def grade_response(case, text, judge_model):
        graded.append(case.name)
        return grade(case, text)

    monkeypatch.setattr(home.module, "_grade_response", grade_response)
    return graded


def test_with_a_judge_the_verdict_decides_and_its_score_and_reason_are_recorded(home, monkeypatch):
    suite = _suite(
        home,
        [
            {"name": "good", "input": "good", "expected_output": "x"},
            {"name": "bad", "input": "bad", "expected_output": "x"},
        ],
    )
    _fake_claude(home, monkeypatch, {})
    graded = _with_judge(
        home,
        monkeypatch,
        lambda case, text: (True, 9.0, "fine") if case.name == "good" else (False, 3.0, "wrong"),
    )
    out = _out(home)

    code = _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.target), "--out", str(out))

    good, bad = json.loads(out.read_text())["cases"]
    assert graded == ["good", "bad"]
    assert (good["passed"], good["accuracy_passed"], good["accuracy_score"], good["accuracy_reason"]) == (True, True, 9.0, "fine")
    assert (bad["passed"], bad["accuracy_passed"], bad["accuracy_score"], bad["accuracy_reason"]) == (False, False, 3.0, "wrong")
    assert code == 1


def test_an_empty_response_is_not_sent_to_the_judge(home, monkeypatch):
    suite = _suite(home, [{"name": "a", "input": "a", "expected_output": "x"}])
    _fake_claude(home, monkeypatch, {"a": ("", {})})
    graded = _with_judge(home, monkeypatch, lambda case, text: (True, 10.0, ""))

    _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.target), "--out", str(_out(home)))

    assert graded == []


def test_a_judge_that_raises_is_recorded_and_the_case_falls_back_to_the_response(home, monkeypatch):
    suite = _suite(home, [{"name": "a", "input": "a", "expected_output": "x"}])
    _fake_claude(home, monkeypatch, {})

    def explode(case, text):
        raise RuntimeError("judge down")

    _with_judge(home, monkeypatch, explode)
    out = _out(home)

    code = _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.target), "--out", str(out))

    (case,) = json.loads(out.read_text())["cases"]
    assert case["accuracy_reason"] == "judge exception: judge down"
    assert case["accuracy_passed"] is None
    assert case["passed"] is True
    assert code == 0


def test_a_judge_that_has_no_verdict_leaves_the_response_to_decide(home, monkeypatch):
    suite = _suite(home, [{"name": "a", "input": "a"}])
    _fake_claude(home, monkeypatch, {})
    _with_judge(home, monkeypatch, lambda case, text: (None, None, ""))
    out = _out(home)

    _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.target), "--out", str(out))

    (case,) = json.loads(out.read_text())["cases"]
    assert case["passed"] is True
    assert case["accuracy_score"] is None


def _judge_seen(home, monkeypatch):
    """Record the model the script builds for the judge instead of grading."""
    seen = {}

    async def grade_response(case, text, judge_model):
        seen["model"] = judge_model
        return True, 9.0, ""

    monkeypatch.setattr(home.module, "_grade_response", grade_response)
    return seen


def test_the_judge_model_defaults_to_the_minimax_endpoint(home, monkeypatch, capsys):
    suite = _suite(home, [{"name": "a", "input": "a", "expected_output": "x"}])
    _fake_claude(home, monkeypatch, {})
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "key")
    seen = _judge_seen(home, monkeypatch)

    _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.target), "--out", str(_out(home)))

    assert "Judge model: MiniMax-M2.7 @ https://api.minimax.io/v1" in capsys.readouterr().err
    assert seen["model"].id == "MiniMax-M2.7"
    assert seen["model"].api_key == "key"
    assert str(seen["model"].base_url) == "https://api.minimax.io/v1"


def test_the_judge_model_follows_the_environment(home, monkeypatch, capsys):
    suite = _suite(home, [{"name": "a", "input": "a", "expected_output": "x"}])
    _fake_claude(home, monkeypatch, {})
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "key")
    monkeypatch.setenv("IGNI_TEST_LLM_BASE_URL", "https://judge.example/v1")
    monkeypatch.setenv("IGNI_TEST_LLM_MODEL", "judge-1")
    seen = _judge_seen(home, monkeypatch)

    _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.target), "--out", str(_out(home)))

    assert "Judge model: judge-1 @ https://judge.example/v1" in capsys.readouterr().err
    assert seen["model"].id == "judge-1"
    assert str(seen["model"].base_url) == "https://judge.example/v1"


def test_without_a_key_the_run_warns_and_goes_on_without_a_judge(home, monkeypatch, capsys):
    suite = _suite(home, [{"name": "a", "input": "a", "expected_output": "x"}])
    _fake_claude(home, monkeypatch, {})
    seen = _judge_seen(home, monkeypatch)
    out = _out(home)

    code = _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.target), "--out", str(out))

    assert "WARNING: IGNI_TEST_LLM_API_KEY not set" in capsys.readouterr().err
    assert seen == {}
    assert code == 0


def test_no_judge_skips_the_judge_even_with_a_key_and_says_nothing_about_it(home, monkeypatch, capsys):
    suite = _suite(home, [{"name": "a", "input": "a", "expected_output": "x"}])
    _fake_claude(home, monkeypatch, {})
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "key")
    seen = _judge_seen(home, monkeypatch)

    _run(home, monkeypatch, "--suite", suite, "--target-project-dir", str(home.target), "--out", str(_out(home)), "--no-judge")

    err = capsys.readouterr().err
    assert seen == {}
    assert "Judge model" not in err
    assert "WARNING" not in err
