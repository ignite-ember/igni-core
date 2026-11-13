"""SuiteRunner and CaseRunner through their public surface, with a real workspace.

The agent and the agent pool are the dependencies, so they are stubs; the
runners, the workspace and the assertion drivers are real.
"""

from __future__ import annotations

import asyncio
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.core.config.schemas.llm_behavior import EvalsConfig
from igni_core.core.evals.assertions import FileExists
from igni_core.core.evals.loader import EvalCase, EvalSuite
from igni_core.core.evals.runner import CaseRunner, SuiteRunner
from igni_core.core.evals.schemas import FixtureSpec
from igni_core.core.paths import CONFIG_DIR


def response(text="ok", messages=None):
    r = MagicMock()
    r.content = text
    r.tools = None
    r.messages = messages or []
    return r


def agent_with(*responses):
    agent = MagicMock()
    agent.arun = AsyncMock(side_effect=list(responses) or [response()])
    return agent


class Pool:
    def __init__(self, agent=None, raises=None, reason="no such agent"):
        self.agent, self.raises, self.reason = agent, raises, reason

    def get(self, name):
        if self.raises:
            raise self.raises
        return self.agent

    def missing(self, name):
        return self.reason


def settings(evals=None):
    return SimpleNamespace(evals=evals)


def runner_for(suite, pool, tmp_path, evals=None):
    return SuiteRunner(suite, pool, settings(evals), tmp_path)


class TestCaseRunner:
    async def test_prior_messages_go_first_on_the_same_session(self):
        agent = agent_with(response("1"), response("2"), response("final"))
        case = EvalCase(name="c", input="last", prior_messages=["a", "b"])

        result = await CaseRunner(case, agent, session_id="sess").run()

        calls = agent.arun.call_args_list
        assert [c.args[0] for c in calls] == ["a", "b", "last"]
        assert all(c.kwargs == {"stream": False, "session_id": "sess"} for c in calls)
        assert result.response_text == "final"
        assert result.passed is True

    async def test_no_session_id_is_not_sent(self):
        agent = agent_with(response())
        await CaseRunner(EvalCase(name="c", input="i"), agent).run()
        assert agent.arun.call_args.kwargs == {"stream": False}

    async def test_an_arun_failure_is_reported_not_raised(self):
        agent = MagicMock()
        agent.arun = AsyncMock(side_effect=RuntimeError("model down"))
        result = await CaseRunner(EvalCase(name="c", input="i"), agent).run()
        assert result.passed is False
        assert result.error == "agent.arun failed: model down"

    async def test_a_failing_prior_message_stops_before_the_input(self):
        agent = MagicMock()
        agent.arun = AsyncMock(side_effect=RuntimeError("x"))
        case = EvalCase(name="c", input="i", prior_messages=["p"])
        result = await CaseRunner(case, agent).run()
        assert agent.arun.await_count == 1
        assert result.error == "agent.arun failed: x"

    async def test_an_assertion_phase_error_is_the_error_and_fails_the_case(self):
        class Boom:
            async def run(self, ctx, result):
                raise ValueError("driver exploded")

        result = await CaseRunner(
            EvalCase(name="c", input="i"), agent_with(response()), assertion_runner=Boom()
        ).run()
        assert result.error == "driver exploded"
        assert result.passed is False
        assert result.response_text == "ok"

    async def test_the_timeout_covers_the_assertion_phase(self):
        class Slow:
            async def run(self, ctx, result):
                await asyncio.sleep(30)

        result = await CaseRunner(
            EvalCase(name="c", input="i", case_timeout=0.05),
            agent_with(response()),
            assertion_runner=Slow(),
        ).run()
        assert result.passed is False
        assert result.error.startswith("case_timeout: exceeded")

    async def test_a_fractional_budget_is_named_exactly(self):
        async def never(*a, **k):
            await asyncio.sleep(30)

        agent = MagicMock()
        agent.arun = AsyncMock(side_effect=never)
        result = await CaseRunner(EvalCase(name="c", input="i", case_timeout=0.05), agent).run()
        assert result.error == "case_timeout: exceeded 0.05s"

    async def test_a_whole_second_budget_has_no_decimals(self):
        async def never(*a, **k):
            await asyncio.sleep(30)

        agent = MagicMock()
        agent.arun = AsyncMock(side_effect=never)
        runner = CaseRunner(EvalCase(name="c", input="i"), agent, default_timeout=1)
        # Shrink the wait without changing the formatting under test.
        result = await asyncio.wait_for(runner.run(), 5)
        assert result.error == "case_timeout: exceeded 1s"

    @pytest.mark.parametrize(
        ("case_timeout", "default", "expected"),
        [
            (5, 9, 5.0),
            (None, 9, 9.0),
            (0, 9, 9.0),
            (-1, 9, 9.0),
            (None, None, None),
            (None, 0, None),
            (None, -3, None),
            (0, 0, None),
        ],
    )
    def test_timeout_resolution(self, case_timeout, default, expected):
        case = EvalCase(name="c", input="i", case_timeout=case_timeout)
        assert CaseRunner(case, MagicMock(), default_timeout=default).timeout == expected

    async def test_only_the_last_turn_is_judged(self):
        history = SimpleNamespace(
            role="assistant",
            from_history=True,
            tool_calls=[{"function": {"name": "spawn_agent", "arguments": "{}"}}],
        )
        r = response("done", messages=[history])
        case = EvalCase(name="c", input="i", unexpected_tool_calls=["Write"])
        result = await CaseRunner(case, agent_with(r)).run()
        assert result.passed is True
        assert result.tool_trace == []


