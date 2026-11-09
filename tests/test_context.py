"""Tests for utils/context.py — hierarchical rules loading."""

import json
import logging
from pathlib import Path

import pytest

from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.utils import context_managed as context_managed_module
from igni_core.core.utils import context_memory as context_memory_module
from igni_core.core.utils.context import RulesContextLoader
from igni_core.core.utils.context_frontmatter import parse_frontmatter
from igni_core.core.utils.context_memory import (
    ProjectMemoryBank,
    _claude_project_memory_dir,
    _igni_project_memory_dir,
    _project_memory_slug,
)
from igni_core.core.utils.context_schemas import UserRulesPaths

# Alias for the deleted ``settings._platform_managed_settings_path``
# shim — kept as a local name so the assertion sites below still read
# naturally.
_real_settings_path = ManagedPolicySource.platform_path

# The real managed-rules lookup, captured before the autouse fixture
# neutralises it — the platform-mapping tests need the real answer.
_REAL_MANAGED_RULES_DIR = context_managed_module.platform_managed_rules_dir


@pytest.fixture(autouse=True)
def _isolate_managed_rules(monkeypatch):
    """Default every context test to "no managed policy deployed."

    Keeps the existing user/project tests hermetic — a stray
    ``/Library/Application Support/igni/igni.md`` on the dev
    machine won't bleed into their assertions. Managed-rules
    tests opt in by monkeypatching the lookup themselves; the
    captured-at-import reference ``_REAL_MANAGED_RULES_DIR`` lets
    the platform-mapping tests still reach the real function."""
    monkeypatch.setattr(
        context_managed_module,
        "platform_managed_rules_dir",
        lambda: None,
    )


def _user_rules_paths(root: Path, *, with_claude: bool = False) -> UserRulesPaths:
    """User-rule locations under ``root`` — a hermetic sandbox."""
    return UserRulesPaths(
        legacy_file=root / "rules.md",
        rules_dir=root / "rules",
        claude_rules_dir=root / "claude-rules" if with_claude else root / "nonexistent",
    )


def _loader(
    project_dir: Path,
    *,
    working_dir: Path | None = None,
    read_claude_md: bool = True,
    user_rules: UserRulesPaths | None = None,
) -> RulesContextLoader:
    """A rules loader whose user-rule locations point at an empty sandbox.

    The sandbox keeps the developer's real ``~/.igni/rules`` out of
    every assertion in this file.
    """
    return RulesContextLoader(
        project_dir=project_dir,
        working_dir=working_dir,
        read_claude_md=read_claude_md,
        user_rules_paths=(
            user_rules
            if user_rules is not None
            else UserRulesPaths(
                legacy_file=project_dir / "_no_user_rules.md",
                rules_dir=project_dir / "_no_user_rules_dir",
                claude_rules_dir=project_dir / "_no_claude_user_rules_dir",
            )
        ),
    )


