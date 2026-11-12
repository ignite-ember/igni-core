"""The live-model smoke runner (``scripts/run_evals_smoke.py``).

The scheduled live-model workflow runs this script against a real model;
nothing here needs one. The model-facing parts are replaced by a fake
case runner, so what is pinned is what the runner itself decides: how a
case is categorised, retried, timed out, reported and summarised, and
what it leaves on disk.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from igni_core.core.evals import runner as eval_runner
from igni_core.core.evals.loader import load_eval_file
from igni_core.core.evals.schemas import CaseResult, EvalCase, ToolTraceEntry

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "run_evals_smoke.py"


@pytest.fixture
def smoke(monkeypatch):
    """The script as a module, without leaving its DuckDuckGo stubs behind."""
    from agno.tools.duckduckgo import DuckDuckGoTools

    monkeypatch.setattr(DuckDuckGoTools, "web_search", DuckDuckGoTools.web_search)
    monkeypatch.setattr(DuckDuckGoTools, "search_news", DuckDuckGoTools.search_news)
    spec = importlib.util.spec_from_file_location("_run_evals_smoke_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _case(name: str = "c", **kw) -> EvalCase:
    return EvalCase(name=name, input="do it", **kw)


# ── Nothing in the script names the old product ────────────────────


def test_the_script_does_not_name_the_old_product():
    assert not re.search(r"\bember", SCRIPT.read_text(), re.IGNORECASE)


def test_every_editor_case_lands_in_the_category_its_name_asks_for(smoke):
    """The category keywords are matched against the shipped case names;
    a keyword that matches nothing sends its case to the catch-all."""
    suite = load_eval_file(REPO / "evals" / "editor.yaml")
    by_name = {c.name: smoke._categorize(c.name, "editor") for c in suite.cases}
    assert by_name["follows_igni_md_conventions"] == "E. verification"
    assert by_name["conflicting_igni_md_vs_request"] == "F. judgement"
    assert not [n for n in by_name if "ember" in n]


# ── What --help promises about timeouts ────────────────────────────


def _help_text(smoke, monkeypatch, capsys) -> str:
    monkeypatch.setattr(sys, "argv", ["run_evals_smoke.py", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(smoke.main())
    assert exit_info.value.code == 0
    return " ".join(capsys.readouterr().out.split())


def test_the_help_says_a_timed_out_case_is_retried(smoke, monkeypatch, capsys):
    """``_run_case`` retries a timeout like a model error; the help must not
    tell the person a stuck case is run only once."""
    text = _help_text(smoke, monkeypatch, capsys)
    assert "Does NOT retry on case_timeout" not in text
    assert "and on case_timeout" in text


def test_the_help_says_a_timed_out_case_is_reported_as_env(smoke, monkeypatch, capsys):
    """The suite prints a timeout as ENV, not FAIL (it is the environment's
    failure, not the eval's); the help must say the same."""
    text = _help_text(smoke, monkeypatch, capsys)
    assert "marked FAIL" not in text
    assert "reported as ENV with reason=case_timeout" in text


# ── Importing the script ───────────────────────────────────────────


def test_importing_the_script_quiets_the_loggers_and_stubs_web_search(smoke):
    import logging

    from agno.tools.duckduckgo import DuckDuckGoTools

    assert logging.getLogger("agno").level == logging.ERROR
    assert logging.getLogger("igni_core").level == logging.WARNING
    assert "no results" in DuckDuckGoTools.web_search(None, "anything").lower()
    assert "no results" in DuckDuckGoTools.search_news(None, "anything").lower()


# ── Auto-approving the sub-agent pauses ────────────────────────────


class _Requirement:
    def __init__(self):
        self.confirmed = False
        self.rejected = False
        self.tool_execution = None

    def confirm(self):
        self.confirmed = True

    def reject(self, note=""):
        self.rejected = True


def test_every_pause_a_specialist_raises_is_confirmed(smoke):
    from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator

    async def go():
        coord = SubAgentHITLCoordinator()
        first, second = _Requirement(), _Requirement()
        drain = asyncio.create_task(smoke._auto_approve_loop(coord))
        await coord.push_requirement(first, "run-1", ["architect"])
        await coord.push_requirement(second, "run-2", ["reviewer"])
        for _ in range(100):
            if first.confirmed and second.confirmed:
                break
            await asyncio.sleep(0.01)
        drain.cancel()
        await asyncio.wait_for(drain, 1)  # cancellation ends it quietly
        return first, second

    first, second = asyncio.run(go())
    assert first.confirmed and second.confirmed
    assert not first.rejected and not second.rejected


def test_a_failure_in_the_drain_does_not_end_it(smoke, capsys):
    class Coord:
        def __init__(self):
            self.new_arrival = asyncio.Event()
            self.calls = 0
            self.resolved: list[str] = []

        def list_new_pending(self):
            self.calls += 1
            self.new_arrival.clear()  # as the real registry does
            if self.calls == 1:
                raise RuntimeError("boom")
            return [type("H", (), {"requirement_id": "r1"})()]

        def resolve(self, req_id, action):
            self.resolved.append((req_id, action))

    async def go():
        coord = Coord()
        coord.new_arrival.set()
        drain = asyncio.create_task(smoke._auto_approve_loop(coord))
        for _ in range(100):
            if coord.calls == 1:
                coord.new_arrival.set()  # the next arrival
            if coord.resolved:
                break
            await asyncio.sleep(0.01)
        drain.cancel()
        await drain
        return coord

    coord = asyncio.run(go())
    assert coord.resolved == [("r1", "confirm")]
    assert "auto-approve drain error (continuing): boom" in capsys.readouterr().out


# ── Categories ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "agent", "category"),
    [
        ("uses_grep_for_search", "editor", "A. tool-selection"),
        ("rm_rf_refused", "editor", "B. anti-patterns"),
        ("minimal_diff_for_rename", "editor", "C. minimal-diff"),
        ("respects_tabs", "editor", "D. style"),
        ("runs_tests_after_edit", "editor", "E. verification"),
        ("ambiguous_task", "editor", "F. judgement"),
        ("something_else", "editor", "G. specific"),
        ("direct_simple_chat", "main", "A. Direct (no delegation)"),
        ("single_spawn_reviewer", "main", "B. Single-specialist"),
        ("broadcast_two", "main", "C. Broadcast"),
        ("tasks_mode_plan", "main", "D. Tasks mode"),
        ("anti_overspawn", "main", "E. Anti-patterns"),
        ("routing_to_docs", "main", "F. Routing"),
        ("unprefixed", "main", "uncategorized"),
        ("direct_simple_chat", "explorer", "uncategorized"),
    ],
)
def test_a_case_name_lands_in_its_category(smoke, name, agent, category):
    assert smoke._categorize(name, agent) == category


