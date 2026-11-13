"""Each rules tier loads on its own, from the readers and paths it is given.

No loader is built here: a tier is a frozen value that holds its readers
and its paths, so these tests construct the tiers directly, with real
files, and with one reader swapped by keyword where the point is that
the tier uses what it holds.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from igni_core.core.paths import CONFIG_DIR
from igni_core.core.utils import context_managed
from igni_core.core.utils.context_loader import (
    ManagedTier,
    MemoryTier,
    ProjectDirsTier,
    ProjectRootTier,
    RulesReaders,
    SubdirectoryTier,
    UserTier,
)
from igni_core.core.utils.context_project import (
    load_project_rules_dirs,
    load_subdirectory_rules,
)
from igni_core.core.utils.context_readers import (
    read_if_exists,
    read_rules_dir,
    read_rules_dir_files,
    read_with_imports,
    rules_filenames,
)
from igni_core.core.utils.context_schemas import SubdirectoryRules, UserRulesPaths


class _Sandbox:
    """Memory paths that point into a test directory."""

    def __init__(self, root: Path) -> None:
        self.igni = root / "igni_mem"
        self.claude = root / "claude_mem"

    def igni_dir(self) -> Path:
        return self.igni

    def claude_dir(self) -> Path:
        return self.claude


@pytest.fixture(autouse=True)
def _no_managed_policy(monkeypatch):
    monkeypatch.setattr(context_managed, "platform_managed_rules_dir", lambda: None)


def _user_paths(root: Path) -> UserRulesPaths:
    return UserRulesPaths(
        legacy_file=root / "rules.md",
        rules_dir=root / "rules",
        claude_rules_dir=root / "claude-rules",
    )


# ── RulesReaders ───────────────────────────────────────────────────


def test_the_readers_default_to_the_real_helpers():
    readers = RulesReaders()
    assert readers.read_if_exists is read_if_exists
    assert readers.read_with_imports is read_with_imports
    assert readers.rules_filenames is rules_filenames
    assert readers.read_rules_dir is read_rules_dir
    assert readers.read_rules_dir_files is read_rules_dir_files


def test_a_reader_swapped_by_keyword_is_the_one_a_tier_uses(tmp_path):
    (tmp_path / "igni.md").write_text("on disk")
    seen: list[Path] = []

    def fake(directory, filenames, allowed_root=None):
        seen.append(directory)
        return "from the swapped reader"

    tier = ProjectRootTier(readers=RulesReaders(read_rules_dir=fake), project_dir=tmp_path)
    assert tier.load() == "from the swapped reader"
    assert seen == [tmp_path]


def test_the_readers_and_the_tiers_are_frozen(tmp_path):
    with pytest.raises(dataclasses.FrozenInstanceError):
        RulesReaders().read_if_exists = read_with_imports  # type: ignore[misc]
    tier = ProjectRootTier(readers=RulesReaders(), project_dir=tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        tier.read_claude_md = False  # type: ignore[misc]


# ── Headings ───────────────────────────────────────────────────────


def test_each_tier_carries_its_own_section_heading(tmp_path):
    readers = RulesReaders()
    assert ManagedTier(readers=readers).heading == "Managed Policy"
    assert MemoryTier(project_dir=tmp_path).heading == "Memory Index"
    assert UserTier(readers=readers, paths=_user_paths(tmp_path)).heading == "User Rules"
    assert ProjectRootTier(readers=readers, project_dir=tmp_path).heading == "Project Rules"
    assert ProjectDirsTier(readers=readers, project_dir=tmp_path).heading == "Project Shared Rules"


# ── Managed, memory, user ──────────────────────────────────────────


def test_the_managed_tier_reads_the_platform_directory_and_honours_the_flag(tmp_path, monkeypatch):
    (tmp_path / "igni.md").write_text("IGNI POLICY")
    (tmp_path / "CLAUDE.md").write_text("CLAUDE POLICY")
    monkeypatch.setattr(context_managed, "platform_managed_rules_dir", lambda: tmp_path)
    assert ManagedTier(readers=RulesReaders()).load() == "IGNI POLICY\n\nCLAUDE POLICY"
    assert ManagedTier(readers=RulesReaders(), read_claude_md=False).load() == "IGNI POLICY"


def test_the_managed_tier_is_empty_when_there_is_no_policy_directory():
    assert ManagedTier(readers=RulesReaders()).load() == ""


def test_the_memory_tier_reads_the_index_from_the_paths_it_is_given(tmp_path):
    paths = _Sandbox(tmp_path)
    paths.igni.mkdir()
    (paths.igni / "MEMORY.md").write_text("- [a](a.md)\n")
    tier = MemoryTier(project_dir=tmp_path / "proj", paths=paths)
    assert tier.load() == "- [a](a.md)\n"


def test_the_memory_tier_falls_back_to_claude_only_when_the_flag_allows(tmp_path):
    paths = _Sandbox(tmp_path)
    paths.claude.mkdir()
    (paths.claude / "MEMORY.md").write_text("claude index")
    proj = tmp_path / "proj"
    assert MemoryTier(project_dir=proj, paths=paths).load() == "claude index"
    assert MemoryTier(project_dir=proj, paths=paths, read_claude_md=False).load() == ""


def test_the_memory_tier_caps_the_index_at_two_hundred_lines(tmp_path):
    paths = _Sandbox(tmp_path)
    paths.igni.mkdir()
    (paths.igni / "MEMORY.md").write_text("".join(f"line {i}\n" for i in range(250)))
    out = MemoryTier(project_dir=tmp_path, paths=paths).load()
    assert out.count("\n") == 200
    assert out.endswith("line 199\n")


def test_the_memory_tier_without_paths_uses_the_real_home_layout(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    proj = tmp_path / "proj"
    proj.mkdir()
    slug = str(proj.resolve()).replace("/", "-")
    memory = home / CONFIG_DIR / "projects" / slug / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("home index")
    assert MemoryTier(project_dir=proj).load() == "home index"


def test_the_user_tier_reads_its_three_locations_and_the_claude_flag(tmp_path):
    paths = _user_paths(tmp_path)
    paths.legacy_file.write_text("LEGACY")
    paths.rules_dir.mkdir()
    (paths.rules_dir / "a.md").write_text("IGNI RULE")
    paths.claude_rules_dir.mkdir()
    (paths.claude_rules_dir / "b.md").write_text("CLAUDE RULE")
    readers = RulesReaders()
    assert UserTier(readers=readers, paths=paths).load() == "LEGACY\n\nIGNI RULE\n\nCLAUDE RULE"
    assert (
        UserTier(readers=readers, paths=paths, read_claude_md=False).load() == "LEGACY\n\nIGNI RULE"
    )


# ── Project tiers ──────────────────────────────────────────────────


def test_the_project_root_tier_puts_local_overrides_after_the_base(tmp_path):
    (tmp_path / "igni.local.md").write_text("LOCAL")
    (tmp_path / "igni.md").write_text("BASE")
    tier = ProjectRootTier(readers=RulesReaders(), project_dir=tmp_path)
    assert tier.load() == "BASE\n\nLOCAL"


def test_the_project_root_tier_drops_claude_files_without_the_flag(tmp_path):
    (tmp_path / "igni.md").write_text("IGNI")
    (tmp_path / "CLAUDE.md").write_text("CLAUDE")
    assert (
        ProjectRootTier(readers=RulesReaders(), project_dir=tmp_path, read_claude_md=False).load()
        == "IGNI"
    )


def _rules_dir(project: Path, name: str, body: str) -> None:
    directory = project / name / "rules"
    directory.mkdir(parents=True)
    (directory / "r.md").write_text(body)


def test_the_project_dirs_tier_joins_igni_rules_before_claude_rules(tmp_path):
    _rules_dir(tmp_path, ".claude", "CLAUDE RULE")
    _rules_dir(tmp_path, CONFIG_DIR, "IGNI RULE")
    tier = ProjectDirsTier(readers=RulesReaders(), project_dir=tmp_path)
    assert tier.load() == "IGNI RULE\n\nCLAUDE RULE"


def test_the_project_dirs_tier_skips_claude_rules_without_the_flag(tmp_path):
    _rules_dir(tmp_path, ".claude", "CLAUDE RULE")
    _rules_dir(tmp_path, CONFIG_DIR, "IGNI RULE")
    tier = ProjectDirsTier(readers=RulesReaders(), project_dir=tmp_path, read_claude_md=False)
    assert tier.load() == "IGNI RULE"


def test_the_project_dirs_tier_filters_by_the_working_directory(tmp_path):
    directory = tmp_path / CONFIG_DIR / "rules"
    directory.mkdir(parents=True)
    (directory / "web.md").write_text("---\npaths:\n  - web*\n---\nWEB ONLY")
    (directory / "all.md").write_text("EVERYWHERE")
    (tmp_path / "web").mkdir()
    (tmp_path / "api").mkdir()
    readers = RulesReaders()
    inside = ProjectDirsTier(readers=readers, project_dir=tmp_path, working_dir=tmp_path / "web")
    outside = ProjectDirsTier(readers=readers, project_dir=tmp_path, working_dir=tmp_path / "api")
    assert inside.load() == "EVERYWHERE\n\nWEB ONLY"
    assert outside.load() == "EVERYWHERE"


def test_the_project_dirs_function_is_empty_with_neither_directory(tmp_path):
    assert load_project_rules_dirs(read_rules_dir_files, tmp_path) == ""


def test_the_project_dirs_function_passes_the_project_and_working_dirs_to_the_reader(tmp_path):
    calls: list[tuple[Path, Path | None, Path | None]] = []

    def reader(directory, working_dir=None, project_dir=None):
        calls.append((directory, working_dir, project_dir))
        return ""

    work = tmp_path / "w"
    load_project_rules_dirs(reader, tmp_path, working_dir=work)
    assert calls == [
        (tmp_path / CONFIG_DIR / "rules", work, tmp_path),
        (tmp_path / ".claude" / "rules", work, tmp_path),
    ]


# ── Subdirectory tier ──────────────────────────────────────────────


def _walk(tmp_path: Path) -> Path:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (tmp_path / "igni.md").write_text("ROOT")
    (tmp_path / "a" / "igni.md").write_text("A")
    (deep / "CLAUDE.md").write_text("B")
    return deep


def test_the_subdirectory_tier_returns_each_walked_directory_shallowest_first(tmp_path):
    deep = _walk(tmp_path)
    tier = SubdirectoryTier(readers=RulesReaders(), project_dir=tmp_path, working_dir=deep)
    assert tier.load() == [
        SubdirectoryRules(rel_path="a", content="A"),
        SubdirectoryRules(rel_path="a/b", content="B"),
    ]


def test_the_subdirectory_tier_drops_claude_files_without_the_flag(tmp_path):
    deep = _walk(tmp_path)
    tier = SubdirectoryTier(
        readers=RulesReaders(), project_dir=tmp_path, working_dir=deep, read_claude_md=False
    )
    assert tier.load() == [SubdirectoryRules(rel_path="a", content="A")]


def test_the_subdirectory_tier_is_empty_without_a_working_dir_at_the_root_or_outside(tmp_path):
    _walk(tmp_path)
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    (elsewhere / "igni.md").write_text("STRAY")
    readers = RulesReaders()
    for working_dir in (None, tmp_path, elsewhere):
        tier = SubdirectoryTier(readers=readers, project_dir=tmp_path, working_dir=working_dir)
        assert tier.load() == []


def test_a_symlinked_working_dir_is_resolved_before_the_walk(tmp_path):
    deep = _walk(tmp_path)
    link = tmp_path.parent / f"{tmp_path.name}-link"
    link.symlink_to(deep, target_is_directory=True)
    try:
        found = load_subdirectory_rules(
            read_rules_dir, rules_filenames, project_dir=tmp_path, working_dir=link
        )
    finally:
        link.unlink()
    assert [s.rel_path for s in found] == ["a", "a/b"]