class TestLoadProjectRules:
    def test_loads_igni_md(self, tmp_path):
        (tmp_path / "igni.md").write_text("igni rules")
        assert _loader(tmp_path).load_project_root() == "igni rules"

    def test_loads_claude_md(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("claude rules")
        assert _loader(tmp_path).load_project_root() == "claude rules"

    def test_merges_both_files(self, tmp_path):
        (tmp_path / "ember.md").write_text("igni rules")
        (tmp_path / "CLAUDE.md").write_text("claude rules")
        result = _loader(tmp_path).load_project_root()
        assert "igni rules" in result
        assert "claude rules" in result

    def test_skips_claude_md_when_disabled(self, tmp_path):
        (tmp_path / "ember.md").write_text("igni rules")
        (tmp_path / "CLAUDE.md").write_text("claude rules")
        result = _loader(tmp_path, read_claude_md=False).load_project_root()
        assert "igni rules" in result
        assert "claude rules" not in result

    def test_returns_empty_for_missing(self, tmp_path):
        assert _loader(tmp_path).load_project_root() == ""


class TestLoadSubdirectoryRules:
    def test_collects_subdirectory_rules(self, tmp_path):
        src = tmp_path / "src"
        auth = src / "auth"
        working = auth / "middleware"
        working.mkdir(parents=True)
        (src / "ember.md").write_text("src rules")
        (auth / "ember.md").write_text("auth rules")

        results = _loader(tmp_path, working_dir=working).load_subdirectory()
        assert len(results) == 2
        assert (results[0].rel_path, results[0].content) == ("src", "src rules")
        assert (results[1].rel_path, results[1].content) == ("src/auth", "auth rules")

    def test_collects_claude_md_from_subdirectories(self, tmp_path):
        src = tmp_path / "src"
        working = src / "api"
        working.mkdir(parents=True)
        (src / "CLAUDE.md").write_text("claude src rules")

        results = _loader(tmp_path, working_dir=working).load_subdirectory()
        assert len(results) == 1
        assert (results[0].rel_path, results[0].content) == ("src", "claude src rules")

    def test_merges_both_files_in_subdirectory(self, tmp_path):
        src = tmp_path / "src"
        working = src / "api"
        working.mkdir(parents=True)
        (src / "ember.md").write_text("claude src")
        (src / "CLAUDE.md").write_text("claude src")

        results = _loader(tmp_path, working_dir=working).load_subdirectory()
        assert len(results) == 1
        assert "claude src" in results[0].content

    def test_returns_empty_when_no_rules(self, tmp_path):
        working = tmp_path / "src"
        working.mkdir()
        assert _loader(tmp_path, working_dir=working).load_subdirectory() == []

    def test_returns_empty_when_working_dir_is_root(self, tmp_path):
        assert _loader(tmp_path, working_dir=tmp_path).load_subdirectory() == []

    def test_returns_empty_when_working_dir_is_none(self, tmp_path):
        assert _loader(tmp_path, working_dir=None).load_subdirectory() == []

    def test_returns_empty_when_outside_project(self, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        assert _loader(tmp_path / "project", working_dir=other).load_subdirectory() == []


class TestLoadProjectContext:
    def test_merges_root_rules(self, tmp_path):
        (tmp_path / "ember.md").write_text("root rules")
        result = _loader(tmp_path).load_all().render()
        assert "root rules" in result
        assert "Project Rules" in result

    def test_returns_empty_when_no_rules(self, tmp_path):
        assert _loader(tmp_path).load_all().render() == ""

    def test_merges_root_and_subdirectory(self, tmp_path):
        (tmp_path / "ember.md").write_text("root rules")
        src = tmp_path / "src"
        src.mkdir()
        (src / "ember.md").write_text("src rules")

        result = _loader(tmp_path, working_dir=src).load_all().render()
        assert "root rules" in result
        assert "src rules" in result
        assert "Project Rules" in result
        assert "Directory Rules" in result

    def test_claude_md_at_root(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("claude project rules")
        result = _loader(tmp_path).load_all().render()
        assert "claude project rules" in result

    def test_both_files_at_root(self, tmp_path):
        (tmp_path / "ember.md").write_text("igni root")
        (tmp_path / "CLAUDE.md").write_text("claude root")
        result = _loader(tmp_path).load_all().render()
        assert "igni root" in result
        assert "claude root" in result

    def test_skips_claude_md_when_disabled(self, tmp_path):
        (tmp_path / "ember.md").write_text("igni root")
        (tmp_path / "CLAUDE.md").write_text("claude root")
        result = _loader(tmp_path, read_claude_md=False).load_all().render()
        assert "igni root" in result
        assert "claude root" not in result

    def test_sections_separated_by_divider(self, tmp_path):
        (tmp_path / "ember.md").write_text("root")
        src = tmp_path / "src"
        src.mkdir()
        (src / "ember.md").write_text("src")

        result = _loader(tmp_path, working_dir=src).load_all().render()
        assert "---" in result


class TestParseFrontmatter:
    def test_no_frontmatter(self):
        paths, body = parse_frontmatter("hello world\n")
        assert paths == []
        assert body == "hello world\n"

    def test_frontmatter_without_paths(self):
        content = "---\nname: test\n---\nbody text\n"
        paths, body = parse_frontmatter(content)
        assert paths == []
        assert body == "body text\n"

    def test_paths_block(self):
        content = '---\npaths:\n  - "**/*.test.ts"\n  - src/api/**\n---\nscoped rule body'
        paths, body = parse_frontmatter(content)
        assert paths == ["**/*.test.ts", "src/api/**"]
        assert body == "scoped rule body"

    def test_paths_inline_list(self):
        content = "---\npaths: [\"a/*\", 'b/*']\n---\nbody"
        paths, body = parse_frontmatter(content)
        assert paths == ["a/*", "b/*"]
        assert body == "body"


class TestLoadUserRules:
    def test_loads_legacy_single_file(self, tmp_path):
        (tmp_path / "rules.md").write_text("legacy rules")
        result = _loader(tmp_path, user_rules=_user_rules_paths(tmp_path)).load_user()
        assert result == "legacy rules"

    def test_loads_igni_rules_directory(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "a.md").write_text("rule A")
        (rules_dir / "b.md").write_text("rule B")
        result = _loader(tmp_path, user_rules=_user_rules_paths(tmp_path)).load_user()
        assert "rule A" in result
        assert "rule B" in result

    def test_merges_legacy_and_directory(self, tmp_path):
        (tmp_path / "rules.md").write_text("legacy")
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "one.md").write_text("dir rule")
        result = _loader(tmp_path, user_rules=_user_rules_paths(tmp_path)).load_user()
        assert "legacy" in result
        assert "dir rule" in result

    def test_reads_claude_rules_when_enabled(self, tmp_path):
        claude_dir = tmp_path / "claude-rules"
        claude_dir.mkdir()
        (claude_dir / "git.md").write_text("git habits")
        paths = _user_rules_paths(tmp_path, with_claude=True)
        result = _loader(tmp_path, read_claude_md=True, user_rules=paths).load_user()
        assert "git habits" in result

    def test_skips_claude_rules_when_disabled(self, tmp_path):
        claude_dir = tmp_path / "claude-rules"
        claude_dir.mkdir()
        (claude_dir / "git.md").write_text("git habits")
        paths = _user_rules_paths(tmp_path, with_claude=True)
        result = _loader(tmp_path, read_claude_md=False, user_rules=paths).load_user()
        assert "git habits" not in result

    def test_paths_frontmatter_filters_out_when_no_match(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "tui.md").write_text("---\npaths:\n  - tui/**\n---\ntui-only rule")
        project_dir = tmp_path / "project"
        working_dir = project_dir / "backend"
        working_dir.mkdir(parents=True)
        result = _loader(
            project_dir,
            working_dir=working_dir,
            user_rules=_user_rules_paths(tmp_path),
        ).load_user()
        assert "tui-only rule" not in result

    def test_paths_frontmatter_includes_when_match(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "tui.md").write_text("---\npaths:\n  - tui/**\n---\ntui-only rule")
        project_dir = tmp_path / "project"
        working_dir = project_dir / "tui" / "panels"
        working_dir.mkdir(parents=True)
        result = _loader(
            project_dir,
            working_dir=working_dir,
            user_rules=_user_rules_paths(tmp_path),
        ).load_user()
        assert "tui-only rule" in result

    def test_paths_frontmatter_excluded_without_working_dir(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "scoped.md").write_text("---\npaths:\n  - any/**\n---\nscoped")
        (rules_dir / "always.md").write_text("always-on rule")
        result = _loader(tmp_path, user_rules=_user_rules_paths(tmp_path)).load_user()
        assert "always-on rule" in result
        assert "scoped" not in result

    def test_empty_when_nothing_configured(self, tmp_path):
        result = _loader(tmp_path, user_rules=_user_rules_paths(tmp_path)).load_user()
        assert result == ""


class TestProjectRulesDirs:
    """``<project>/.igni/rules/*.md`` and ``<project>/.claude/rules/*.md``
    — committed shared rules, symmetric to the user-level pattern."""

    def _load(self, project_dir, working_dir=None, read_claude_md=True):
        return _loader(
            project_dir,
            working_dir=working_dir,
            read_claude_md=read_claude_md,
        ).load_project_dirs()

    def test_igni_rules_dir_loaded(self, tmp_path):
        (tmp_path / CONFIG_DIR / "rules").mkdir(parents=True)
        (tmp_path / CONFIG_DIR / "rules" / "style.md").write_text("PROJECT-IGNI-STYLE")
        assert "PROJECT-IGNI-STYLE" in self._load(tmp_path)

    def test_claude_rules_dir_loaded(self, tmp_path):
        (tmp_path / ".claude" / "rules").mkdir(parents=True)
        (tmp_path / ".claude" / "rules" / "api.md").write_text("PROJECT-CLAUDE-API")
        assert "PROJECT-CLAUDE-API" in self._load(tmp_path)

    def test_claude_rules_dir_skipped_when_cross_tool_disabled(self, tmp_path):
        (tmp_path / CONFIG_DIR / "rules").mkdir(parents=True)
        (tmp_path / CONFIG_DIR / "rules" / "ok.md").write_text("IGNI-OK")
        (tmp_path / ".claude" / "rules").mkdir(parents=True)
        (tmp_path / ".claude" / "rules" / "skip.md").write_text("CLAUDE-SHOULD-SKIP")
        result = self._load(tmp_path, read_claude_md=False)
        assert "IGNI-OK" in result
        assert "CLAUDE-SHOULD-SKIP" not in result

    def test_paths_frontmatter_filters_scoped_rules(self, tmp_path):
        (tmp_path / CONFIG_DIR / "rules").mkdir(parents=True)
        (tmp_path / CONFIG_DIR / "rules" / "tauri.md").write_text(
            "---\npaths:\n  - 'clients/tauri/**'\n---\nTAURI-ONLY"
        )
        # working_dir doesn't match → file filtered out
        no_match = self._load(tmp_path, working_dir=tmp_path / "src")
        assert "TAURI-ONLY" not in no_match
        # working_dir matches → file contributes
        match_dir = tmp_path / "clients" / "tauri" / "src-tauri"
        match_dir.mkdir(parents=True)
        with_match = self._load(tmp_path, working_dir=match_dir)
        assert "TAURI-ONLY" in with_match

    def test_at_imports_resolve_within_rules_dir(self, tmp_path):
        rules = tmp_path / CONFIG_DIR / "rules"
        rules.mkdir(parents=True)
        (rules / "main.md").write_text("@./shared.md")
        (rules / "shared.md").write_text("SHARED-CONTENT")
        result = self._load(tmp_path)
        assert "SHARED-CONTENT" in result
        assert "@./shared.md" not in result

    def test_at_import_escaping_rules_dir_left_literal(self, tmp_path):
        rules = tmp_path / CONFIG_DIR / "rules"
        rules.mkdir(parents=True)
        # Try to reach project root from inside .igni/rules — should
        # be refused (scope = rules dir, matches user-level behavior).
        (tmp_path / "outside.md").write_text("OUTSIDE")
        (rules / "main.md").write_text("@../../outside.md")
        result = self._load(tmp_path)
        assert "OUTSIDE" not in result

    def test_empty_when_nothing_present(self, tmp_path):
        assert self._load(tmp_path) == ""

    def test_load_project_context_includes_shared_rules_section(self, tmp_path):
        (tmp_path / "ember.md").write_text("ROOT")
        (tmp_path / CONFIG_DIR / "rules").mkdir(parents=True)
        (tmp_path / CONFIG_DIR / "rules" / "shared.md").write_text("SHARED")
        result = _loader(tmp_path).load_all().render()
        # Ordering: root rules before shared rules.
        assert result.index("ROOT") < result.index("SHARED")
        assert "# Project Shared Rules" in result


class TestLocalOverrides:
    """``ember.local.md`` / ``CLAUDE.local.md`` — gitignored personal
    overrides that load after their committed counterpart at every
    level so they take precedence in the agent's read order."""

    def test_project_root_local_loads_after_base(self, tmp_path):
        (tmp_path / "ember.md").write_text("BASE")
        (tmp_path / "ember.local.md").write_text("LOCAL")
        result = _loader(tmp_path).load_project_root()
        assert result.index("BASE") < result.index("LOCAL")

    def test_project_root_claude_local_loads(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("CLAUDE-BASE")
        (tmp_path / "CLAUDE.local.md").write_text("CLAUDE-LOCAL")
        result = _loader(tmp_path, read_claude_md=True).load_project_root()
        assert "CLAUDE-BASE" in result
        assert "CLAUDE-LOCAL" in result
        assert result.index("CLAUDE-BASE") < result.index("CLAUDE-LOCAL")

    def test_claude_local_skipped_when_cross_tool_disabled(self, tmp_path):
        (tmp_path / "ember.md").write_text("EMBER")
        (tmp_path / "CLAUDE.local.md").write_text("SHOULD-NOT-LOAD")
        result = _loader(tmp_path, read_claude_md=False).load_project_root()
        assert "EMBER" in result
        assert "SHOULD-NOT-LOAD" not in result

    def test_local_alone_still_loads_at_root(self, tmp_path):
        (tmp_path / "ember.local.md").write_text("ONLY-LOCAL")
        assert "ONLY-LOCAL" in _loader(tmp_path).load_project_root()

    def test_subdirectory_local_override(self, tmp_path):
        sub = tmp_path / "svc"
        sub.mkdir()
        (sub / "ember.md").write_text("SUB-BASE")
        (sub / "ember.local.md").write_text("SUB-LOCAL")
        results = _loader(tmp_path, working_dir=sub).load_subdirectory()
        # Each subdir's contributions concatenate base + local.
        assert len(results) == 1
        content = results[0].content
        assert content.index("SUB-BASE") < content.index("SUB-LOCAL")


class TestAtImports:
    """``@<path>.md`` import resolution in rules files."""

    def test_relative_import_inlines_content(self, tmp_path):
        (tmp_path / "ember.md").write_text("Project rules.\n\n@./conventions.md")
        (tmp_path / "conventions.md").write_text("Always run make fmt.")
        result = _loader(tmp_path).load_project_root()
        assert "Project rules." in result
        assert "Always run make fmt." in result
        assert "@./conventions.md" not in result  # token replaced

    def test_import_without_dot_slash_prefix(self, tmp_path):
        (tmp_path / "ember.md").write_text("@conventions.md")
        (tmp_path / "conventions.md").write_text("naked path works too")
        assert "naked path works too" in _loader(tmp_path).load_project_root()

    def test_absolute_path_under_project_inlines(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "ref.md").write_text("absolute target")
        (tmp_path / "ember.md").write_text(f"@{sub / 'ref.md'}")
        assert "absolute target" in _loader(tmp_path).load_project_root()

    def test_path_outside_project_left_as_literal(self, tmp_path):
        outside = tmp_path.parent / "outside-rules.md"
        outside.write_text("SECRET STUFF")
        try:
            (tmp_path / "ember.md").write_text(f"@{outside}")
            result = _loader(tmp_path).load_project_root()
            assert "SECRET STUFF" not in result
            # The literal token survives so the agent can see the
            # unresolved reference instead of silent dropping.
            assert "@" in result
        finally:
            outside.unlink(missing_ok=True)

    def test_missing_file_left_as_literal(self, tmp_path):
        (tmp_path / "ember.md").write_text("Hello @./does-not-exist.md.")
        result = _loader(tmp_path).load_project_root()
        assert "@./does-not-exist.md" in result

    def test_nested_imports_resolve(self, tmp_path):
        (tmp_path / "ember.md").write_text("top @./a.md")
        (tmp_path / "a.md").write_text("mid @./b.md")
        (tmp_path / "b.md").write_text("leaf")
        result = _loader(tmp_path).load_project_root()
        assert "top" in result and "mid" in result and "leaf" in result

    def test_cycle_breaks_at_repeat(self, tmp_path):
        """``a → @b → @a`` must not loop. The second ``@a`` leaves
        the literal token in place."""
        (tmp_path / "ember.md").write_text("root @./a.md done")
        (tmp_path / "a.md").write_text("A @./b.md")
        (tmp_path / "b.md").write_text("B @./a.md")
        result = _loader(tmp_path).load_project_root()
        assert "root" in result
        assert "A" in result
        assert "B" in result
        # The cycle re-encounter is left intact rather than recursing.
        assert "@./a.md" in result

    def test_depth_capped(self, tmp_path):
        """Chain longer than ``_IMPORT_MAX_DEPTH`` stops recursing
        and leaves the deepest token literal. The cap is 4 hops
        (Claude Code parity, bumped from 3 on 2026-06-25)."""
        (tmp_path / "ember.md").write_text("@./a.md")
        (tmp_path / "a.md").write_text("A @./b.md")
        (tmp_path / "b.md").write_text("B @./c.md")
        (tmp_path / "c.md").write_text("C @./d.md")
        (tmp_path / "d.md").write_text("D @./e.md")
        (tmp_path / "e.md").write_text("E")
        result = _loader(tmp_path).load_project_root()
        # First four layers resolve.
        assert "A" in result and "B" in result and "C" in result and "D" in result
        # Fifth layer hits the depth cap — token left as literal.
        assert "@./e.md" in result
        assert "\nE\n" not in result and not result.endswith("E")

    def test_inline_code_span_skipped(self, tmp_path):
        """An ``@./foo.md`` token inside a backtick code span is
        left as a literal, not inlined. This lets rules files
        document the ``@`` import syntax inside backticks without
        triggering an accidental import."""
        (tmp_path / "ember.md").write_text("Real: @./real.md but `@./fake.md` should stay as text.")
        (tmp_path / "real.md").write_text("REAL CONTENT")
        (tmp_path / "fake.md").write_text("FAKE CONTENT — must NOT appear")
        result = _loader(tmp_path).load_project_root()
        assert "REAL CONTENT" in result
        assert "FAKE CONTENT" not in result
        # The literal token is preserved (inside its backticks).
        assert "`@./fake.md`" in result

    def test_triple_backtick_fence_skipped(self, tmp_path):
        """Tokens inside a fenced code block are NOT inlined."""
        (tmp_path / "ember.md").write_text(
            "Above.\n\n```\nthis is code: @./fake.md\n```\n\nBelow: @./real.md"
        )
        (tmp_path / "real.md").write_text("REAL")
        (tmp_path / "fake.md").write_text("MUST NOT INLINE")
        result = _loader(tmp_path).load_project_root()
        assert "REAL" in result
        assert "MUST NOT INLINE" not in result
        assert "@./fake.md" in result  # preserved inside the fence

    def test_tilde_fence_skipped(self, tmp_path):
        """Tilde-fenced blocks behave the same as backtick-fenced."""
        (tmp_path / "ember.md").write_text(
            "Above.\n\n~~~\n@./fake.md inside tilde fence\n~~~\n\n@./real.md outside."
        )
        (tmp_path / "real.md").write_text("REAL")
        (tmp_path / "fake.md").write_text("MUST NOT INLINE")
        result = _loader(tmp_path).load_project_root()
        assert "REAL" in result
        assert "MUST NOT INLINE" not in result
        assert "@./fake.md" in result

    def test_fenced_block_with_info_string(self, tmp_path):
        """Fence with a language hint (e.g. ```python) is still
        recognised — the info string is ignored."""
        (tmp_path / "ember.md").write_text(
            "Above.\n\n```python\n# import @./fake.md\n```\n\n@./real.md"
        )
        (tmp_path / "real.md").write_text("REAL")
        (tmp_path / "fake.md").write_text("MUST NOT INLINE")
        result = _loader(tmp_path).load_project_root()
        assert "REAL" in result
        assert "MUST NOT INLINE" not in result

    def test_indented_fence_up_to_three_spaces(self, tmp_path):
        """Up to 3 leading spaces of indent on the opening fence
        still counts as a fenced block (CommonMark)."""
        (tmp_path / "ember.md").write_text(
            "Above.\n\n   ```\n   @./fake.md indented\n   ```\n\n@./real.md"
        )
        (tmp_path / "real.md").write_text("REAL")
        (tmp_path / "fake.md").write_text("MUST NOT INLINE")
        result = _loader(tmp_path).load_project_root()
        assert "REAL" in result
        assert "MUST NOT INLINE" not in result

    def test_mixed_code_and_text_both_handled(self, tmp_path):
        """A file with both a code block and inline code AND a
        legitimate import — only the legit import is inlined."""
        (tmp_path / "ember.md").write_text(
            "Real before: @./real.md\n\n"
            "`@./skipme.md` inline\n\n"
            "```\nblock @./alsoskip.md\n```\n\n"
            "Real after too: @./real2.md"
        )
        (tmp_path / "real.md").write_text("REAL1")
        (tmp_path / "real2.md").write_text("REAL2")
        (tmp_path / "skipme.md").write_text("SKIP-INLINE")
        (tmp_path / "alsoskip.md").write_text("SKIP-BLOCK")
        result = _loader(tmp_path).load_project_root()
        assert "REAL1" in result
        assert "REAL2" in result
        assert "SKIP-INLINE" not in result
        assert "SKIP-BLOCK" not in result
        assert "`@./skipme.md`" in result
        assert "@./alsoskip.md" in result

    def test_imported_file_with_own_code_block(self, tmp_path):
        """A top-level ``@`` imports a file that itself contains
        a code block with another ``@`` token — the inner token
        in the inlined content stays literal."""
        (tmp_path / "ember.md").write_text("Top: @./outer.md")
        (tmp_path / "outer.md").write_text("OUTER\n\n```\ninner: @./inner.md\n```\n")
        (tmp_path / "inner.md").write_text("INNER MUST NOT APPEAR")
        result = _loader(tmp_path).load_project_root()
        assert "OUTER" in result
        assert "INNER MUST NOT APPEAR" not in result
        assert "@./inner.md" in result


class TestPlatformManagedRulesDir:
    """The autouse fixture neutralises the live module lookup;
    these tests reach the real function via the captured
    ``_REAL_MANAGED_RULES_DIR`` reference."""

    def test_darwin_dir(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        p = _REAL_MANAGED_RULES_DIR()
        assert p is not None
        assert str(p) == "/Library/Application Support/igni"

    def test_linux_dir(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        p = _REAL_MANAGED_RULES_DIR()
        assert p is not None
        assert str(p) == "/etc/igni"

    def test_win32_uses_programdata(self, monkeypatch):
        monkeypatch.setenv("PROGRAMDATA", r"C:\TestProgramData")
        monkeypatch.setattr("sys.platform", "win32")
        p = _REAL_MANAGED_RULES_DIR()
        assert p is not None
        assert "igni" in str(p)
        # Honours the environment rather than hardcoding C:\ProgramData.
        assert str(p).startswith(r"C:\TestProgramData")

    @pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
    def test_every_managed_tier_reads_the_same_directory(self, platform, monkeypatch):
        """The regression this class exists for.

        Four modules used to answer "where is managed policy?"
        separately, and MCP disagreed with the other three on *every*
        platform — ``EmberCode`` vs ``Ember`` on macOS, ``/etc/ignite-ember``
        vs ``/etc/ember`` on Linux, and no Windows support at all. So an
        admin had to populate two directory trees, MCP policy could not
        be deployed on Windows, and nothing said so.

        Managed policy is the tier a user is not permitted to override,
        which makes "it was silently not read" the worst failure it has.
        """
        monkeypatch.setattr("sys.platform", platform)
        monkeypatch.setenv("PROGRAMDATA", r"C:\TestProgramData")

        from igni_core.core.config.managed_policy import ManagedPolicySource
        from igni_core.core.paths import managed_policy_dir
        from igni_core.core.plugins.loader import _platform_managed_plugins_root

        shared = managed_policy_dir()
        assert shared is not None

        # The rules directory, the plugin root, and the settings file's
        # parent all have to be the one directory.
        assert _REAL_MANAGED_RULES_DIR() == shared
        assert _platform_managed_plugins_root() == shared
        settings_path = ManagedPolicySource.platform_path()
        assert settings_path is not None
        assert settings_path.parent == shared

    def test_mcp_reads_the_shared_directory(self, tmp_path, monkeypatch):
        """MCP is the one that drifted, so it gets a behavioural test.

        Asserting that the module *imports* ``managed_policy_dir`` proves
        nothing — the import survives while the body goes back to a
        hardcoded path, and a test written that way passes against the
        exact bug it is named after. So: redirect the shared function and
        check the policy is actually read from where it now points.
        """
        from igni_core.core.mcp import config as mcp_config

        monkeypatch.setattr(mcp_config, "managed_policy_dir", lambda: tmp_path)
        (tmp_path / "managed-settings.json").write_text(
            json.dumps({"mcp": {"allowed": ["only-this-one"]}})
        )

        policy = mcp_config.MCPPolicy.from_managed_settings()

        assert policy.allowed == ["only-this-one"]

    def test_mcp_finds_nothing_when_the_shared_directory_is_empty(self, tmp_path, monkeypatch):
        """The other half: an empty managed directory must mean "no
        policy", not a policy that blocks everything."""
        from igni_core.core.mcp import config as mcp_config

        monkeypatch.setattr(mcp_config, "managed_policy_dir", lambda: tmp_path)

        assert mcp_config.MCPPolicy.from_managed_settings().allowed in (None, [], ())

    def test_unknown_platform_returns_none(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "freebsd")
        assert _REAL_MANAGED_RULES_DIR() is None

    def test_sibling_to_managed_settings(self):
        """The managed rules dir IS the same parent directory as
        the managed settings file — by design, both live side by
        side so a sysadmin can drop a full policy bundle in one
        place."""
        rules_dir = _REAL_MANAGED_RULES_DIR()
        settings_path = _real_settings_path()
        if rules_dir is None:
            assert settings_path is None
        else:
            assert settings_path is not None
            assert settings_path.parent == rules_dir


class TestLoadManagedRules:
    """The managed-rules tier reads ``igni.md`` / ``CLAUDE.md`` from
    the platform-specific write-protected dir. Tests override the
    platform lookup via the autouse fixture's monkeypatch."""

    def test_reads_managed_igni_md(self, tmp_path, monkeypatch):
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "igni.md").write_text("ORG: never commit secrets.")
        monkeypatch.setattr(
            context_managed_module,
            "platform_managed_rules_dir",
            lambda: managed_dir,
        )
        assert "ORG: never commit secrets." in _loader(tmp_path).load_managed()

    def test_reads_managed_claude_md_when_enabled(self, tmp_path, monkeypatch):
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "CLAUDE.md").write_text("ORG-CC: be safe.")
        monkeypatch.setattr(
            context_managed_module,
            "platform_managed_rules_dir",
            lambda: managed_dir,
        )
        assert "ORG-CC: be safe." in _loader(tmp_path, read_claude_md=True).load_managed()

    def test_skips_claude_md_when_disabled(self, tmp_path, monkeypatch):
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "igni.md").write_text("ORG-IGNI")
        (managed_dir / "CLAUDE.md").write_text("ORG-CC")
        monkeypatch.setattr(
            context_managed_module,
            "platform_managed_rules_dir",
            lambda: managed_dir,
        )
        out = _loader(tmp_path, read_claude_md=False).load_managed()
        assert "ORG-IGNI" in out
        assert "ORG-CC" not in out

    def test_missing_dir_is_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            context_managed_module,
            "platform_managed_rules_dir",
            lambda: tmp_path / "does-not-exist",
        )
        assert _loader(tmp_path).load_managed() == ""

    def test_unknown_platform_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            context_managed_module,
            "platform_managed_rules_dir",
            lambda: None,
        )
        assert _loader(Path.cwd()).load_managed() == ""

    def test_at_imports_scoped_to_managed_dir(self, tmp_path, monkeypatch):
        """A managed policy can't reach into the user's project
        via ``@<path>.md`` — imports resolve against the managed
        dir only. An escape attempt is left as a literal token."""
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "igni.md").write_text("policy @./detail.md plus @/etc/passwd")
        (managed_dir / "detail.md").write_text("ALLOWED IMPORT")
        monkeypatch.setattr(
            context_managed_module,
            "platform_managed_rules_dir",
            lambda: managed_dir,
        )
        out = _loader(tmp_path).load_managed()
        assert "ALLOWED IMPORT" in out
        # The escape attempt is preserved literally — not inlined.
        assert "@/etc/passwd" in out


