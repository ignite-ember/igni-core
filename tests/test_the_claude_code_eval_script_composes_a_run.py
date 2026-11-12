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
import os
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

    code = _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.root / "nope"),
        "--out",
        str(_out(home)),
        "--no-judge",
    )

    assert code == 1
    assert "target dir does not exist" in capsys.readouterr().err


def test_a_missing_claude_binary_says_so_and_exits_1(home, monkeypatch, tmp_path, capsys):
    empty = tmp_path / "no-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    name = _suite(home, [{"name": "a"}])

    code = _run(
        home,
        monkeypatch,
        "--suite",
        name,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(_out(home)),
        "--no-judge",
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "claude CLI was not found on PATH" in err
    assert "Traceback" not in err
    assert not _out(home).exists()


def test_a_missing_suite_exits_1_naming_the_yaml(home, monkeypatch, capsys):
    code = _run(
        home,
        monkeypatch,
        "--suite",
        "absent",
        "--target-project-dir",
        str(home.target),
        "--out",
        str(_out(home)),
        "--no-judge",
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "suite yaml not found" in err
    assert "absent.yaml" in err


def test_a_suite_that_fails_to_load_exits_1(home, monkeypatch, capsys):
    (home.root / "evals/bad.yaml").write_text("just: a mapping\n")

    code = _run(
        home,
        monkeypatch,
        "--suite",
        "bad",
        "--target-project-dir",
        str(home.target),
        "--out",
        str(_out(home)),
        "--no-judge",
    )

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

    _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(_out(home)),
        "--no-judge",
    )

    assert {c["prompt"]: c["timeout"] for c in calls} == {"unset": 420, "short": 120, "long": 460}
    assert all(c["cwd"] == home.target.resolve() for c in calls)


# -- pass or fail ------------------------------------------------------------


def test_without_a_judge_a_case_passes_on_a_non_empty_response_and_fails_on_an_empty_one(
    home, monkeypatch
):
    suite = _suite(home, [{"name": "full", "input": "full"}, {"name": "empty", "input": "empty"}])
    _fake_claude(home, monkeypatch, {"empty": ("", {})})
    out = _out(home)

    code = _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(out),
        "--no-judge",
    )

    result = json.loads(out.read_text())
    assert [c["passed"] for c in result["cases"]] == [True, False]
    assert result["totals"]["passed"] == 1
    assert result["totals"]["failed"] == 1
    assert result["totals"]["total"] == 2
    assert code == 1


def test_a_case_that_errored_fails_even_when_a_response_text_exists(home, monkeypatch):
    suite = _suite(home, [{"name": "a", "input": "a"}])
    _fake_claude(
        home, monkeypatch, {"a": ("partial text", {"error": "claude exit 2", "stderr": "boom"})}
    )
    out = _out(home)

    code = _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(out),
        "--no-judge",
    )

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

    code = _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(out),
        "--no-judge",
    )

    result = json.loads(out.read_text())
    assert code == 0
    assert list(result) == ["mode", "target_project_dir", "cases", "totals"]
    assert result["mode"] == "claude-code"
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
    assert (result["totals"]["passed"], result["totals"]["failed"], result["totals"]["total"]) == (
        1,
        0,
        1,
    )


def test_an_empty_suite_writes_zero_totals_and_exits_0(home, monkeypatch):
    (home.root / "evals/none.yaml").write_text("agent: main\ncases: []\n")
    out = _out(home)

    code = _run(
        home,
        monkeypatch,
        "--suite",
        "none",
        "--target-project-dir",
        str(home.target),
        "--out",
        str(out),
        "--no-judge",
    )

    result = json.loads(out.read_text())
    assert code == 0
    assert result["cases"] == []
    assert (result["totals"]["passed"], result["totals"]["failed"], result["totals"]["total"]) == (
        0,
        0,
        0,
    )


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

    code = _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(out),
    )

    good, bad = json.loads(out.read_text())["cases"]
    assert graded == ["good", "bad"]
    assert (
        good["passed"],
        good["accuracy_passed"],
        good["accuracy_score"],
        good["accuracy_reason"],
    ) == (True, True, 9.0, "fine")
    assert (
        bad["passed"],
        bad["accuracy_passed"],
        bad["accuracy_score"],
        bad["accuracy_reason"],
    ) == (False, False, 3.0, "wrong")
    assert code == 1


def test_an_empty_response_is_not_sent_to_the_judge(home, monkeypatch):
    suite = _suite(home, [{"name": "a", "input": "a", "expected_output": "x"}])
    _fake_claude(home, monkeypatch, {"a": ("", {})})
    graded = _with_judge(home, monkeypatch, lambda case, text: (True, 10.0, ""))

    _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(_out(home)),
    )

    assert graded == []


