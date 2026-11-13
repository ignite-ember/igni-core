"""Tests for what each contextual tip fires on and how the registry picks."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from igni_core.core.config.schemas import GuardrailsConfig, PermissionsConfig
from igni_core.core.config.settings import Settings
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.utils import tips
from igni_core.core.utils.tips import (
    ContextualTip,
    GuardrailsOffTip,
    LearningOffTip,
    NoCustomAgentsTip,
    NoProjectContextTip,
    ReasoningOffTip,
    TipContext,
    TipRegistry,
    WebDeniedTip,
    get_tip,
)


def _ctx(tmp_path: Path, **updates: object) -> TipContext:
    return TipContext(settings=Settings().model_copy(update=updates), project_dir=tmp_path)


def test_guardrails_tip_fires_only_when_all_three_guardrails_are_off(tmp_path: Path) -> None:
    off = GuardrailsConfig(pii_detection=False, prompt_injection=False, moderation=False)
    assert GuardrailsOffTip().matches(_ctx(tmp_path, guardrails=off)) is True
    for one_on in ("pii_detection", "prompt_injection", "moderation"):
        g = off.model_copy(update={one_on: True})
        assert GuardrailsOffTip().matches(_ctx(tmp_path, guardrails=g)) is False, one_on


def test_reasoning_and_learning_tips_follow_their_own_switches(tmp_path: Path) -> None:
    base = Settings()
    for reasoning, learning in ((True, False), (False, True), (True, True), (False, False)):
        ctx = _ctx(
            tmp_path,
            reasoning=base.reasoning.model_copy(update={"enabled": reasoning}),
            learning=base.learning.model_copy(update={"enabled": learning}),
        )
        assert ReasoningOffTip().matches(ctx) is (not reasoning)
        assert LearningOffTip().matches(ctx) is (not learning)


def test_the_custom_agents_tip_wants_a_markdown_agent_in_the_config_dir(tmp_path: Path) -> None:
    tip = NoCustomAgentsTip()
    ctx = _ctx(tmp_path)
    assert tip.matches(ctx) is True  # no directory

    agents = tmp_path / CONFIG_DIR / "agents"
    agents.mkdir(parents=True)
    assert tip.matches(ctx) is True  # empty directory

    (agents / "notes.txt").write_text("not an agent")
    assert tip.matches(ctx) is True  # no .md

    (agents / "reviewer.md").write_text("---\nname: reviewer\n---")
    assert tip.matches(ctx) is False
    assert f"{CONFIG_DIR}/agents/" in tip.message


def test_the_web_tip_fires_only_when_web_search_is_denied(tmp_path: Path) -> None:
    for value, fires in (("deny", True), ("allow", False), ("ask", False)):
        ctx = _ctx(tmp_path, permissions=PermissionsConfig(web_search=value))
        assert WebDeniedTip().matches(ctx) is fires, value


def test_the_no_project_context_tip_names_the_configured_file(tmp_path: Path) -> None:
    settings = Settings()
    assert "igni.md" in NoProjectContextTip().render(_ctx(tmp_path))
    settings.context.project_file = "TEAM.md"
    ctx = TipContext(settings=settings, project_dir=tmp_path)
    assert "TEAM.md" in NoProjectContextTip().render(ctx)


def test_the_shipped_catalog_points_at_igni_and_never_at_the_old_name() -> None:
    texts = [*tips.GENERAL_TIPS, *(t.message for t in tips.CONTEXTUAL_TIPS)]
    joined = "\n".join(texts)
    assert "igni --resume" in joined
    assert "igni -m" in joined
    assert "igni-core[web]" in joined
    assert not re.search(r"\bember\b", joined, re.IGNORECASE)


class _Always(ContextualTip):
    id: str = "always"
    message: str = "ALWAYS"

    def matches(self, ctx: TipContext) -> bool:
        return True


class _Never(ContextualTip):
    id: str = "never"
    message: str = "NEVER"

    def matches(self, ctx: TipContext) -> bool:
        return False


class _Broken(ContextualTip):
    id: str = "broken"
    message: str = "BROKEN"

    def matches(self, ctx: TipContext) -> bool:
        raise RuntimeError("predicate blew up")


def test_a_registered_tip_can_be_picked(tmp_path: Path) -> None:
    registry = TipRegistry(contextual=[], general=["GENERAL"])
    assert registry.pick(_ctx(tmp_path)) == "GENERAL"
    registry.register(_Always())
    assert registry.pick(_ctx(tmp_path)) == "ALWAYS"


def test_the_pick_is_one_of_the_matching_tips_only(tmp_path: Path) -> None:
    class _Other(_Always):
        id: str = "other"
        message: str = "OTHER"

    registry = TipRegistry(contextual=[_Always(), _Never(), _Other()], general=["GENERAL"])
    picks = {registry.pick(_ctx(tmp_path)) for _ in range(200)}
    assert picks == {"ALWAYS", "OTHER"}


def test_a_predicate_that_raises_is_no_match_and_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry = TipRegistry(contextual=[_Broken(), _Always()], general=["GENERAL"])
    with caplog.at_level(logging.DEBUG, logger=tips.logger.name):
        picks = {registry.pick(_ctx(tmp_path)) for _ in range(20)}
    assert picks == {"ALWAYS"}
    assert any(
        "broken" in r.getMessage() and "_Broken" in r.getMessage() and "blew up" in r.getMessage()
        for r in caplog.records
    )


def test_a_broken_predicate_alone_falls_back_to_a_general_tip(tmp_path: Path) -> None:
    registry = TipRegistry(contextual=[_Broken()], general=["GENERAL"])
    assert registry.pick(_ctx(tmp_path)) == "GENERAL"


def test_registering_on_a_registry_does_not_change_the_shipped_snapshots() -> None:
    before_contextual = tuple(tips.CONTEXTUAL_TIPS)
    before_general = tuple(tips.GENERAL_TIPS)
    registry = TipRegistry.default()
    registry.register(_Always())
    registry.general.append("EXTRA")
    assert tuple(tips.CONTEXTUAL_TIPS) == before_contextual
    assert tuple(tips.GENERAL_TIPS) == before_general
    assert len(TipRegistry.default().contextual) == len(before_contextual)


def test_a_missing_settings_or_project_dir_gives_a_general_tip(tmp_path: Path) -> None:
    assert get_tip(settings=Settings()) in tips.GENERAL_TIPS
    assert get_tip(project_dir=tmp_path) in tips.GENERAL_TIPS


def test_the_default_registry_has_the_six_shipped_contextual_tips() -> None:
    ids = [t.id for t in TipRegistry.default().contextual]
    assert ids == [
        "no_project_context",
        "guardrails_off",
        "reasoning_off",
        "learning_off",
        "no_custom_agents",
        "web_denied",
    ]
