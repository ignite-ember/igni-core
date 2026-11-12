"""``scripts/_repro_explorer_no_tool_calls.py`` reproduces "the explorer narrates without acting".

It needs a live model to run for real, so these tests drive what is its
own: reading ``.env`` without overriding the environment, the model it
builds from ``IGNI_TEST_LLM_*``, the explorer agent it assembles from the
bundled prompt, and the report it prints about a response. The response
comes from a stand-in agent; the model is the one thing not under audit.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "_repro_explorer_no_tool_calls.py"
# The old product name, spelled in pieces so the repository guard stays meaningful.
OLD_NAME = "em" + "ber"
LLM_VARS = ("IGNI_TEST_LLM_MODEL", "IGNI_TEST_LLM_API_KEY", "IGNI_TEST_LLM_BASE_URL")


def _load(monkeypatch, cwd: Path):
    """Import the script afresh with ``cwd`` as the working directory."""
    monkeypatch.chdir(cwd)
    spec = importlib.util.spec_from_file_location("explorer_repro_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_env(monkeypatch):
    for name in LLM_VARS:
        monkeypatch.delenv(name, raising=False)


class TestTheEnvFile:
    def test_it_fills_in_what_the_environment_lacks(self, tmp_path, monkeypatch, clean_env):
        (tmp_path / ".env").write_text(
            "# a comment\n"
            "IGNI_TEST_LLM_MODEL=from-file\n"
            "no equals sign here\n"
            'IGNI_TEST_LLM_BASE_URL="http://x/v1"\n'
        )
        _load(monkeypatch, tmp_path)

        assert os.environ["IGNI_TEST_LLM_MODEL"] == "from-file"
        # Quotes are kept literally, not parsed.
        assert os.environ["IGNI_TEST_LLM_BASE_URL"] == '"http://x/v1"'
        monkeypatch.delenv("IGNI_TEST_LLM_MODEL")
        monkeypatch.delenv("IGNI_TEST_LLM_BASE_URL")

    def test_it_never_overrides_a_variable_already_set(self, tmp_path, monkeypatch, clean_env):
        monkeypatch.setenv("IGNI_TEST_LLM_MODEL", "from-shell")
        (tmp_path / ".env").write_text("IGNI_TEST_LLM_MODEL=from-file\n")
        _load(monkeypatch, tmp_path)

        assert os.environ["IGNI_TEST_LLM_MODEL"] == "from-shell"

    def test_without_an_env_file_it_just_imports(self, tmp_path, monkeypatch, clean_env):
        _load(monkeypatch, tmp_path)
        assert "IGNI_TEST_LLM_MODEL" not in os.environ


class TestTheModel:
    def test_it_comes_from_the_igni_test_llm_variables(self, tmp_path, monkeypatch, clean_env):
        monkeypatch.setenv("IGNI_TEST_LLM_MODEL", "some-model")
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
        monkeypatch.setenv("IGNI_TEST_LLM_BASE_URL", "http://localhost:9/v1")
        module = _load(monkeypatch, tmp_path)

        model = module.build_model()

        assert model.id == "some-model"
        assert model.base_url == "http://localhost:9/v1"

    def test_the_old_product_spelling_is_not_read(self, tmp_path, monkeypatch, clean_env):
        # Spelled in pieces: the repository's guard forbids the bare old prefix.
        old = "EM" + "BER_TEST_LLM_"
        for suffix in ("MODEL", "API_KEY", "BASE_URL"):
            monkeypatch.setenv(old + suffix, "old")
        module = _load(monkeypatch, tmp_path)

        with pytest.raises(KeyError, match="IGNI_TEST_LLM_MODEL"):
            module.build_model()

    def test_a_missing_variable_is_named_in_the_error(self, tmp_path, monkeypatch, clean_env):
        monkeypatch.setenv("IGNI_TEST_LLM_MODEL", "m")
        module = _load(monkeypatch, tmp_path)

        with pytest.raises(KeyError, match="IGNI_TEST_LLM_API_KEY"):
            module.build_model()


class TestTheExplorerAgent:
    @pytest.fixture
    def agent(self, tmp_path, monkeypatch, clean_env):
        monkeypatch.setenv("IGNI_TEST_LLM_MODEL", "m")
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
        monkeypatch.setenv("IGNI_TEST_LLM_BASE_URL", "http://localhost:9/v1")
        module = _load(monkeypatch, REPO)
        return module.build_explorer_agent(tmp_path)

    def test_its_prompt_is_the_bundled_one_without_front_matter(self, agent):
        bundled = (REPO / "agents" / "explorer.md").read_text()
        body = bundled.split("\n---\n", 1)[1]

        assert bundled.startswith("---\n")
        assert agent.instructions == body
        assert not agent.instructions.startswith("---")
        assert "name: explorer" not in agent.instructions

    def test_it_is_named_explorer(self, agent):
        assert agent.name == "explorer"

    def test_it_has_the_web_and_shell_toolkits_and_nothing_that_edits(self, agent):
        assert [type(toolkit).__name__ for toolkit in agent.tools] == [
            "WebTools",
            "DuckDuckGoTools",
            "IgniShellTools",
        ]

    def test_a_prompt_without_front_matter_is_used_whole(self, tmp_path, monkeypatch, clean_env):
        monkeypatch.setenv("IGNI_TEST_LLM_MODEL", "m")
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
        monkeypatch.setenv("IGNI_TEST_LLM_BASE_URL", "http://localhost:9/v1")
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "explorer.md").write_text("Just a prompt.\n")
        module = _load(monkeypatch, tmp_path)

        assert module.build_explorer_agent(tmp_path / "w").instructions == "Just a prompt.\n"

    def test_outside_the_repo_root_the_prompt_cannot_be_found(
        self, tmp_path, monkeypatch, clean_env
    ):
        monkeypatch.setenv("IGNI_TEST_LLM_MODEL", "m")
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
        monkeypatch.setenv("IGNI_TEST_LLM_BASE_URL", "http://localhost:9/v1")
        module = _load(monkeypatch, tmp_path)

        with pytest.raises(FileNotFoundError):
            module.build_explorer_agent(tmp_path)


class _StandIn:
    def __init__(self, response):
        self._response = response

    async def arun(self, task, stream=False):
        self.task = task
        return self._response


def _report(module, monkeypatch, capsys, response) -> tuple[str, _StandIn]:
    agent = _StandIn(response)
    monkeypatch.setattr(module, "build_explorer_agent", lambda work_dir: agent)
    asyncio.run(module.main())
    return capsys.readouterr().out, agent


class TestTheReport:
    @pytest.fixture
    def module(self, tmp_path, monkeypatch, clean_env):
        return _load(monkeypatch, tmp_path)

    def test_it_sends_the_long_task_and_names_a_file_that_exists(self, module, monkeypatch, capsys):
        out, agent = _report(module, monkeypatch, capsys, SimpleNamespace(content="done"))

        assert agent.task == module.LONG_TASK
        assert "Read igni.md" in agent.task
        assert (REPO / "igni.md").is_file()
        assert OLD_NAME not in agent.task.lower()
        assert "igni-repro-" in out and OLD_NAME not in out.lower()

    def test_it_prints_the_response_and_every_tool_call_it_saw(self, module, monkeypatch, capsys):
        response = SimpleNamespace(
            content="narration",
            tools=[
                SimpleNamespace(tool_name="Bash", tool_args={"command": "ls"}, result="a\nb"),
                SimpleNamespace(tool_name="WebFetch", tool_args=None, result=None),
            ],
            messages=[],
        )
        out, _ = _report(module, monkeypatch, capsys, response)

        assert "response.content (len=9):" in out
        assert "narration" in out
        assert "response.tools (count=2):" in out
        assert f"[0] Bash({json.dumps({'command': 'ls'})})  →  'a b'" in out
        assert "[1] WebFetch(null)  →  ''" in out

    def test_a_long_tool_result_is_cut_to_two_hundred_characters(self, module, monkeypatch, capsys):
        response = SimpleNamespace(
            content="x",
            tools=[SimpleNamespace(tool_name="Bash", tool_args={}, result="z" * 500)],
            messages=[],
        )
        out, _ = _report(module, monkeypatch, capsys, response)

        assert "z" * 200 in out and "z" * 201 not in out

    def test_an_empty_response_says_so_and_counts_zero(self, module, monkeypatch, capsys):
        out, _ = _report(
            module,
            monkeypatch,
            capsys,
            SimpleNamespace(content=None, tools=None, messages=None),
        )

        assert "response.content (len=0):" in out
        assert "<empty>" in out
        assert "response.tools (count=0):" in out
        assert "response.messages — turn-by-turn (count=0):" in out

    def test_it_lists_each_turn_with_the_tools_the_model_called(self, module, monkeypatch, capsys):
        messages = [
            SimpleNamespace(role="user", content="go", tool_calls=None),
            SimpleNamespace(
                role="assistant",
                content="",
                tool_calls=[
                    {"function": {"name": "Bash"}},
                    {"name": "Read"},
                    SimpleNamespace(name="WebFetch"),
                ],
            ),
            SimpleNamespace(role="assistant", content=["a", "b"], tool_calls=None),
        ]
        out, _ = _report(
            module,
            monkeypatch,
            capsys,
            SimpleNamespace(content="c", tools=[], messages=messages),
        )

        assert "[0] role=user" in out and "content: go" in out
        assert "[1] role=assistant  tool_calls=['Bash', 'Read', 'WebFetch']" in out
        assert "content: ['a', 'b']" in out

    def test_it_sweeps_finish_reasons_and_metrics_and_survives_odd_ones(
        self, module, monkeypatch, capsys
    ):
        messages = [
            SimpleNamespace(role="assistant", content="x", finish_reason="stop", metrics=None),
            SimpleNamespace(
                role="assistant", content="y", stop_reason="end_turn", metrics={"finish_x": 1}
            ),
            SimpleNamespace(role="assistant", content="z", metrics="a string, not a dict"),
            SimpleNamespace(role="assistant", content="w", metrics=3),
        ]
        out, _ = _report(
            module,
            monkeypatch,
            capsys,
            SimpleNamespace(content="c", tools=[], messages=messages),
        )

        assert "msg[0].finish_reason = 'stop'" in out
        assert "msg[1].stop_reason = 'end_turn'" in out
        assert "msg[1].metrics.finish_x = 1" in out