# ── The live model ─────────────────────────────────────────────────


def test_the_live_model_reads_its_endpoint_from_the_environment(smoke, monkeypatch):
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "key-1")
    monkeypatch.delenv("IGNI_TEST_LLM_MODEL", raising=False)
    monkeypatch.delenv("IGNI_TEST_LLM_BASE_URL", raising=False)
    model = smoke._build_live_model()
    assert (model.id, model.api_key, str(model.base_url)) == (
        "gpt-4o-mini",
        "key-1",
        "https://api.openai.com/v1",
    )
    monkeypatch.setenv("IGNI_TEST_LLM_MODEL", "m-x")
    monkeypatch.setenv("IGNI_TEST_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    model = smoke._build_live_model()
    assert (model.id, str(model.base_url)) == ("m-x", "http://127.0.0.1:9/v1")


def test_the_live_model_needs_a_key(smoke, monkeypatch):
    monkeypatch.delenv("IGNI_TEST_LLM_API_KEY", raising=False)
    with pytest.raises(KeyError):
        smoke._build_live_model()


# ── One case: retries, timeouts, cancellation ──────────────────────


@pytest.fixture
def fake_case_runner(monkeypatch):
    """Replace the engine's CaseRunner with one that plays a script of
    outcomes, and the backoff sleep with one that only records."""
    plan: list = []
    attempts: list[str] = []
    sleeps: list[float] = []

    class FakeRunner:
        def __init__(self, case, agent, judge, session_id=None, work_dir=None, **kw):
            self.case, self.session_id, self.work_dir = case, session_id, work_dir

        async def run(self):
            attempts.append(self.session_id)
            step = plan.pop(0) if plan else "hang"
            if step == "hang":
                await asyncio.Event().wait()
            if isinstance(step, BaseException):
                raise step
            return CaseResult(case=self.case, passed=True, elapsed=0.5)

    real_sleep = asyncio.sleep

    async def record_sleep(delay, *a, **kw):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(eval_runner, "CaseRunner", FakeRunner)
    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    return type("Rig", (), {"plan": plan, "attempts": attempts, "sleeps": sleeps})


def test_a_case_that_passes_is_run_once_with_its_own_session(smoke, fake_case_runner):
    fake_case_runner.plan[:] = ["ok"]
    result = asyncio.run(smoke._run_case(_case(), object(), None))
    assert result.passed
    assert len(fake_case_runner.attempts) == 1
    assert fake_case_runner.attempts[0].startswith("eval-")
    assert fake_case_runner.sleeps == []


def test_a_model_error_is_retried_with_a_growing_backoff(smoke, fake_case_runner):
    fake_case_runner.plan[:] = [RuntimeError("429"), RuntimeError("429"), "ok"]
    result = asyncio.run(smoke._run_case(_case(), object(), None, case_retries=2))
    assert result.passed
    assert len(fake_case_runner.attempts) == 3
    assert len(set(fake_case_runner.attempts)) == 3  # a fresh session each time
    assert fake_case_runner.sleeps == [2, 4]


def test_the_retries_run_out_in_an_error_result_not_an_exception(smoke, fake_case_runner):
    fake_case_runner.plan[:] = [RuntimeError("down")] * 3
    result = asyncio.run(smoke._run_case(_case(), object(), None, case_retries=2))
    assert not result.passed
    assert result.error == "model_error: down (after 3 attempts)"
    assert result.elapsed == 0.0
    assert len(fake_case_runner.attempts) == 3


def test_a_negative_retry_count_means_one_attempt(smoke, fake_case_runner):
    fake_case_runner.plan[:] = [RuntimeError("down")]
    result = asyncio.run(smoke._run_case(_case(), object(), None, case_retries=-5))
    assert result.error == "model_error: down (after 1 attempts)"
    assert len(fake_case_runner.attempts) == 1


def test_a_timed_out_case_is_retried_and_reports_its_timeout(smoke, fake_case_runner):
    result = asyncio.run(
        smoke._run_case(_case(case_timeout=0.05), object(), None, case_timeout=60, case_retries=1)
    )
    assert result.error == "case_timeout: exceeded 0s (after 2 attempts)"
    assert result.elapsed == 0.05
    assert len(fake_case_runner.attempts) == 2


def test_a_case_without_its_own_timeout_gets_the_default(smoke, fake_case_runner):
    for own in (None, 0):
        fake_case_runner.attempts.clear()
        result = asyncio.run(
            smoke._run_case(
                _case(case_timeout=own), object(), None, case_timeout=0.05, case_retries=0
            )
        )
        assert result.elapsed == 0.05
        assert result.error.startswith("case_timeout:")


def test_a_cancelled_case_stays_cancelled(smoke, fake_case_runner):
    fake_case_runner.plan[:] = [asyncio.CancelledError()]

    async def go():
        return await smoke._run_case(_case(), object(), None, case_retries=3)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())
    assert len(fake_case_runner.attempts) == 1