def test_a_judge_that_raises_is_recorded_and_the_case_falls_back_to_the_response(home, monkeypatch):
    suite = _suite(home, [{"name": "a", "input": "a", "expected_output": "x"}])
    _fake_claude(home, monkeypatch, {})

    def explode(case, text):
        raise RuntimeError("judge down")

    _with_judge(home, monkeypatch, explode)
    out = _out(home)

    code = _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(out),
    )

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

    _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(out),
    )

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

    _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(_out(home)),
    )

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

    _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(_out(home)),
    )

    assert "Judge model: judge-1 @ https://judge.example/v1" in capsys.readouterr().err
    assert seen["model"].id == "judge-1"
    assert str(seen["model"].base_url) == "https://judge.example/v1"


def test_without_a_key_the_run_warns_and_goes_on_without_a_judge(home, monkeypatch, capsys):
    suite = _suite(home, [{"name": "a", "input": "a", "expected_output": "x"}])
    _fake_claude(home, monkeypatch, {})
    seen = _judge_seen(home, monkeypatch)
    out = _out(home)

    code = _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(out),
    )

    assert "WARNING: IGNI_TEST_LLM_API_KEY not set" in capsys.readouterr().err
    assert seen == {}
    assert code == 0


def test_no_judge_skips_the_judge_even_with_a_key_and_says_nothing_about_it(
    home, monkeypatch, capsys
):
    suite = _suite(home, [{"name": "a", "input": "a", "expected_output": "x"}])
    _fake_claude(home, monkeypatch, {})
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "key")
    seen = _judge_seen(home, monkeypatch)

    _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(_out(home)),
        "--no-judge",
    )

    err = capsys.readouterr().err
    assert seen == {}
    assert "Judge model" not in err
    assert "WARNING" not in err


# -- the project .env --------------------------------------------------------


def _run_quietly(home, monkeypatch):
    suite = _suite(home, [{"name": "a", "input": "a"}])
    _fake_claude(home, monkeypatch, {})
    return _run(
        home,
        monkeypatch,
        "--suite",
        suite,
        "--target-project-dir",
        str(home.target),
        "--out",
        str(_out(home)),
        "--no-judge",
    )


def test_the_project_env_supplies_the_judge_settings_and_skips_comments_and_bare_lines(
    home, monkeypatch
):
    (home.root / ".env").write_text(
        "# a comment\n"
        "\n"
        "no equals sign here\n"
        "IGNI_TEST_LLM_API_KEY=abc\n"
        "IGNI_TEST_LLM_MODEL = judge-2 \n"
    )

    _run_quietly(home, monkeypatch)

    import os

    assert os.environ["IGNI_TEST_LLM_API_KEY"] == "abc"
    assert os.environ["IGNI_TEST_LLM_MODEL"] == "judge-2"


def test_a_quoted_value_in_the_project_env_is_stored_without_its_quotes(home, monkeypatch):
    (home.root / ".env").write_text(
        "IGNI_TEST_LLM_API_KEY=\"abc\"\nIGNI_TEST_LLM_BASE_URL='https://judge.example/v1'\n"
    )

    _run_quietly(home, monkeypatch)

    import os

    assert os.environ["IGNI_TEST_LLM_API_KEY"] == "abc"
    assert os.environ["IGNI_TEST_LLM_BASE_URL"] == "https://judge.example/v1"


def test_the_project_env_never_overrides_what_the_environment_already_sets(home, monkeypatch):
    (home.root / ".env").write_text(
        "IGNI_TEST_LLM_MODEL=from-file\nIGNI_TEST_LLM_BASE_URL=https://file/v1\n"
    )
    monkeypatch.setenv("IGNI_TEST_LLM_MODEL", "from-shell")

    _run_quietly(home, monkeypatch)

    import os

    assert os.environ["IGNI_TEST_LLM_MODEL"] == "from-shell"
    assert os.environ["IGNI_TEST_LLM_BASE_URL"] == "https://file/v1"


def test_the_project_env_is_not_read_when_the_key_is_already_set(home, monkeypatch):
    (home.root / ".env").write_text("IGNI_TEST_LLM_MODEL=from-file\n")
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "shell-key")

    _run_quietly(home, monkeypatch)

    import os

    assert "IGNI_TEST_LLM_MODEL" not in os.environ


# -- the default suite -------------------------------------------------------


@pytest.fixture
def claude_on_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text('#!/bin/sh\necho \'{"result": "ok"}\'\n')
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def _load_real(monkeypatch):
    spec = importlib.util.spec_from_file_location("claude_code_eval_real_under_test", _SOURCE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_the_default_suite_is_one_the_repository_ships(tmp_path, monkeypatch, claude_on_path):
    """Run the real script with no ``--suite``: it must find a suite and run its cases."""
    from igni_core.core.evals.loader import load_eval_file

    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "set-so-the-repo-env-is-not-read")
    module = _load_real(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "x",
            "--target-project-dir",
            str(tmp_path),
            "--out",
            str(tmp_path / "o.json"),
            "--no-judge",
        ],
    )

    code = asyncio.run(module.main())

    result = json.loads((tmp_path / "o.json").read_text())
    shipped = load_eval_file(_SOURCE.parent.parent / "evals/main_agent.yaml")
    assert code == 0
    assert result["totals"]["total"] == len(shipped.cases) > 0
