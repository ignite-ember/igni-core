"""An extension shaping an agent must not be undone by the file it shapes.

The applier reads the winning definition's file as the base text. When that
file is gone, or no longer decodes, there is no base text: an override still
stands, an edit has nothing to land on, and neither takes the session down.
An edit the text cannot take, or one aimed at an agent nobody defines, leaves
the agent exactly as it was.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.agents.contributions import AgentContributionApplier
from igni_core.core.agents.pool import AgentPool
from igni_core.core.agents.schemas import AgentPriority
from igni_core.core.config.settings import Settings
from igni_core.core.prompts import PromptFragments, PromptLoader
from igni_core.core.prompts.contribution_models import EditOp, PromptEdit, PromptOverride
from igni_core.core.prompts.contributions import PromptContributions

PROJECT = (
    "---\nname: explorer\ndescription: the project's\n---\n\n"
    "<!-- section:rules -->\nthe project's rules\n<!-- /section:rules -->\n"
)
OVERRIDE = "---\nname: explorer\ndescription: acme's\n---\n\nacme's body\n"


def _agent_file(project: Path) -> Path:
    path = project / ".igni" / "agents" / "explorer.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PROJECT, encoding="utf-8")
    return path


def _loaded(project: Path) -> AgentPool:
    pool = AgentPool()
    pool.load_definitions(Settings(), project)
    return pool


def _apply(pool: AgentPool, contributions: PromptContributions) -> None:
    AgentContributionApplier(contributions, prompt_loader=PromptLoader(PromptFragments())).apply_to(
        pool
    )


def _overriding() -> PromptContributions:
    contributions = PromptContributions()
    contributions.set_owner(
        "acme.scan",
        "Acme Scan",
        overrides=[PromptOverride(target="agent:explorer", text=OVERRIDE)],
    )
    return contributions


def _editing(section: str = "rules") -> PromptContributions:
    contributions = PromptContributions()
    contributions.set_owner(
        "acme.scan",
        "Acme Scan",
        edits=[
            PromptEdit(
                target="agent:explorer", op=EditOp.REPLACE, section=section, text="acme's rules"
            )
        ],
    )
    return contributions


def test_an_override_stands_when_the_agent_file_has_vanished(tmp_path) -> None:
    path = _agent_file(tmp_path)
    pool = _loaded(tmp_path)
    path.unlink()

    _apply(pool, _overriding())

    entry = pool.get_entry("explorer")
    assert entry.priority is AgentPriority.EXTENSION_OVERRIDE
    assert "acme's body" in entry.definition.system_prompt


def test_an_override_stands_when_the_agent_file_is_not_utf8(tmp_path) -> None:
    path = _agent_file(tmp_path)
    pool = _loaded(tmp_path)
    path.write_bytes(b"---\nname: explorer\ndescription: \xff\xfe\n---\nbody")

    _apply(pool, _overriding())

    entry = pool.get_entry("explorer")
    assert entry.priority is AgentPriority.EXTENSION_OVERRIDE
    assert "acme's body" in entry.definition.system_prompt


def test_an_edit_with_no_readable_file_leaves_the_agent_as_it_was(tmp_path) -> None:
    path = _agent_file(tmp_path)
    pool = _loaded(tmp_path)
    before = pool.get_entry("explorer")
    path.write_bytes(b"\xff\xfe not text")

    _apply(pool, _editing())

    assert pool.get_entry("explorer") == before


def test_an_edit_the_text_cannot_take_leaves_the_agent_as_it_was(tmp_path) -> None:
    _agent_file(tmp_path)
    pool = _loaded(tmp_path)
    before = pool.get_entry("explorer")

    _apply(pool, _editing(section="no-such-section"))

    assert pool.get_entry("explorer") == before
    assert "the project's rules" in before.definition.system_prompt


def test_an_edit_to_an_agent_nobody_defines_adds_nothing(tmp_path) -> None:
    pool = _loaded(tmp_path)

    _apply(pool, _editing())

    assert pool.get_entry("explorer") is None


def test_with_nothing_contributed_the_pool_is_untouched(tmp_path) -> None:
    _agent_file(tmp_path)
    pool = _loaded(tmp_path)
    before = pool.get_entry("explorer")

    _apply(pool, PromptContributions())

    assert pool.get_entry("explorer") == before