# ── One suite ──────────────────────────────────────────────────────

SUITE_YAML = """\
agent: editor
fixtures:
- source: proj
  target: proj
cases:
{cases}
"""


def _suite_file(tmp_path: Path, names: list[str]) -> Path:
    cases = "\n".join(f"- name: {n}\n  input: do {n}" for n in names)
    path = tmp_path / "suite.yaml"
    path.write_text(SUITE_YAML.format(cases=cases))
    return path


def _outcomes(smoke, monkeypatch, by_name: dict):
    """Make ``_run_case`` return (or raise) what ``by_name`` says per case."""
    dirs: dict[str, Path] = {}

    async def fake_run_case(case, agent, judge, *, work_dir, **kw):
        dirs[case.name] = work_dir
        spec = by_name[case.name]
        await asyncio.sleep(spec.get("delay", 0))
        if "raise" in spec:
            raise spec["raise"]
        return spec["result"](case)

    monkeypatch.setattr(smoke, "_run_case", fake_run_case)
    return dirs


def test_a_suite_prints_each_case_as_pass_env_or_fail_in_submission_order(
    smoke, tmp_path, monkeypatch, capsys
):
    names = ["slow_pass", "env_model", "env_timeout", "env_arun", "real_fail"]
    _outcomes(
        smoke,
        monkeypatch,
        {
            "slow_pass": {"delay": 0.2, "result": lambda c: CaseResult(case=c, passed=True)},
            "env_model": {"result": lambda c: CaseResult(case=c, error="model_error: 429 (x)")},
            "env_timeout": {
                "result": lambda c: CaseResult(case=c, error="case_timeout: exceeded 60s")
            },
            "env_arun": {"result": lambda c: CaseResult(case=c, error="agent.arun failed: boom")},
            "real_fail": {
                "result": lambda c: CaseResult(
                    case=c,
                    reliability_passed=False,
                    reliability_detail="never called grep",
                    unexpected_passed=False,
                    unexpected_detail="called rm",
                    tool_arg_passed=False,
                    tool_arg_detail="bad arg",
                    accuracy_score=3.0,
                    accuracy_passed=False,
                    error="judge said no",
                )
            },
        },
    )
    suite, results = asyncio.run(
        smoke._run_suite(_suite_file(tmp_path, names), lambda d: object(), None, None, None, 5)
    )
    out = capsys.readouterr().out.splitlines()
    lines = [ln for ln in out if ln.startswith("  [")]
    assert [ln.split()[2] for ln in lines] == ["PASS", "ENV", "ENV", "ENV", "FAIL"]
    assert [ln.split()[3] for ln in lines] == names
    assert "          rel: never called grep" in out
    assert "          unexp: called rm" in out
    assert "          arg: bad arg" in out
    assert "          acc: 3.0/7.0" in out
    assert "          err: judge said no" in out
    assert suite.agent == "editor"
    assert [c.name for c, _ in results] == names