class TestSuiteRunnerLookup:
    async def test_a_missing_agent_gives_one_error_row_per_case(self, tmp_path):
        suite = EvalSuite(
            agent="ghost",
            cases=[EvalCase(name="a", input="x"), EvalCase(name="b", input="y")],
        )
        result = await runner_for(
            suite, Pool(None, reason="Agent 'ghost' not found."), tmp_path
        ).run()
        assert [r.case.name for r in result.case_results] == ["a", "b"]
        assert [r.error for r in result.case_results] == [
            "agent 'ghost' not found: Agent 'ghost' not found."
        ] * 2
        assert not any(r.passed for r in result.case_results)

    async def test_a_pool_that_raises_value_error_gives_its_message(self, tmp_path):
        suite = EvalSuite(agent="a", cases=[EvalCase(name="a", input="x")])
        result = await runner_for(suite, Pool(raises=ValueError("cannot build")), tmp_path).run()
        assert result.case_results[0].error == "agent 'a' not found: cannot build"

    async def test_a_suite_with_no_cases_runs_to_an_empty_result(self, tmp_path):
        suite = EvalSuite(agent="a")
        result = await runner_for(suite, Pool(agent_with()), tmp_path).run()
        assert result.case_results == []


class TestSuiteRunnerRun:
    async def test_cases_run_in_order_each_on_its_own_session(self, tmp_path, capsys):
        agent = agent_with(response(), response())
        suite = EvalSuite(
            agent="coder",
            cases=[EvalCase(name="first", input="1"), EvalCase(name="we ird/na:me", input="2")],
        )
        result = await runner_for(suite, Pool(agent), tmp_path).run()

        assert [r.case.name for r in result.case_results] == ["first", "we ird/na:me"]
        assert all(r.passed for r in result.case_results)
        sessions = [c.kwargs["session_id"] for c in agent.arun.call_args_list]
        assert re.fullmatch(r"eval-coder-first-[0-9a-f]{8}", sessions[0])
        assert re.fullmatch(r"eval-coder-we ird/na:me-[0-9a-f]{8}", sessions[1])
        tag = sessions[0].rsplit("-", 1)[1]
        assert sessions[1].endswith(tag)

        out = capsys.readouterr().out
        assert "  [case 1/2] first starting..." in out
        assert re.search(r"\[case 2/2\] we ird/na:me → PASS in \d+\.\ds", out)

    async def test_a_failing_case_prints_fail(self, tmp_path, capsys):
        agent = MagicMock()
        agent.arun = AsyncMock(side_effect=RuntimeError("x"))
        suite = EvalSuite(agent="a", cases=[EvalCase(name="c", input="i")])
        result = await runner_for(suite, Pool(agent), tmp_path).run()
        assert result.case_results[0].passed is False
        assert "[case 1/1] c → FAIL in" in capsys.readouterr().out

    async def test_the_suite_flag_and_config_timeout_reach_each_case(self, tmp_path):
        async def never(*a, **k):
            await asyncio.sleep(30)

        agent = MagicMock()
        agent.arun = AsyncMock(side_effect=never)
        suite = EvalSuite(agent="a", cases=[EvalCase(name="c", input="i")])
        result = await runner_for(
            suite, Pool(agent), tmp_path, EvalsConfig(timeout_per_case=1)
        ).run()
        assert result.case_results[0].error == "case_timeout: exceeded 1s"

    async def test_the_suite_flag_decides_how_the_reliability_check_reads_the_list(
        self, tmp_path, monkeypatch
    ):
        import igni_core.core.evals.assertion_runner as driver_mod

        built = []

        class Spy:
            def __init__(self, **kwargs):
                built.append(kwargs)

            async def arun(self, **_kwargs):
                return SimpleNamespace(eval_status="PASSED")

        monkeypatch.setattr(driver_mod, "ReliabilityEval", Spy)
        for allow in (False, True):
            suite = EvalSuite(
                agent="a",
                allow_additional_tool_calls=allow,
                cases=[EvalCase(name="c", input="i", expected_tool_calls=["spawn_agent"])],
            )
            await runner_for(suite, Pool(agent_with(response())), tmp_path).run()
        assert [(k["allow_additional_tool_calls"], k["expected_tool_calls"]) for k in built] == [
            (False, ["spawn_agent", "update_user_memory"]),
            (True, ["spawn_agent"]),
        ]

    async def test_fixtures_come_from_evals_fixtures_when_that_dir_exists(self, tmp_path):
        (tmp_path / "evals" / "fixtures").mkdir(parents=True)
        (tmp_path / "evals" / "fixtures" / "a.txt").write_text("builtin")
        (tmp_path / CONFIG_DIR / "evals").mkdir(parents=True)
        (tmp_path / CONFIG_DIR / "evals" / "a.txt").write_text("user")
        assert await self.fixture_text(tmp_path) == "builtin"

    async def test_fixtures_fall_back_to_the_user_evals_dir(self, tmp_path):
        (tmp_path / CONFIG_DIR / "evals").mkdir(parents=True)
        (tmp_path / CONFIG_DIR / "evals" / "a.txt").write_text("user")
        assert await self.fixture_text(tmp_path) == "user"

    async def fixture_text(self, project):
        seen = {}

        class Capture:
            async def run(self, ctx, result):
                seen["text"] = (ctx.work_dir / "out.txt").read_text()
                seen["dir"] = ctx.work_dir

        agent = agent_with(response())
        suite = EvalSuite(
            agent="a",
            fixtures=[FixtureSpec(source="a.txt", target="out.txt")],
            cases=[EvalCase(name="c", input="i")],
        )
        runner = runner_for(suite, Pool(agent), project)
        # Swap the assertion step to observe the work dir the case saw.
        import igni_core.core.evals.runner as mod

        original = mod.CaseAssertionRunner
        mod.CaseAssertionRunner = Capture
        try:
            await runner.run()
        finally:
            mod.CaseAssertionRunner = original
        assert not seen["dir"].exists()
        return seen["text"]

    async def test_a_file_assertion_sees_the_fixture_in_the_work_dir(self, tmp_path):
        (tmp_path / CONFIG_DIR / "evals").mkdir(parents=True)
        (tmp_path / CONFIG_DIR / "evals" / "a.txt").write_text("x")
        suite = EvalSuite(
            agent="a",
            fixtures=[FixtureSpec(source="a.txt", target="out.txt")],
            cases=[
                EvalCase(
                    name="c",
                    input="i",
                    file_assertions=[FileExists(type="file_exists", path="out.txt")],
                )
            ],
        )
        result = await runner_for(suite, Pool(agent_with(response())), tmp_path).run()
        assert result.case_results[0].passed is True

    async def test_a_failing_setup_module_gives_error_rows_and_leaks_nothing(self, tmp_path):
        (tmp_path / "hook_fail_mod.py").write_text(
            "def setup(w, p):\n    raise OSError('no git')\n"
        )
        suite = EvalSuite(
            agent="a",
            setup_module="hook_fail_mod",
            cases=[EvalCase(name="a", input="x"), EvalCase(name="b", input="y")],
        )
        agent = agent_with(response())
        before = list(sys.path)
        try:
            result = await runner_for(suite, Pool(agent), tmp_path).run()
        finally:
            sys.modules.pop("hook_fail_mod", None)
        assert [r.error for r in result.case_results] == ["setup_module failed: no git"] * 2
        agent.arun.assert_not_awaited()
        assert sys.path == before

    async def test_the_workspace_is_torn_down_even_when_a_case_blows_up(self, tmp_path):
        seen = {}

        class Boom:
            async def run(self, ctx, result):
                seen["dir"] = ctx.work_dir
                raise ValueError("x")

        import igni_core.core.evals.runner as mod

        original = mod.CaseAssertionRunner
        mod.CaseAssertionRunner = Boom
        try:
            suite = EvalSuite(agent="a", cases=[EvalCase(name="c", input="i")])
            result = await runner_for(suite, Pool(agent_with(response())), tmp_path).run()
        finally:
            mod.CaseAssertionRunner = original
        assert result.case_results[0].error == "x"
        assert not seen["dir"].exists()


