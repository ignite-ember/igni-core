"""The pack's entry names become paths, and the server is not trusted.

``GroupPolicyCache`` materialises a group's pack to
``~/.igni/group-policy/<kind>/<entry_name><ext>``, and ``Path`` accepts a
separator without complaint. Verified before the fix: the path for the
agent ``../../../../../../tmp/PWNED`` resolved to
``/tmp/PWNED.md``, and the ``scripts`` kind the same — those are written
mode 0700, so the primitive is an executable dropped anywhere the user
can write, on every machine in the org.

Checked here and not only on the server because this is the side that
must not be talked into it. A client can be pointed at a server somebody
else runs, and "the server validates it" is not a property this process
can verify.

Rejects rather than sanitises: a name that has to be rewritten to be safe
is not the name the admin thinks they published, and quietly writing
``....tmp.PWNED.md`` would leave both sides believing different things
about what shipped.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from igni_core.core.config.group_policy.cache import GroupPolicyCache
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.layout import EntryPlacement, GroupPolicyLayout
from igni_core.core.config.group_policy.pack import GroupPolicyPack

ESCAPES = [
    "../../../../../../tmp/PWNED",
    "../escape",
    "/tmp/PWNED",
    "..",
    ".",
    "a/b/c",
    "~/.ssh/authorized_keys",
    "..\\..\\evil",
    "nul\x00byte",
    "",
    "   ",
    "C:evil",
]


def _entry(kind: str, name: str, content: str = "body") -> GroupPolicyEntry:
    return GroupPolicyEntry(
        kind=kind, entry_name=name, content=content, content_type="markdown", enabled=True
    )


class TestSafeEntryName:
    @pytest.mark.parametrize("name", ESCAPES)
    def test_it_refuses_anything_path_shaped(self, name: str):
        assert not _entry("agents", name).safe_name().ok

    @pytest.mark.parametrize(
        "name", ["reviewer", "pre-pr-review", "a.b", "a_b-c.d", ".mcp", "UPPER1"]
    )
    def test_ordinary_names_pass_through_unchanged(self, name: str):
        check = _entry("agents", name).safe_name()
        assert check.ok and check.name == name

    def test_the_message_names_the_entry(self):
        """An admin reading a log needs to know which entry to fix."""
        assert "escape" in _entry("agents", "../escape").safe_name().reason


#: The filename template each kind's loader expects (``kinds.KindRegistry``).
TEMPLATES = {"agents": "{name}.md", "scripts": "{name}.sh", "skills": "{name}/SKILL.md"}


def _place(layout: GroupPolicyLayout, kind: str, name: str) -> EntryPlacement:
    return layout.place(kind, TEMPLATES[kind], _entry(kind, name))


class TestPlacementStaysInside:
    @pytest.mark.parametrize("kind", ["agents", "scripts"])
    @pytest.mark.parametrize("name", ["../../../../../../tmp/PWNED", "/tmp/PWNED", ".."])
    def test_it_refuses_to_build_an_escaping_path(self, tmp_path: Path, kind: str, name: str):
        placement = _place(GroupPolicyLayout(root=tmp_path / "group-policy"), kind, name)

        assert not placement.ok and placement.path is None

    @pytest.mark.parametrize("kind", ["agents", "scripts"])
    def test_an_ordinary_name_lands_inside(self, tmp_path: Path, kind: str):
        cache_dir = tmp_path / "group-policy"

        path = _place(GroupPolicyLayout(root=cache_dir), kind, "reviewer").path

        assert cache_dir.resolve() in path.resolve().parents


class TestMaterializeSkipsRatherThanDies:
    def test_one_bad_entry_does_not_cost_the_rest(self, tmp_path: Path):
        """Without this, the first unsafe name aborts the materialise and
        the session comes up with no agents, no hooks and no MCP servers
        — a worse outcome than the one entry being missing.

        The warning is captured with a handler attached to the module's
        own logger rather than through ``caplog``. Under the full suite an
        earlier test's logging configuration kept the record from reaching
        caplog at all, so the assertion passed in this file and failed in
        the suite — which is a test problem, not a product one, and not
        worth leaving as a mystery.
        """
        cache_dir = tmp_path / "group-policy"
        cache = GroupPolicyCache(GroupPolicyLayout(root=cache_dir))
        pack = GroupPolicyPack(
            group_id="g",
            group_name="G",
            fetched_at=0.0,
            entries=[
                _entry("agents", "good-one", "---\nname: good-one\ndescription: d\n---\nbody"),
                _entry("agents", "../../../../../../tmp/PWNED"),
                _entry("scripts", "../../../../../../tmp/PWNED-SH", "#!/bin/sh\necho hi"),
                _entry("agents", "also-good", "---\nname: also-good\ndescription: d\n---\nbody"),
            ],
        )

        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        module_logger = logging.getLogger("igni_core.core.config.group_policy")
        handler = _Collect()
        previous_level = module_logger.level
        # A local handler is used rather than ``caplog``: pytest's capture
        # handler gets removed from the root logger by an earlier test, so
        # anything emitted is not recorded. That part is real.
        #
        # This also used to clear ``logger.disabled``, for a suppression
        # whose cause was never identified. That is no longer reproducible —
        # instrumenting ``logging.config.fileConfig`` and ``dictConfig``
        # across the suite recorded zero calls to either, and a probe on
        # this logger was never disabled in 3432 tests. See
        # ``tests/test_http_retry.py`` for the full account.
        #
        # An assertion replaces the clearing, so a recurrence fails loudly
        # instead of a log assertion passing for the wrong reason.
        assert not module_logger.disabled, (
            "this logger is disabled, so the assertions below would pass "
            "vacuously — nothing it emits is recorded"
        )
        module_logger.addHandler(handler)
        module_logger.setLevel(logging.WARNING)
        try:
            cache.materialize(pack)
        finally:
            module_logger.removeHandler(handler)
            module_logger.setLevel(previous_level)

        written = {p.name for p in cache_dir.rglob("*") if p.is_file()}
        assert "good-one.md" in written
        assert "also-good.md" in written
        assert not Path("/tmp/PWNED.md").exists()
        assert not Path("/tmp/PWNED-SH.sh").exists()

        # And it said so, naming the entry — silence would leave an admin
        # wondering why their agent never appeared.
        messages = [record.getMessage() for record in records]
        assert any("skipped" in message for message in messages), messages
        assert any("PWNED" in message for message in messages), messages

    def test_nothing_is_written_outside_the_cache_directory(self, tmp_path: Path):
        """The property that matters, asserted directly rather than by
        listing the names it expects: every file the materialise produced
        is under the cache root.
        """
        cache_dir = tmp_path / "group-policy"
        cache = GroupPolicyCache(GroupPolicyLayout(root=cache_dir))
        pack = GroupPolicyPack(
            group_id="g",
            group_name="G",
            fetched_at=0.0,
            entries=[_entry("agents", name) for name in ESCAPES if name.strip()]
            + [_entry("agents", "legit", "---\nname: legit\ndescription: d\n---\nbody")],
        )

        cache.materialize(pack)

        root = cache_dir.resolve()
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert root in path.resolve().parents, f"{path} is outside {root}"


class TestSkillsAreDirectoriesAndStillContained:
    """The case that made the first containment check wrong.

    The ``skills`` kind's template is ``{name}/SKILL.md`` — a separator the
    *template* owns, not the entry name. Asserting that a written path's
    parent IS the kind directory therefore rejected every legitimate
    skill, so the property is "under the kind directory" instead.

    Which means the check has to keep holding for skills specifically,
    both ways.
    """

    def test_a_skill_lands_in_its_own_directory(self, tmp_path: Path):
        cache_dir = tmp_path / "group-policy"

        path = _place(GroupPolicyLayout(root=cache_dir), "skills", "deploy").path

        assert path.name == "SKILL.md"
        assert path.parent.name == "deploy"
        assert cache_dir.resolve() in path.resolve().parents

    @pytest.mark.parametrize("name", ["../../../../../../tmp/PWNED", "..", "a/b"])
    def test_a_skill_name_cannot_escape_either(self, tmp_path: Path, name: str):
        """The template's own separator must not become cover for one in
        the name."""
        placement = _place(GroupPolicyLayout(root=tmp_path / "group-policy"), "skills", name)

        assert not placement.ok