def test_a_suite_runs_only_the_first_n_cases(smoke, tmp_path, monkeypatch, capsys):
    _outcomes(
        smoke,
        monkeypatch,
        {n: {"result": lambda c: CaseResult(case=c, passed=True)} for n in "abc"},
    )
    path = _suite_file(tmp_path, ["a", "b", "c"])
    _, results = asyncio.run(smoke._run_suite(path, lambda d: object(), None, 2, None, 5))
    assert [c.name for c, _ in results] == ["a", "b"]
    assert "running 2/3 cases" in capsys.readouterr().out
    _, results = asyncio.run(smoke._run_suite(path, lambda d: object(), None, 99, None, 5))
    assert len(results) == 3


def test_a_suite_that_cannot_be_loaded_is_reported_and_skipped(smoke, tmp_path, capsys):
    path = tmp_path / "broken.yaml"
    path.write_text("not: [a suite")
    assert asyncio.run(smoke._run_suite(path, lambda d: object(), None, None, None, 1)) is None
    assert f"could not load {path}" in capsys.readouterr().out


def test_cases_run_in_clean_work_dirs_that_are_removed_afterwards(smoke, tmp_path, monkeypatch):
    fixtures = tmp_path / "fx"
    (fixtures / "proj").mkdir(parents=True)
    (fixtures / "proj" / "app.py").write_text("print(1)\n")
    dirs = _outcomes(
        smoke,
        monkeypatch,
        {
            "ok": {"result": lambda c: CaseResult(case=c, passed=True)},
            "bad": {"result": lambda c: CaseResult(case=c, error="x")},
        },
    )
    seen: dict[str, str] = {}
    real = smoke._run_case

    async def peek(case, agent, judge, *, work_dir, **kw):
        seen[case.name] = (work_dir / "proj" / "app.py").read_text()
        (work_dir / "scribble").write_text(case.name)
        return await real(case, agent, judge, work_dir=work_dir, **kw)

    monkeypatch.setattr(smoke, "_run_case", peek)
    asyncio.run(
        smoke._run_suite(
            _suite_file(tmp_path, ["ok", "bad"]), lambda d: object(), None, None, fixtures, 1
        )
    )
    assert seen == {"ok": "print(1)\n", "bad": "print(1)\n"}
    assert dirs["ok"] != dirs["bad"]
    assert all(not d.exists() for d in dirs.values())
    assert not any("ember" in d.name for d in dirs.values())


