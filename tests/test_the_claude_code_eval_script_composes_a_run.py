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
