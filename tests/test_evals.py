"""Tests for the evals framework — loader, assertions, runner, reporter."""

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from igni_core.backend.command_handler import CommandHandler
from igni_core.core.evals.agno_adapter import AgnoResponseAdapter
from igni_core.core.evals.assertion_runner import ToolArgDriver, UnexpectedToolsDriver
from igni_core.core.evals.assertions import (
    FILE_ASSERTION,
    FileContains,
    FileExists,
    FileNotContains,
    FileNotExists,
)
from igni_core.core.evals.loader import EvalCase, EvalSuite, load_eval_file
from igni_core.core.evals.reporter import format_results
from igni_core.core.evals.runner import (
    CaseResult,
    CaseRunner,
    SuiteResult,
    SuiteRunner,
    ToolTraceEntry,
)
from igni_core.core.evals.schemas import ToolArgAssertion
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.session.commands import InteractiveCommandDispatcher


async def dispatch(session, command):
    """Test shim so existing tests keep their call shape."""
    return await InteractiveCommandDispatcher(session).dispatch(command)


# ── Loader tests ──────────────────────────────────────────────


class TestLoadEvalFile:
    def test_loads_valid_yaml(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            agent: editor
            description: Editor evals
            cases:
              - name: test_edit
                input: "Edit the file"
                expected_tool_calls: [Edit]
              - name: test_create
                input: "Create a file"
                expected_output: "File created"
                accuracy_threshold: 8.0
        """)
        f = tmp_path / "editor.yaml"
        f.write_text(yaml_content)

        suite = load_eval_file(f)
        assert suite is not None
        assert suite.agent == "editor"
        assert suite.description == "Editor evals"
        assert len(suite.cases) == 2
        assert suite.cases[0].name == "test_edit"
        assert suite.cases[0].expected_tool_calls == ["Edit"]
        assert suite.cases[1].accuracy_threshold == 8.0

    def test_skips_invalid_yaml(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("just_a_string")
        assert load_eval_file(f) is None

    def test_skips_missing_agent(self, tmp_path):
        f = tmp_path / "no_agent.yaml"
        f.write_text("cases:\n  - name: x\n    input: y\n")
        assert load_eval_file(f) is None

    def test_skips_missing_cases(self, tmp_path):
        f = tmp_path / "no_cases.yaml"
        f.write_text("agent: editor\n")
        assert load_eval_file(f) is None

    def test_skips_cases_without_required_fields(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            agent: editor
            cases:
              - name: valid
                input: "do something"
              - name: no_input
              - input: "no_name"
        """)
        f = tmp_path / "partial.yaml"
        f.write_text(yaml_content)

        suite = load_eval_file(f)
        assert suite is not None
        assert len(suite.cases) == 1
        assert suite.cases[0].name == "valid"

    def test_defaults(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            agent: explorer
            cases:
              - name: basic
                input: "search for something"
        """)
        f = tmp_path / "explorer.yaml"
        f.write_text(yaml_content)

        suite = load_eval_file(f)
        case = suite.cases[0]
        # Defaults now come from EvalsConfig rather than literals in the
        # loader — see tests/test_evals_config_and_timeout.py. The
        # documented default for num_iterations has always been 3; the
        # loader just wasn't reading it.
        assert case.accuracy_threshold == 7.0
        assert case.num_iterations == 3
        assert case.expected_tool_calls is None
        assert case.unexpected_tool_calls is None
        assert case.file_assertions is None


class TestLoadEvalSuites:
    def test_discovers_yaml_files(self, tmp_path):
        evals_dir = tmp_path / CONFIG_DIR / "evals"
        evals_dir.mkdir(parents=True)
        (evals_dir / "a.yaml").write_text("agent: a\ncases:\n  - name: t\n    input: x\n")
        (evals_dir / "b.yaml").write_text("agent: b\ncases:\n  - name: t\n    input: x\n")
        (evals_dir / "c.txt").write_text("not yaml")

        suites = EvalSuite.load_all(tmp_path)
        assert len(suites) == 2
        agents = {s.agent for s in suites}
        assert agents == {"a", "b"}

    def test_returns_empty_when_no_dir(self, tmp_path):
        assert EvalSuite.load_all(tmp_path) == []


# ── Assertions tests ──────────────────────────────────────────


def check_unexpected(response, unexpected):
    called = AgnoResponseAdapter(response).called_tool_names()
    return UnexpectedToolsDriver.check(called, unexpected)


class TestCheckUnexpectedToolCalls:
    def test_no_forbidden_tools(self):
        response = MagicMock()
        tool = MagicMock()
        tool.tool_name = "Edit"
        response.tools = [tool]

        assert check_unexpected(response, ["Write"]).ok is True

    def test_forbidden_tool_detected(self):
        response = MagicMock()
        tool = MagicMock()
        tool.tool_name = "Write"
        response.tools = [tool]

        check = check_unexpected(response, ["Write"])
        assert check.ok is False
        assert "Write" in check.detail

    def test_fallback_to_messages(self):
        response = MagicMock()
        response.tools = None
        msg = MagicMock()
        msg.tool_calls = [{"function": {"name": "Bash"}}]
        response.messages = [msg]

        check = check_unexpected(response, ["Bash"])
        assert check.ok is False
        assert "Bash" in check.detail

    def test_no_tools_at_all(self):
        response = MagicMock()
        response.tools = None
        response.messages = []

        assert check_unexpected(response, ["Write"]).ok is True


class TestFileAssertions:
    def test_file_exists_pass(self, tmp_path):
        f = tmp_path / "exists.txt"
        f.write_text("hello")
        assert FileExists(path=str(f)).check().passed is True

    def test_file_exists_fail(self, tmp_path):
        assert FileExists(path=str(tmp_path / "nope.txt")).check().passed is False

    def test_file_not_exists_pass(self, tmp_path):
        assert FileNotExists(path=str(tmp_path / "nope.txt")).check().passed is True

    def test_file_not_exists_fail(self, tmp_path):
        f = tmp_path / "exists.txt"
        f.write_text("hello")
        assert FileNotExists(path=str(f)).check().passed is False

    def test_file_contains_pass(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("def process_data(x):\n    return x\n")
        assert FileContains(path=str(f), pattern=r"def process_data").check().passed is True

    def test_file_contains_fail(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("def processData(x):\n    return x\n")
        assert FileContains(path=str(f), pattern=r"def process_data").check().passed is False

    def test_file_not_contains_pass(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("def process_data(x):\n    return x\n")
        assert FileNotContains(path=str(f), pattern=r"def processData").check().passed is True

    def test_file_not_contains_fail(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("def processData(x):\n    return x\n")
        assert FileNotContains(path=str(f), pattern=r"def processData").check().passed is False

    def test_file_contains_missing_file(self, tmp_path):
        check = FileContains(path=str(tmp_path / "missing.py"), pattern=r"anything").check()
        assert check.passed is False

    def test_relative_path_resolves_against_work_dir(self, tmp_path):
        (tmp_path / "out.txt").write_text("x")
        check = FileExists(path="out.txt").check(tmp_path)
        assert check.passed is True
        assert check.type == "file_exists"

    def test_yaml_entry_validates_to_its_kind(self):
        spec = FILE_ASSERTION.validate_python(
            {"type": "file_contains", "path": "a.py", "pattern": "x"}
        )
        assert isinstance(spec, FileContains)

    def test_unknown_type_fails_validation(self):
        with pytest.raises(ValidationError):
            FILE_ASSERTION.validate_python({"type": "bogus"})

    def test_file_unchanged_is_not_accepted(self):
        # It was never implemented and used to pass silently.
        with pytest.raises(ValidationError):
            FILE_ASSERTION.validate_python({"type": "file_unchanged", "path": "a"})


# ── Runner tests ──────────────────────────────────────────────


class TestCaseResult:
    def test_default_state(self):
        case = EvalCase(name="test", input="test input")
        result = CaseResult(case=case)
        assert result.passed is False
        assert result.error is None
        assert result.elapsed == 0.0


class TestSuiteResult:
    def test_aggregation(self):
        case1 = EvalCase(name="a", input="x")
        case2 = EvalCase(name="b", input="y")
        suite = EvalSuite(agent="test")

        sr = SuiteResult(
            suite=suite,
            case_results=[
                CaseResult(case=case1, passed=True, elapsed=1.0),
                CaseResult(case=case2, passed=False, elapsed=2.0),
            ],
        )

        assert sr.passed == 1
        assert sr.failed == 1
        assert sr.total == 2
        assert sr.elapsed == 3.0


class TestRunEvalCase:
    @pytest.mark.asyncio
    async def test_passing_case(self):
        case = EvalCase(name="simple", input="do something")

        agent = MagicMock()
        response = MagicMock()
        response.content = "Done"
        agent.arun = AsyncMock(return_value=response)

        result = await CaseRunner(case, agent, judge_model=None).run()
        assert result.passed is True
        assert result.error is None
        assert result.elapsed > 0

    @pytest.mark.asyncio
    async def test_agent_error(self):
        case = EvalCase(name="error", input="fail")

        agent = MagicMock()
        agent.arun = AsyncMock(side_effect=RuntimeError("boom"))

        result = await CaseRunner(case, agent, judge_model=None).run()
        assert result.passed is False
        assert "boom" in result.error

    @pytest.mark.asyncio
    async def test_unexpected_tool_calls_fail(self):
        # The runner expands display names ("Write") to actual Agno
        # function names ("save_file", "create_file"). The test mocks a
        # tool emitting "save_file" so the blocklist hit is genuine.
        case = EvalCase(
            name="unexpected",
            input="test",
            unexpected_tool_calls=["Write"],
        )

        agent = MagicMock()
        response = MagicMock()
        response.content = "Done"
        tool = MagicMock()
        tool.tool_name = "save_file"
        response.tools = [tool]
        agent.arun = AsyncMock(return_value=response)

        result = await CaseRunner(case, agent, judge_model=None).run()
        assert result.passed is False
        assert result.unexpected_passed is False

    @pytest.mark.asyncio
    async def test_file_assertions(self, tmp_path):
        test_file = tmp_path / "output.txt"
        test_file.write_text("hello world")

        case = EvalCase(
            name="file_check",
            input="test",
            file_assertions=[
                {"type": "file_exists", "path": str(test_file)},
                {"type": "file_contains", "path": str(test_file), "pattern": "hello"},
            ],
        )

        agent = MagicMock()
        response = MagicMock()
        response.content = "Done"
        response.tools = None
        response.messages = []
        agent.arun = AsyncMock(return_value=response)

        result = await CaseRunner(case, agent, judge_model=None).run()
        assert result.passed is True
        assert len(result.file_results) == 2
        assert all(r.passed for r in result.file_results)


# ── Reporter tests ─────────────────────────────────────────────


class TestFormatResults:
    def test_all_pass(self):
        case = EvalCase(name="test_case", input="x")
        suite = EvalSuite(agent="editor")
        sr = SuiteResult(
            suite=suite,
            case_results=[
                CaseResult(case=case, passed=True, elapsed=1.5),
            ],
        )

        report = format_results([sr])
        assert "Eval Results" in report
        assert "editor" in report
        assert "1/1 passed" in report
        assert "100%" in report

    def test_mixed_results(self):
        case1 = EvalCase(name="pass_case", input="x")
        case2 = EvalCase(name="fail_case", input="y")
        suite = EvalSuite(agent="explorer")
        sr = SuiteResult(
            suite=suite,
            case_results=[
                CaseResult(case=case1, passed=True, elapsed=1.0),
                CaseResult(case=case2, passed=False, elapsed=2.0, error="something broke"),
            ],
        )

        report = format_results([sr])
        assert "1/2 passed" in report
        assert "50%" in report
        assert "Failed:" in report
        assert "explorer.fail_case" in report

    def test_empty_results(self):
        report = format_results([])
        assert "0/0" in report or "0%" in report


# ── Commands integration test ──────────────────────────────────


class TestEvalsCommand:
    @pytest.mark.asyncio
    async def test_evals_dispatch(self):
        session = MagicMock()
        session.pool = MagicMock()
        session.settings = MagicMock()
        session.project_dir = Path("/tmp/fake")

        with patch.object(
            SuiteRunner, "run_all", new_callable=AsyncMock, return_value=[]
        ) as mock_run:
            result = await dispatch(session, "/evals")
            assert result is True
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_evals_with_agent_filter(self):
        session = MagicMock()
        session.pool = MagicMock()
        session.settings = MagicMock()
        session.project_dir = Path("/tmp/fake")

        with patch.object(
            SuiteRunner, "run_all", new_callable=AsyncMock, return_value=[]
        ) as mock_run:
            await dispatch(session, "/evals editor")
            mock_run.assert_called_once_with(
                pool=session.pool,
                settings=session.settings,
                project_dir=session.project_dir,
                agent_filter="editor",
            )

    def test_evals_registered(self):
        assert "evals" in CommandHandler.builtin_names()


class TestToolTraceEntry:
    """Post-refactor ``CaseResult.tool_trace`` holds
    :class:`ToolTraceEntry` (Pydantic) instead of raw ``dict``.
    Locks in the model shape and the :class:`ToolArgDriver.check`
    contract that now consumes it."""

    def test_defaults(self):
        entry = ToolTraceEntry(name="save_file")
        assert entry.name == "save_file"
        assert entry.args is None
        assert entry.result_preview == ""
        assert entry.error is False

    def test_extra_fields_rejected(self):
        # ``extra="forbid"`` — a stray field means Agno's schema
        # drifted and we want it to fail loud, not silently ignored.
        with pytest.raises(ValidationError):
            ToolTraceEntry(name="x", unknown_field="value")  # type: ignore[call-arg]

    def test_dump_matches_legacy_dict_shape(self):
        # Previous shape: {"name","args","result_preview","error"} —
        # ``model_dump()`` MUST produce that same shape so any
        # downstream JSON-reporter integration doesn't see key drift.
        entry = ToolTraceEntry(
            name="save_file",
            args={"path": "/tmp/x"},
            result_preview="ok",
            error=False,
        )
        assert entry.model_dump() == {
            "name": "save_file",
            "args": {"path": "/tmp/x"},
            "result_preview": "ok",
            "error": False,
        }

    def test_check_tool_arg_assertion_matches_pydantic_trace(self):
        # End-to-end for the check function: pydantic trace + typed
        # assertions → assertion matches when args contain the
        # required key/value pair.
        trace = [
            ToolTraceEntry(name="spawn_team", args={"mode": "coordinate"}),
            ToolTraceEntry(name="save_file", args={"path": "/tmp/x"}),
        ]
        check = ToolArgDriver.check(
            trace,
            [ToolArgAssertion(tool="spawn_team", args_must_contain={"mode": "coordinate"})],
        )
        assert check.ok is True
        assert "all tool-arg assertions matched" in check.detail

    def test_check_tool_arg_assertion_reports_missing(self):
        # No matching call → clear failure message.
        trace = [ToolTraceEntry(name="save_file", args={"path": "/tmp/x"})]
        check = ToolArgDriver.check(
            trace,
            [ToolArgAssertion(tool="spawn_team", args_must_contain={"mode": "coordinate"})],
        )
        assert check.ok is False
        assert "missing tool-arg matches" in check.detail
        assert "spawn_team" in check.detail

    def test_check_tool_arg_assertion_skips_missing_tool_field(self):
        # An assertion naming no tool is skipped — it has no call to match.
        trace = [ToolTraceEntry(name="save_file", args={})]
        check = ToolArgDriver.check(trace, [ToolArgAssertion(args_must_contain={"x": 1})])
        assert check.ok is True

    def test_check_tool_arg_assertion_handles_none_args(self):
        # ``args=None`` (no args captured) must not crash the check.
        trace = [ToolTraceEntry(name="save_file", args=None)]
        check = ToolArgDriver.check(
            trace, [ToolArgAssertion(tool="save_file", args_must_contain={"path": "/x"})]
        )
        # Should be a clean failure ("no match"), not an exception.
        assert check.ok is False
        assert "save_file" in check.detail