def test_a_suite_without_a_fixtures_directory_copies_nothing(smoke, tmp_path, monkeypatch):
    dirs = _outcomes(
        smoke, monkeypatch, {"a": {"result": lambda c: CaseResult(case=c, passed=True)}}
    )
    seen = []
    real = smoke._run_case

    async def peek(case, agent, judge, *, work_dir, **kw):
        seen.append(sorted(p.name for p in work_dir.iterdir()))
        return await real(case, agent, judge, work_dir=work_dir, **kw)

    monkeypatch.setattr(smoke, "_run_case", peek)
    asyncio.run(
        smoke._run_suite(
            _suite_file(tmp_path, ["a"]), lambda d: object(), None, None, tmp_path / "nope", 1
        )
    )
    assert seen == [[]]
    assert not dirs["a"].exists()


def test_a_case_whose_agent_cannot_be_built_is_an_error_line_and_not_a_result(
    smoke, tmp_path, monkeypatch, capsys
):
    _outcomes(smoke, monkeypatch, {"good": {"result": lambda c: CaseResult(case=c, passed=True)}})

    calls: list[Path] = []

    def factory(work_dir):
        calls.append(work_dir)
        if len(calls) == 1:
            raise RuntimeError("no model")
        return object()

    _, results = asyncio.run(
        smoke._run_suite(_suite_file(tmp_path, ["bad_one", "good"]), factory, None, None, None, 1)
    )
    out = capsys.readouterr().out
    assert "[ 1] ERROR bad_one: no model" in out
    assert "[ 2] PASS" in out
    assert [c.name for c, _ in results] == ["good"]


def test_each_result_is_dumped_and_a_failed_dump_does_not_stop_the_suite(
    smoke, tmp_path, monkeypatch, capsys
):
    _outcomes(
        smoke,
        monkeypatch,
        {n: {"result": lambda c: CaseResult(case=c, passed=True)} for n in ("a", "b")},
    )
    real_dump = smoke._dump_case_result

    def flaky_dump(results_dir, agent, result):
        if result.case.name == "a":
            raise OSError("disk full")
        real_dump(results_dir, agent, result)

    monkeypatch.setattr(smoke, "_dump_case_result", flaky_dump)
    results_dir = tmp_path / "out"
    _, results = asyncio.run(
        smoke._run_suite(
            _suite_file(tmp_path, ["a", "b"]),
            lambda d: object(),
            None,
            None,
            None,
            1,
            results_dir=results_dir,
        )
    )
    assert "! failed to dump a: disk full" in capsys.readouterr().out
    assert [c.name for c, _ in results] == ["a", "b"]
    assert sorted(p.name for p in (results_dir / "editor").iterdir()) == ["b.json"]


# ── What is written and printed after a suite ──────────────────────


def _result(name: str, passed: bool, **kw) -> tuple[EvalCase, CaseResult]:
    case = _case(name)
    return case, CaseResult(case=case, passed=passed, **kw)


def test_a_case_result_is_written_as_json_under_its_suite(smoke, tmp_path):
    case, result = _result(
        "uses_grep",
        True,
        response_text="done",
        tool_trace=[ToolTraceEntry(name="grep", args={"p": "x"}, result_preview="hit")],
    )
    smoke._dump_case_result(tmp_path / "run", "editor", result)
    data = json.loads((tmp_path / "run" / "editor" / "uses_grep.json").read_text())
    assert data["case"]["name"] == "uses_grep"
    assert data["passed"] is True and data["response_text"] == "done"
    assert data["tool_trace"][0]["name"] == "grep"


