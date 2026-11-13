"""An entry name with surrounding spaces is written under its stripped name, and kept.

``check_name`` strips the name, so ``" reviewer "`` is the file ``reviewer.md``.
What the pass records as written has to be that same name, or the prune that
follows removes the file it has just written and the group silently loses the
entry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from igni_core.core.config.group_policy.cache import GroupPolicyCache
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.pack import GroupPolicyPack


def _pack(*entries: GroupPolicyEntry) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Legal",
        fetched_at=datetime.now(timezone.utc),
        entries=list(entries),
    )


@pytest.fixture
def cache(tmp_path: Path) -> GroupPolicyCache:
    return GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "group-policy"))


@pytest.mark.parametrize(
    ("kind", "template"),
    [("agents", "{}.md"), ("commands", "{}.md"), ("scripts", "{}.sh"), ("mcps", "{}.json")],
)
def test_a_padded_name_is_written_and_survives_the_prune(
    cache: GroupPolicyCache, kind: str, template: str
):
    entry = GroupPolicyEntry(
        kind=kind, entry_name="  padded ", content="{}", content_type="markdown"
    )
    cache.materialize(_pack(entry))
    written = cache.layout.dir_for(kind) / template.format("padded")
    assert written.is_file()


def test_a_padded_skill_keeps_its_directory(cache: GroupPolicyCache):
    entry = GroupPolicyEntry(
        kind="skills", entry_name=" padded ", content="body", content_type="markdown"
    )
    cache.materialize(_pack(entry))
    assert (cache.layout.dir_for("skills") / "padded" / "SKILL.md").read_text() == "body"


def test_a_padded_legacy_plugin_file_survives_the_prune(cache: GroupPolicyCache):
    entry = GroupPolicyEntry(
        kind="plugins", entry_name=" legacy ", content="name: x", content_type="yaml"
    )
    cache.materialize(_pack(entry))
    assert (cache.layout.dir_for("plugins") / "legacy.yaml").is_file()


def test_a_padded_installed_plugin_is_not_removed_as_planted(cache: GroupPolicyCache):
    installed = cache.layout.dir_for("plugins") / "tool"
    installed.mkdir(parents=True)
    (installed / "plugin.yaml").write_text("name: tool")
    entry = GroupPolicyEntry(
        kind="plugins",
        entry_name=" tool ",
        content="",
        content_type="yaml",
        source_url="https://example.invalid/tool.git",
    )
    cache.materialize(_pack(entry), rebuild=True)
    assert (installed / "plugin.yaml").is_file()