class TestJudgeModel:
    def test_the_configured_judge_is_asked_of_the_registry(self, tmp_path, monkeypatch):
        import igni_core.core.evals.runner as mod

        asked = []

        class Registry:
            def __init__(self, s):
                pass

            def get_model(self, name):
                asked.append(name)
                return "the-model"

        monkeypatch.setattr(mod, "ModelRegistry", Registry)
        runner = runner_for(EvalSuite(agent="a"), Pool(), tmp_path, EvalsConfig(judge_model="big"))
        assert runner._lookup_judge_model() == "the-model"
        assert asked == ["big"]

    def test_a_registry_that_cannot_build_it_gives_none(self, tmp_path, monkeypatch):
        import igni_core.core.evals.runner as mod

        class Registry:
            def __init__(self, s):
                raise RuntimeError("no key")

        monkeypatch.setattr(mod, "ModelRegistry", Registry)
        runner = runner_for(EvalSuite(agent="a"), Pool(), tmp_path)
        assert runner._lookup_judge_model() is None


class TestRunAll:
    def write_suite(self, root, agent):
        d = root / CONFIG_DIR / "evals"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{agent}.yaml").write_text(f"agent: {agent}\ncases:\n  - name: c\n    input: hi\n")

    async def test_no_suites_gives_an_empty_list(self, tmp_path):
        assert await SuiteRunner.run_all(Pool(), settings(), tmp_path) == []

    async def test_every_suite_runs_in_order_without_a_filter(self, tmp_path):
        self.write_suite(tmp_path, "alpha")
        self.write_suite(tmp_path, "beta")
        results = await SuiteRunner.run_all(
            Pool(agent_with(response(), response())), settings(), tmp_path
        )
        assert [r.suite.agent for r in results] == ["alpha", "beta"]

    async def test_the_filter_keeps_only_the_named_agent(self, tmp_path):
        self.write_suite(tmp_path, "alpha")
        self.write_suite(tmp_path, "beta")
        results = await SuiteRunner.run_all(
            Pool(agent_with(response())), settings(), tmp_path, agent_filter="beta"
        )
        assert [r.suite.agent for r in results] == ["beta"]

    async def test_a_filter_matching_nothing_gives_an_empty_list(self, tmp_path):
        self.write_suite(tmp_path, "alpha")
        assert await SuiteRunner.run_all(Pool(), settings(), tmp_path, agent_filter="alph") == []