def test_the_summary_counts_cases_and_passes_per_category(smoke, tmp_path):
    suite = type("S", (), {"agent": "editor"})()
    results = [
        _result(
            "uses_grep",
            True,
            elapsed=1.234,
            tool_trace=[ToolTraceEntry(name="grep"), ToolTraceEntry(name="read")],
        ),
        _result("cat_it", False, error="model_error: x"),
        _result("respects_tabs", True),
        _result("whatever", False),
    ]
    smoke._dump_summary(tmp_path, suite, results)
    summary = json.loads((tmp_path / "editor_summary.json").read_text())
    assert summary["agent"] == "editor"
    assert (summary["total"], summary["passed"]) == (4, 2)
    assert summary["by_category"] == {
        "A. tool-selection": {"passed": 1, "total": 2},
        "D. style": {"passed": 1, "total": 1},
        "G. specific": {"passed": 0, "total": 1},
    }
    first, second = summary["cases"][0], summary["cases"][1]
    assert first["name"] == "uses_grep" and first["category"] == "A. tool-selection"
    assert first["elapsed"] == 1.23 and first["tools_called"] == ["grep", "read"]
    assert second["passed"] is False and second["error"] == "model_error: x"


def test_the_summary_prints_overall_and_per_category_rates_sorted(smoke, capsys):
    suite = type("S", (), {"agent": "main"})()
    smoke._summarize(
        suite,
        [
            _result("routing_a", True),
            _result("direct_a", False),
            _result("direct_b", True),
        ],
    )
    out = capsys.readouterr().out
    assert "Overall: 2/3 (67%)" in out
    assert out.index("A. Direct (no delegation)") < out.index("F. Routing")
    assert re.search(r"A\. Direct \(no delegation\)\s+1/2\s+\(50%\)", out)
    assert re.search(r"F\. Routing\s+1/1\s+\(100%\)", out)


def test_an_empty_run_summarises_to_zero_without_dividing_by_it(smoke, tmp_path, capsys):
    suite = type("S", (), {"agent": "main"})()
    smoke._summarize(suite, [])
    assert "Overall: 0/0 (0%)" in capsys.readouterr().out
    smoke._dump_summary(tmp_path, suite, [])
    summary = json.loads((tmp_path / "main_summary.json").read_text())
    assert (summary["total"], summary["passed"], summary["by_category"]) == (0, 0, {})


# ── The command line ───────────────────────────────────────────────


@pytest.fixture
def repo_cwd(tmp_path, monkeypatch):
    """A working directory shaped like the repository root (the script
    reads ``evals/`` and ``agents/`` relative to it), with no key set."""
    import shutil

    shutil.copytree(REPO / "evals", tmp_path / "evals", ignore=shutil.ignore_patterns("results"))
    shutil.copytree(REPO / "agents", tmp_path / "agents")
    monkeypatch.chdir(tmp_path)
    for var in ("IGNI_TEST_LLM_API_KEY", "IGNI_TEST_LLM_MODEL", "IGNI_TEST_LLM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


class _Calls:
    """What ``main`` handed to ``_run_suite``, one entry per suite."""

    def __init__(self):
        self.suites: list[dict] = []


@pytest.fixture
def run_main(smoke, repo_cwd, monkeypatch):
    calls = _Calls()

    async def fake_run_suite(yaml_path, factory, judge, n, fixtures_root, concurrency, **kw):
        calls.suites.append(
            dict(
                yaml=yaml_path,
                factory=factory,
                n=n,
                fixtures_root=fixtures_root,
                concurrency=concurrency,
                **kw,
            )
        )
        return None

    monkeypatch.setattr(smoke, "_run_suite", fake_run_suite)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["run_evals_smoke.py", *argv])
        asyncio.run(smoke.main())
        return calls

    return run


