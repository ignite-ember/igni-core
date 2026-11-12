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