class TestManagedPolicyInContextOutput:
    """The managed policy section appears FIRST in the merged rules
    block and is labelled ``# Managed Policy`` so the model can
    identify it."""

    def test_managed_section_appears_first(self, tmp_path, monkeypatch):
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "ember.md").write_text("MANAGED LINE")
        monkeypatch.setattr(
            context_managed_module,
            "platform_managed_rules_dir",
            lambda: managed_dir,
        )
        # Project also has its own rules — managed should still
        # appear before them.
        project = tmp_path / "project"
        project.mkdir()
        (project / "ember.md").write_text("PROJECT LINE")
        out = _loader(project).load_all().render()
        assert "# Managed Policy" in out
        assert "MANAGED LINE" in out
        assert "PROJECT LINE" in out
        assert out.index("MANAGED LINE") < out.index("PROJECT LINE")

    def test_no_managed_section_when_dir_empty(self, tmp_path):
        """No managed file → no ``# Managed Policy`` section
        header. The autouse fixture already stubs the lookup to
        None — this just confirms the loader doesn't synthesize
        a header for an empty section."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "ember.md").write_text("PROJECT LINE")
        out = _loader(project).load_all().render()
        assert "# Managed Policy" not in out
        assert "PROJECT LINE" in out


def _redirect_memory_dirs(monkeypatch, tmp_path, *, with_claude=False):
    """Point both per-project memory dir lookups at sandbox paths
    under ``tmp_path``. Default ``with_claude=False`` so a test
    that only writes an igni-native MEMORY.md doesn't accidentally
    pick up whatever's in the user's real
    ``~/.claude/projects/.../memory/`` (which the slug derivation
    won't normally produce a hit for, but be safe).

    Patches the module that owns the lookups —
    :class:`context_memory.ProjectMemoryBank` resolves them on the
    module namespace at call time, so patching them here is what
    the bank reads.
    """
    igni_dir = tmp_path / "igni_memory"
    claude_dir = tmp_path / "claude_memory" if with_claude else tmp_path / "no_claude_memory"

    ember_fn = lambda _pd, _d=igni_dir: _d  # noqa: E731
    claude_fn = lambda _pd, _d=claude_dir: _d  # noqa: E731

    monkeypatch.setattr(context_memory_module, "_igni_project_memory_dir", ember_fn)
    monkeypatch.setattr(context_memory_module, "_claude_project_memory_dir", claude_fn)
    return igni_dir, claude_dir


class TestProjectMemorySlug:
    """Slug encoding mirrors Claude Code's convention: absolute
    path with ``/`` → ``-``. This shape is what lets the cross-
    tool fallback find an existing CC memory bank for the same
    project without any migration step."""

    def test_unix_path(self):
        # Use a real path that exists so .resolve() doesn't surprise us.
        # The slug shape is what matters, not what the path points at.
        slug = _project_memory_slug(Path("/Users/x/proj"))
        assert slug == "-Users-x-proj"

    def test_paths_relative_to_cwd_resolve_first(self, tmp_path, monkeypatch):
        """Slug derives from the RESOLVED absolute path — so a
        relative path passed in still produces a stable slug."""
        sub = tmp_path / "sub"
        sub.mkdir()
        monkeypatch.chdir(sub)
        slug_abs = _project_memory_slug(sub)
        slug_rel = _project_memory_slug(Path("."))
        assert slug_abs == slug_rel

    def test_dir_paths_match_published_convention(self):
        """The igni and claude memory dirs are siblings under
        their respective home-dir bases."""
        igni = _igni_project_memory_dir(Path("/Users/x/proj"))
        claude = _claude_project_memory_dir(Path("/Users/x/proj"))
        assert igni.parts[-3:] == ("projects", "-Users-x-proj", "memory")
        assert claude.parts[-3:] == ("projects", "-Users-x-proj", "memory")
        assert CONFIG_DIR in igni.parts
        assert ".claude" in claude.parts


class TestLoadMemoryIndex:
    def test_reads_igni_memory_md(self, tmp_path, monkeypatch):
        igni_dir, _ = _redirect_memory_dirs(monkeypatch, tmp_path)
        igni_dir.mkdir()
        (igni_dir / "MEMORY.md").write_text("ONE\nTWO\nTHREE\n")
        assert ProjectMemoryBank(tmp_path).load_index() == "ONE\nTWO\nTHREE\n"

    def test_returns_empty_when_no_index(self, tmp_path, monkeypatch):
        _redirect_memory_dirs(monkeypatch, tmp_path)
        assert ProjectMemoryBank(tmp_path).load_index() == ""

    def test_line_cap_at_200(self, tmp_path, monkeypatch):
        igni_dir, _ = _redirect_memory_dirs(monkeypatch, tmp_path)
        igni_dir.mkdir()
        # 250 short lines — only the first 200 should load.
        body = "".join(f"line{i}\n" for i in range(250))
        (igni_dir / "MEMORY.md").write_text(body)
        out = ProjectMemoryBank(tmp_path).load_index()
        assert "line0" in out
        assert "line199" in out  # 200th line (zero-indexed)
        assert "line200" not in out
        assert out.count("\n") == 200

    def test_byte_cap_at_25kb(self, tmp_path, monkeypatch):
        igni_dir, _ = _redirect_memory_dirs(monkeypatch, tmp_path)
        igni_dir.mkdir()
        # 30 KB on a single line (well under the 200-line cap).
        big = "x" * 30_000 + "\n"
        (igni_dir / "MEMORY.md").write_text(big)
        out = ProjectMemoryBank(tmp_path).load_index()
        assert len(out.encode("utf-8")) <= 25_000
        # Truncation drops the trailing newline; payload starts with x's.
        assert out.startswith("x")

    def test_byte_cap_preserves_utf8(self, tmp_path, monkeypatch):
        """A multi-byte codepoint chopped at the byte cap must
        not produce invalid UTF-8 — ``errors='ignore'`` drops the
        partial sequence cleanly."""
        igni_dir, _ = _redirect_memory_dirs(monkeypatch, tmp_path)
        igni_dir.mkdir()
        # Pad to just under 25_000 bytes, then add a 4-byte emoji
        # that straddles the cap boundary.
        padding = "a" * 24_998
        (igni_dir / "MEMORY.md").write_text(padding + "\U0001f525\n")
        out = ProjectMemoryBank(tmp_path).load_index()
        # Result is valid UTF-8 (re-decoding doesn't raise).
        out.encode("utf-8").decode("utf-8")
        # The fire emoji is the last 4 bytes — at byte cap 25_000
        # with 24_998 padding, only 2 of the 4 bytes fit and get
        # discarded. So the emoji shouldn't appear.
        assert "\U0001f525" not in out

    def test_claude_fallback_when_cross_tool_enabled(self, tmp_path, monkeypatch):
        _, claude_dir = _redirect_memory_dirs(monkeypatch, tmp_path, with_claude=True)
        claude_dir.mkdir()
        (claude_dir / "MEMORY.md").write_text("CC-NATIVE")
        # No igni-native file present → falls back to CC's.
        bank = ProjectMemoryBank(tmp_path, read_claude_fallback=True)
        assert bank.load_index() == "CC-NATIVE"

    def test_claude_fallback_skipped_when_disabled(self, tmp_path, monkeypatch):
        _, claude_dir = _redirect_memory_dirs(monkeypatch, tmp_path, with_claude=True)
        claude_dir.mkdir()
        (claude_dir / "MEMORY.md").write_text("CC-NATIVE")
        bank = ProjectMemoryBank(tmp_path, read_claude_fallback=False)
        assert bank.load_index() == ""

    def test_igni_wins_over_claude_when_both_exist(self, tmp_path, monkeypatch):
        """The igni-native location is the preferred source. If both
        files exist, only the igni version loads — concatenation would
        duplicate near-identical memories during a mid-migration."""
        igni_dir, claude_dir = _redirect_memory_dirs(monkeypatch, tmp_path, with_claude=True)
        igni_dir.mkdir()
        claude_dir.mkdir()
        (igni_dir / "MEMORY.md").write_text("IGNI")
        (claude_dir / "MEMORY.md").write_text("CLAUDE")
        out = ProjectMemoryBank(tmp_path).load_index()
        assert "IGNI" in out
        assert "CLAUDE" not in out


class TestMemoryIndexInContextOutput:
    def test_memory_section_after_managed_before_user(self, tmp_path, monkeypatch):
        """Section order: Managed Policy → Memory Index → User /
        Project Rules. Ensures the agent reads sysadmin
        directives first, then its own remembered context, then
        the rules layered on top."""
        igni_dir, _ = _redirect_memory_dirs(monkeypatch, tmp_path)
        igni_dir.mkdir()
        (igni_dir / "MEMORY.md").write_text("REMEMBERED FACT")

        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "ember.md").write_text("MANAGED")
        monkeypatch.setattr(
            context_managed_module,
            "platform_managed_rules_dir",
            lambda: managed_dir,
        )

        project = tmp_path / "project"
        project.mkdir()
        (project / "ember.md").write_text("PROJECT")
        # Have to override memory lookup again for the actual
        # project_dir we pass in — slug-based lookup means the
        # ``project`` path produces a different slug than
        # ``tmp_path``.
        monkeypatch.setattr(
            context_memory_module,
            "_igni_project_memory_dir",
            lambda _pd, _d=igni_dir: _d,
        )

        out = _loader(project).load_all().render()
        assert "# Managed Policy" in out
        assert "# Memory Index" in out
        assert "REMEMBERED FACT" in out
        # Order check.
        assert out.index("MANAGED") < out.index("REMEMBERED FACT") < out.index("PROJECT")

    def test_no_memory_section_when_no_file(self, tmp_path, monkeypatch):
        _redirect_memory_dirs(monkeypatch, tmp_path)
        project = tmp_path / "project"
        project.mkdir()
        (project / "ember.md").write_text("PROJECT")
        out = _loader(project).load_all().render()
        assert "# Memory Index" not in out
        assert "PROJECT" in out


class TestEnsureMemoryDir:
    def test_creates_when_missing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home)
        target = ProjectMemoryBank(tmp_path / "project").ensure()
        assert target.is_dir()
        assert f"{CONFIG_DIR}/projects/" in str(target)

    def test_idempotent_when_existing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home)
        first = ProjectMemoryBank(tmp_path / "project").ensure()
        # Drop a marker file inside the dir.
        marker = first / "marker.txt"
        marker.write_text("kept")
        # Second call must not wipe the existing dir.
        second = ProjectMemoryBank(tmp_path / "project").ensure()
        assert second == first
        assert marker.read_text() == "kept"

    def test_oserror_is_swallowed(self, tmp_path, monkeypatch, caplog):
        """If ``mkdir`` raises (e.g. read-only filesystem), the
        function logs + returns the intended path without
        raising. Better to fail late on the agent's first save
        than to crash session boot."""
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home)

        def _exploding_mkdir(*_a, **_kw):
            raise OSError("read-only")

        monkeypatch.setattr(Path, "mkdir", _exploding_mkdir)
        with caplog.at_level(logging.DEBUG, logger="igni_core.core.utils.context_memory"):
            target = ProjectMemoryBank(tmp_path / "project").ensure()
        # Returns a path even on failure.
        assert isinstance(target, Path)


class TestMemoryWritebackInstructions:
    """The system-prompt block that teaches the agent how to
    persist memories during a conversation (row 61)."""

    def test_includes_memory_dir_path(self, tmp_path):
        block = ProjectMemoryBank(tmp_path).writeback_instructions()
        # Path appears verbatim so the agent knows WHERE to save.
        assert f"{CONFIG_DIR}/projects/" in block
        assert "memory" in block

    def test_names_all_four_types(self, tmp_path):
        block = ProjectMemoryBank(tmp_path).writeback_instructions()
        for memory_type in ("user", "feedback", "project", "reference"):
            assert memory_type in block

    def test_includes_what_not_to_save(self, tmp_path):
        """The block must explicitly tell the agent NOT to
        save derivable things — otherwise the memory bank
        fills with noise."""
        block = ProjectMemoryBank(tmp_path).writeback_instructions()
        assert "NOT to save" in block or "not save" in block.lower()
        assert "git" in block.lower()
        # Specifically calls out code-patterns / paths as derivable.
        assert "derivable" in block or "paths" in block

    def test_includes_frontmatter_shape(self, tmp_path):
        """Concrete file format with frontmatter so the agent
        produces consistent memories — otherwise the index
        update step is brittle."""
        block = ProjectMemoryBank(tmp_path).writeback_instructions()
        assert "name:" in block
        assert "description:" in block
        # Metadata.type lists the four valid types.
        assert "type:" in block

    def test_mentions_memory_index_file(self, tmp_path):
        """Agent must know about MEMORY.md as the index so
        the read-side prefix budget (row 18) keeps working."""
        block = ProjectMemoryBank(tmp_path).writeback_instructions()
        assert "MEMORY.md" in block