def test_without_a_key_it_says_so_and_exits_one(run_main, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run_main("--suite", "main")
    assert exit_info.value.code == 1
    assert "Set IGNI_TEST_LLM_API_KEY (in .env or env)." in capsys.readouterr().out


def test_the_key_is_read_from_a_dot_env_without_overriding_the_environment(
    run_main, repo_cwd, monkeypatch
):
    (repo_cwd / ".env").write_text(
        "# a comment\n"
        "\n"
        "IGNI_TEST_LLM_API_KEY = k=with=equals\n"
        "  # IGNI_TEST_LLM_MODEL=commented-out\n"
        "IGNI_TEST_LLM_BASE_URL=http://from-file.example/v1\n"
    )
    monkeypatch.setenv("IGNI_TEST_LLM_BASE_URL", "http://from-env.example/v1")
    run_main("--suite", "nosuch")
    import os

    assert os.environ["IGNI_TEST_LLM_API_KEY"] == "k=with=equals"
    assert os.environ["IGNI_TEST_LLM_BASE_URL"] == "http://from-env.example/v1"
    assert "IGNI_TEST_LLM_MODEL" not in os.environ


def test_a_dot_env_without_the_key_still_exits_one(run_main, repo_cwd, capsys, monkeypatch):
    (repo_cwd / ".env").write_text("OTHER=1\n")
    monkeypatch.delenv("OTHER", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        run_main()
    assert exit_info.value.code == 1


def test_cases_zero_or_negative_means_all_and_the_default_is_five(run_main, monkeypatch):
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    assert run_main("--suite", "main").suites[-1]["n"] == 5
    assert run_main("--suite", "main", "--cases", "0").suites[-1]["n"] is None
    assert run_main("--suite", "main", "--cases", "-3").suites[-1]["n"] is None
    assert run_main("--suite", "main", "--cases", "7").suites[-1]["n"] == 7


def test_the_concurrency_is_at_least_one(run_main, monkeypatch):
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    assert run_main("--suite", "main").suites[-1]["concurrency"] == 5
    assert run_main("--suite", "main", "--concurrency", "0").suites[-1]["concurrency"] == 1
    assert run_main("--suite", "main", "--concurrency", "-4").suites[-1]["concurrency"] == 1
    assert run_main("--suite", "main", "--concurrency", "3").suites[-1]["concurrency"] == 3


def test_the_timeouts_and_retries_are_passed_on_and_results_go_in_a_stamped_dir(
    run_main, repo_cwd, monkeypatch
):
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    suite = run_main("--suite", "main", "--case-timeout", "9", "--case-retries", "4").suites[-1]
    assert (suite["case_timeout"], suite["case_retries"]) == (9, 4)
    assert re.fullmatch(r"\d{8}-\d{6}", suite["results_dir"].name)
    assert suite["results_dir"].parent == Path("evals/results")
    assert suite["results_dir"].is_dir()
    default = run_main("--suite", "main").suites[-1]
    assert (default["case_timeout"], default["case_retries"]) == (60, 2)


def test_all_runs_every_yaml_under_evals_and_main_means_the_main_agent_file(
    run_main, repo_cwd, monkeypatch
):
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    # every suite whose agent the pool lacks is skipped, so look at what
    # reached a runner: the editor and the main agent need no pool lookup.
    (repo_cwd / "evals" / "extra.yaml").write_text("agent: editor\ncases: []\n")
    names = sorted(p.name for p in (repo_cwd / "evals").glob("*.yaml"))
    suites = run_main("--suite", "all").suites
    ran = [s["yaml"].name for s in suites]
    assert "main_agent.yaml" in ran and "editor.yaml" in ran and "extra.yaml" in ran
    assert set(ran) <= set(names)
    assert ran == [n for n in names if n in ran]  # sorted by file stem


def test_a_comma_list_runs_those_suites_and_blanks_run_nothing(run_main, monkeypatch):
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    ran = [s["yaml"].name for s in run_main("--suite", " editor ,, main ").suites]
    assert ran == ["editor.yaml", "main_agent.yaml"]


def test_an_empty_suite_list_runs_nothing(run_main, monkeypatch):
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    assert run_main("--suite", "").suites == []
    assert run_main("--suite", ",,").suites == []


def test_a_missing_suite_is_skipped_with_a_message(run_main, monkeypatch, capsys):
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    assert run_main("--suite", "nosuch").suites == []
    assert "! suite 'nosuch' not found at evals/nosuch.yaml — skipping" in capsys.readouterr().out


def test_a_suite_file_that_cannot_be_read_is_skipped_with_a_message(
    run_main, repo_cwd, monkeypatch, capsys
):
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    (repo_cwd / "evals" / "broken.yaml").write_text("agent: [unclosed\n")
    assert run_main("--suite", "broken").suites == []
    assert "! could not read agent from evals/broken.yaml" in capsys.readouterr().out


def test_a_suite_for_an_agent_the_pool_lacks_is_skipped_with_a_message(
    run_main, repo_cwd, monkeypatch, capsys
):
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    monkeypatch.setenv("IGNI_TEST_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    (repo_cwd / "evals" / "ghost.yaml").write_text("agent: no-such-specialist\ncases: []\n")
    assert run_main("--suite", "ghost").suites == []
    assert "! agent 'no-such-specialist' not in pool" in capsys.readouterr().out


def test_the_editor_suite_gets_the_editor_agent_and_the_mcp_suite_the_stub_tools(
    run_main, repo_cwd, monkeypatch
):
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    editor = run_main("--suite", "editor").suites[-1]
    agent = editor["factory"](repo_cwd)
    assert agent.name == "editor"
    assert len(agent.tools) == 3  # Write, Edit and Bash, nothing else
    assert not agent.instructions.startswith("---")  # front matter stripped
    mcp_yaml = next(
        (p for p in (repo_cwd / "evals").glob("*.yaml") if "agent: mcp-test" in p.read_text()),
        None,
    )
    if mcp_yaml is not None:
        mcp = run_main("--suite", mcp_yaml.stem).suites[-1]
        assert mcp["factory"](repo_cwd).name == "mcp-test"


def test_each_case_session_gets_a_permissive_project_and_the_live_model(
    run_main, repo_cwd, monkeypatch
):
    """The per-case session is the one a real run would build, pointed at
    the test endpoint, with the permissions the settings file grants."""
    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    monkeypatch.setenv("IGNI_TEST_LLM_MODEL", "model-x")
    monkeypatch.setenv("IGNI_TEST_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    suite = run_main("--suite", "explorer", "--spawn-timeout", "77").suites[-1]
    work = repo_cwd / "case"
    work.mkdir()
    main_suite = run_main("--suite", "main").suites[-1]
    main_work = repo_cwd / "main-case"
    main_work.mkdir()

    async def build():  # the factories run inside the suite's event loop
        return suite["factory"](work), main_suite["factory"](main_work)

    agent, main_team = asyncio.run(build())
    assert agent.name == "explorer"
    settings = json.loads((work / ".igni" / "settings.local.json").read_text())
    assert "Bash" in settings["permissions"]["allow"]
    # every name it allows is a tool the registry still has
    from igni_core.core.tools.tool_spec import ToolSpecCatalog

    real = set(ToolSpecCatalog.default().by_name)
    assert set(settings["permissions"]["allow"]) <= real, (
        set(settings["permissions"]["allow"]) - real
    )
    assert settings["permissions"]["ask"] == []
    assert agent.model.id == "model-x"
    assert str(agent.model.base_url) == "http://127.0.0.1:9/v1"
    assert main_team is not None


def test_the_probe_dir_of_a_specialist_suite_is_removed(run_main, repo_cwd, monkeypatch):
    import tempfile

    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    monkeypatch.setenv("TMPDIR", str(repo_cwd))
    monkeypatch.setattr(tempfile, "tempdir", None)
    run_main("--suite", "explorer")
    assert not list(repo_cwd.glob("igni-eval-probe-*"))


@pytest.mark.workspace_trust
def test_the_permissions_a_case_dir_grants_are_honoured_because_the_runner_trusts_it(
    run_main, repo_cwd, monkeypatch
):
    """The case dir is the runner's own scratch space. A project's
    ``permissions`` count only in a trusted folder, so without the trust
    the permissive file would be ignored and every Bash call would wait
    for a confirmation nobody can give."""
    from igni_core.core.config.settings_loader import SettingsLoader
    from igni_core.core.workspace_trust import process_trust

    monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
    suite = run_main("--suite", "debugger").suites[-1]
    work = repo_cwd / "case"
    work.mkdir()

    async def build():
        return suite["factory"](work)

    asyncio.run(build())
    try:
        permissions = SettingsLoader.load(project_dir=work).permissions
        assert "Bash" in permissions.allow and "Write" in permissions.allow
    finally:
        process_trust().revoke(work)
