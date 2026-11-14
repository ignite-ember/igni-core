"""The coordinator turns what the session holds into the main agent.

Its phases read the session and nothing else: the prompt (with the
user-memory tool taught only when it is attached), the instructions list,
the model and its context window, the spec's constants, and the order
``build`` runs them in. Collaborators are injected recorders, so what
reaches them is what the coordinator decided.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.agents import AgentPool
from igni_core.core.config.settings import Settings
from igni_core.core.paths import MAIN_AGENT_NAME
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.fragments import PromptFragments
from igni_core.core.prompts.loader import PromptLoader
from igni_core.core.session.agent_builder.coordinator import MainAgentBuilder

_MEMORY_TOOL = "update_user_memory"


class _Model:
    """Stands for the model object the registry hands back."""


class _ModelRegistry:
    window = 100_000
    seen: list[Settings] = []

    def __init__(self, settings: Settings) -> None:
        type(self).seen.append(settings)
        self.model = _Model()

    def get_model(self) -> _Model:
        return self.model

    def get_context_window(self) -> int:
        return self.window


class _Compression:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _style(name: str, body: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, body=body)


class _Workspace:
    additional_dirs: list[Path] = []

    def __init__(self, context: str = "") -> None:
        self._context = context

    def get_context_instructions(self) -> str:
        return self._context


class _Session:
    def __init__(self, project_dir: Path, *, machine: object | None = None) -> None:
        self.project_dir = project_dir
        self.settings = Settings()
        self.learning_mgr = SimpleNamespace(machine=machine)
        self.pool = AgentPool()
        self.skill_pool = SimpleNamespace(describe=lambda: "")
        self.prompt_contributions = PromptContributions()
        self.project_instructions = "Always run the tests."
        self.workspace = _Workspace("WORKSPACE CONTEXT")
        self.output_style = SimpleNamespace(
            styles={"terse": _style("terse", "Be terse.")}, active="terse"
        )
        self.db = object()
        self.session_id = "sess-9"
        self.user_id = "user-9"
        self.hook_calls = 0
        self.hook = object()
        self.sync_hooks: list[Any] = []

    def tool_event_hook(self) -> object:
        self.hook_calls += 1
        return self.hook

    def memory_sync_hooks(self) -> list[Any]:
        return list(self.sync_hooks)


def _builder(
    session: _Session, *, agent_cls: Any = object, guardrails: Any = None
) -> MainAgentBuilder:
    _ModelRegistry.window = 100_000
    return MainAgentBuilder(
        session,  # type: ignore[arg-type]
        agent_cls=agent_cls,
        registry_cls=object,
        permissions_cls=object,
        compression_cls=_Compression,
        model_registry_cls=_ModelRegistry,
        reasoning_factory=lambda settings: None,
        guardrails_factory=guardrails or (lambda settings: None),
        prompt_loader=PromptLoader(PromptFragments()),
    )


class TestThePromptTeachesTheMemoryToolOnlyWhenItIsAttached:
    def test_a_learning_machine_with_user_memory_on_teaches_it(self, tmp_path):
        session = _Session(tmp_path, machine=object())
        session.settings.learning.user_memory = True
        assert _MEMORY_TOOL in _builder(session).build_prompt()

    def test_no_learning_machine_never_names_it(self, tmp_path):
        session = _Session(tmp_path, machine=None)
        session.settings.learning.user_memory = True
        assert _MEMORY_TOOL not in _builder(session).build_prompt()

    def test_a_machine_with_user_memory_off_never_names_it(self, tmp_path):
        session = _Session(tmp_path, machine=object())
        session.settings.learning.user_memory = False
        assert _MEMORY_TOOL not in _builder(session).build_prompt()


class TestThePromptIsFilledFromTheSession:
    def test_the_agent_catalogue_comes_from_the_pool(self, tmp_path):
        prompt = _builder(_Session(tmp_path)).build_prompt()
        assert "(no agents loaded)" in prompt

    def test_skill_descriptions_come_from_the_skill_pool(self, tmp_path):
        session = _Session(tmp_path)
        session.skill_pool = SimpleNamespace(describe=lambda: "- /ship: ships it")
        prompt = _builder(session).build_prompt()
        assert prompt.endswith("- /ship: ships it")

    def test_the_sessions_contributions_shape_it(self, tmp_path):
        from igni_core.core.prompts.contribution_models import EditOp, PromptEdit

        session = _Session(tmp_path)
        session.prompt_contributions.set_owner(
            "a.ext",
            "Ay",
            edits=[PromptEdit(target="main", op=EditOp.APPEND, name="tail", text="AY WAS HERE")],
        )
        assert _builder(session).build_prompt().endswith("AY WAS HERE")


class TestTheInstructionsAreFilledFromTheSession:
    def test_they_follow_the_prompt_in_the_documented_order(self, tmp_path):
        session = _Session(tmp_path)
        out = _builder(session).build_instructions("THE PROMPT")
        assert out[0] == "THE PROMPT"
        assert out[1] == "Project instructions:\nAlways run the tests."
        assert out[2] == "WORKSPACE CONTEXT"
        assert out[3].startswith("PLAN MODE")
        assert out[4] == "# Output style: terse\n\nBe terse."
        assert len(out) == 6

    def test_an_active_style_that_is_not_known_adds_no_block(self, tmp_path):
        session = _Session(tmp_path)
        session.output_style.active = "missing"
        out = _builder(session).build_instructions("P")
        assert not any(entry.startswith("# Output style") for entry in out)


class TestTheModel:
    def test_the_window_is_the_models_when_it_is_the_smaller(self, tmp_path):
        session = _Session(tmp_path)
        session.settings.models.max_context_window = 500_000
        builder = _builder(session)
        _ModelRegistry.window = 64_000
        model, window = builder.build_model()
        assert isinstance(model, _Model)
        assert window == 64_000

    def test_the_window_is_capped_by_the_settings(self, tmp_path):
        session = _Session(tmp_path)
        session.settings.models.max_context_window = 32_000
        builder = _builder(session)
        _ModelRegistry.window = 200_000
        assert builder.build_model()[1] == 32_000

    def test_the_registry_is_built_from_the_sessions_settings(self, tmp_path):
        session = _Session(tmp_path)
        _ModelRegistry.seen.clear()
        _builder(session).build_model()
        assert _ModelRegistry.seen == [session.settings]


class TestTheSpec:
    def _spec(self, session: _Session, *, window: int = 1000, **builder_kwargs):
        model = _Model()
        hook = object()
        spec = _builder(session, **builder_kwargs).build_spec(
            model=model,
            tools=["t"],
            instructions=["i"],
            context_window=window,
            tool_event_hook=hook,
        )
        return spec, model, hook

    def test_the_constants_the_main_agent_runs_on(self, tmp_path):
        session = _Session(tmp_path, machine=object())
        spec, model, _ = self._spec(session)
        assert spec.name == MAIN_AGENT_NAME
        assert spec.model is model
        assert spec.retries == 0
        assert spec.markdown is True
        assert spec.num_history_runs == 10000
        assert spec.add_history_to_context is True
        assert spec.enable_agentic_memory is False
        assert spec.enable_session_summaries is False
        assert spec.add_session_summary_to_context is True
        assert spec.stream is True and spec.stream_events is True
        assert spec.knowledge is None and spec.search_knowledge is False
        assert spec.add_learnings_to_context is True
        assert spec.compress_tool_results is True
        assert spec.telemetry is False

    def test_what_the_session_holds_is_passed_on(self, tmp_path):
        machine = object()
        session = _Session(tmp_path, machine=machine)
        session.settings.memory.add_memories_to_context = False
        spec, _, _ = self._spec(session)
        assert spec.db is session.db
        assert spec.session_id == "sess-9" and spec.user_id == "user-9"
        assert spec.learning is machine
        assert spec.add_memories_to_context is False

    def test_compression_triggers_at_eighty_percent_of_the_window(self, tmp_path):
        spec, model, _ = self._spec(_Session(tmp_path), window=1001)
        assert spec.compression_manager.kwargs == {
            "model": model,
            "compress_tool_results": True,
            "compress_token_limit": 800,
        }

    def test_a_window_of_zero_compresses_at_zero(self, tmp_path):
        spec, _, _ = self._spec(_Session(tmp_path), window=0)
        assert spec.compression_manager.kwargs["compress_token_limit"] == 0

    def test_guardrails_come_from_the_factory_given_the_settings(self, tmp_path):
        session = _Session(tmp_path)
        seen: list[Settings] = []

        def factory(settings: Settings) -> list[str]:
            seen.append(settings)
            return ["guard"]

        spec, _, _ = self._spec(session, guardrails=factory)
        assert spec.pre_hooks == ["guard"]
        assert seen == [session.settings]

    def test_the_tool_event_hook_comes_first_then_the_memory_sync_hooks(self, tmp_path):
        session = _Session(tmp_path)
        sync = [object(), object()]
        session.sync_hooks = sync
        spec, _, hook = self._spec(session)
        assert spec.tool_hooks == [hook, *sync]

    def test_a_local_session_has_only_the_tool_event_hook(self, tmp_path):
        spec, _, hook = self._spec(_Session(tmp_path))
        assert spec.tool_hooks == [hook]


class TestBuild:
    def _build(self, session: _Session, monkeypatch: pytest.MonkeyPatch):
        kwargs: dict[str, Any] = {}

        def agent_cls(**given: Any) -> str:
            kwargs.update(given)
            return "agent"

        builder = _builder(session, agent_cls=agent_cls)
        monkeypatch.setattr(builder, "build_tools", lambda: ["the-tools"])
        return builder.build(), kwargs

    def test_the_agent_is_built_from_every_phase(self, tmp_path, monkeypatch):
        session = _Session(tmp_path, machine=object())
        agent, kwargs = self._build(session, monkeypatch)
        assert agent == "agent"
        assert kwargs["tools"] == ["the-tools"]
        assert kwargs["name"] == MAIN_AGENT_NAME
        assert isinstance(kwargs["model"], _Model)
        # Several instructions are folded into one block that opens with the prompt.
        [block] = kwargs["instructions"]
        assert block.startswith("- You are igni")
        assert "Project instructions:\nAlways run the tests." in block
        assert "# Output style: terse" in block
        assert kwargs["compression_manager"].kwargs["compress_token_limit"] == 80_000

    def test_one_tool_event_hook_is_made_and_shared(self, tmp_path, monkeypatch):
        session = _Session(tmp_path)
        _, kwargs = self._build(session, monkeypatch)
        assert session.hook_calls == 1
        assert kwargs["tool_hooks"] == [session.hook]

    def test_a_phase_that_fails_stops_the_build(self, tmp_path, monkeypatch):
        session = _Session(tmp_path)
        session.workspace = None  # the instructions phase cannot read it
        with pytest.raises(AttributeError):
            self._build(session, monkeypatch)
        assert session.hook_calls == 0
