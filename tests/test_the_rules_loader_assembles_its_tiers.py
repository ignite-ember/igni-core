"""``RulesContextLoader``: the tiers it builds from its own state, the
bundle ``load_all`` assembles from them, and the refusal of a tier it
does not have."""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.utils import context_managed as context_managed_module
from igni_core.core.utils.context import RulesContextLoader
from igni_core.core.utils.context_loader import (
    ManagedTier,
    MemoryTier,
    ProjectDirsTier,
    ProjectRootTier,
    SubdirectoryTier,
    UserTier,
)
from igni_core.core.utils.context_memory_paths import MemoryPaths
from igni_core.core.utils.context_schemas import UserRulesPaths


class _Memory(MemoryPaths):
    def __init__(self, igni: Path) -> None:
        self._igni = igni

    def igni_dir(self) -> Path:
        return self._igni

    def claude_dir(self) -> Path:
        return self._igni.parent / "no_claude_memory"


@pytest.fixture(autouse=True)
def _no_managed_policy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(context_managed_module, "platform_managed_rules_dir", lambda: None)


def _loader(project: Path, **kwargs) -> RulesContextLoader:
    kwargs.setdefault(
        "user_rules_paths",
        UserRulesPaths(
            legacy_file=project.parent / "none.md",
            rules_dir=project.parent / "none-dir",
            claude_rules_dir=project.parent / "none-claude-dir",
        ),
    )
    kwargs.setdefault("memory_paths", _Memory(project.parent / "memory"))
    return RulesContextLoader(project_dir=project, **kwargs)


def test_the_loader_builds_one_tier_of_each_flat_kind_in_prompt_order(tmp_path: Path):
    kinds = [type(t) for t in _loader(tmp_path).tiers()]
    assert kinds == [ManagedTier, MemoryTier, UserTier, ProjectRootTier, ProjectDirsTier]


def test_the_tiers_carry_the_loaders_own_state(tmp_path: Path):
    working = tmp_path / "src"
    loader = _loader(tmp_path, working_dir=working, read_claude_md=False)
    managed, memory, user, root, shared = loader.tiers()
    assert isinstance(memory, MemoryTier) and memory.paths is loader.memory_paths
    assert isinstance(user, UserTier) and user.paths is loader.user_rules_paths
    for tier in (managed, memory, user, root, shared):
        assert tier.read_claude_md is False
    assert isinstance(root, ProjectRootTier) and root.project_dir == tmp_path
    assert isinstance(shared, ProjectDirsTier) and shared.working_dir == working


def test_a_tier_the_loader_does_not_have_is_a_lookup_error(tmp_path: Path):
    with pytest.raises(LookupError, match="SubdirectoryTier"):
        _loader(tmp_path)._tier(SubdirectoryTier)


def test_with_no_rules_anywhere_the_bundle_has_no_sections(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    bundle = _loader(project, working_dir=project).load_all()
    assert bundle.sections == []
    assert bundle.render() == ""


def test_the_memory_path_the_loader_is_given_is_where_the_memory_index_is_read(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "MEMORY.md").write_text("- remembered\n")
    loader = _loader(project)
    assert loader.load_memory() == "- remembered\n"
    assert [(s.heading, s.body) for s in loader.load_all().sections] == [
        ("Memory Index", "- remembered\n")
    ]


def test_sections_are_the_flat_tiers_then_each_directory_shallowest_first(tmp_path: Path):
    project = tmp_path / "project"
    deep = project / "a" / "b"
    deep.mkdir(parents=True)
    (project / "igni.md").write_text("ROOT")
    (project / ".igni" / "rules").mkdir(parents=True)
    (project / ".igni" / "rules" / "shared.md").write_text("SHARED")
    (project / "a" / "igni.md").write_text("A")
    (deep / "igni.md").write_text("B")
    sections = _loader(project, working_dir=deep).load_all().sections
    assert [s.heading for s in sections] == [
        "Project Rules",
        "Project Shared Rules",
        "Directory Rules (a/)",
        "Directory Rules (a/b/)",
    ]
    assert [s.body for s in sections] == ["ROOT", "SHARED", "A", "B"]


def test_without_a_working_dir_no_directory_is_walked(tmp_path: Path):
    project = tmp_path / "project"
    (project / "a").mkdir(parents=True)
    (project / "a" / "igni.md").write_text("A")
    loader = _loader(project)
    assert loader.load_subdirectory() == []
    assert loader.load_all().sections == []


def test_a_working_dir_outside_the_project_walks_nothing(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "igni.md").write_text("OUT")
    assert _loader(project, working_dir=outside).load_subdirectory() == []


def test_turning_off_claude_md_turns_off_every_claude_source(tmp_path: Path):
    project = tmp_path / "project"
    sub = project / "src"
    sub.mkdir(parents=True)
    (project / "CLAUDE.md").write_text("C-ROOT")
    (project / ".claude" / "rules").mkdir(parents=True)
    (project / ".claude" / "rules" / "r.md").write_text("C-SHARED")
    (sub / "CLAUDE.md").write_text("C-SUB")
    (project / "igni.md").write_text("I-ROOT")
    off = _loader(project, working_dir=sub, read_claude_md=False).load_all().render()
    assert "C-" not in off
    assert "I-ROOT" in off
    on = _loader(project, working_dir=sub).load_all().render()
    assert all(text in on for text in ("C-ROOT", "C-SHARED", "C-SUB"))
