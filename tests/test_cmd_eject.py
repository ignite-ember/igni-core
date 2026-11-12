"""``/eject`` — the deliberate copy that replaced the automatic one.

Group entries are read from the policy cache and anything the project
declares under the same name outranks them. Overriding one therefore
means having a file, and before this the only ways to get one were to
write it from scratch or to go rummaging in
``~/.igni/group-policy``.

The thing worth testing is not that a file gets copied — it is the
edges, because every one of them is a way to leave somebody with a
worse setup than they started with: silently overwriting an override
they had already made, reporting success for a name the group does not
ship, or offering to eject a kind where overriding is not a thing that
happens.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.backend.cmd_eject import EJECTABLE, EjectCommand
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.paths import CONFIG_DIR


def _session(tmp_path: Path, group: dict[str, dict[str, str]] | None = None):
    """A session whose group ships ``{kind: {name: content}}``."""
    project = tmp_path / "proj"
    (project / CONFIG_DIR).mkdir(parents=True)
    cache = tmp_path / "group-policy"

    for kind, entries in (group or {}).items():
        subdir, filename = EJECTABLE[kind]
        for name, content in entries.items():
            path = cache / subdir / filename.format(name=name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    return SimpleNamespace(
        project_dir=project,
        group_policy=GroupPolicyLayout.for_data_dir(tmp_path),
        reload_group_agents=lambda: True,
    )


class TestTheHappyPath:
    @pytest.mark.asyncio
    async def test_it_copies_the_group_s_version(self, tmp_path: Path):
        session = _session(tmp_path, {"agents": {"reviewer": "---\nname: reviewer\n---\nTheirs."}})
        result = await EjectCommand(session).run("agents reviewer")

        target = session.project_dir / CONFIG_DIR / "agents" / "reviewer.md"
        assert target.read_text() == "---\nname: reviewer\n---\nTheirs."
        assert not result.is_error()

    @pytest.mark.asyncio
    async def test_it_says_the_copy_now_wins(self, tmp_path: Path):
        """Somebody who does not know the ordering has to be told, or
        they will wonder which one is live."""
        session = _session(tmp_path, {"agents": {"reviewer": "body"}})
        result = await EjectCommand(session).run("agents reviewer")
        assert "loads from now on" in result.content

    @pytest.mark.asyncio
    async def test_it_says_the_group_s_changes_stop_reaching_them(self, tmp_path: Path):
        """The cost of taking a copy, stated at the moment it is taken.

        This is what the old conflict prompt used to surface later, and
        the reason it is worth saying up front instead.
        """
        session = _session(tmp_path, {"agents": {"reviewer": "body"}})
        result = await EjectCommand(session).run("agents reviewer")
        assert "no longer reach you" in result.content

    @pytest.mark.asyncio
    async def test_a_skill_keeps_its_directory_shape(self, tmp_path: Path):
        """Skills are a directory with ``SKILL.md`` inside, not a file —
        writing it flat would produce something no loader reads."""
        session = _session(tmp_path, {"skills": {"deploy": "---\nname: deploy\n---\nSteps."}})
        await EjectCommand(session).run("skills deploy")

        assert (session.project_dir / CONFIG_DIR / "skills" / "deploy" / "SKILL.md").is_file()

    @pytest.mark.asyncio
    async def test_a_workflow_keeps_its_extension(self, tmp_path: Path):
        session = _session(tmp_path, {"workflows": {"review": "export const meta = {}\n"}})
        await EjectCommand(session).run("workflows review")

        assert (session.project_dir / CONFIG_DIR / "workflows" / "review.mjs").is_file()


class TestWhatItRefuses:
    @pytest.mark.asyncio
    async def test_an_existing_file_is_left_alone(self, tmp_path: Path):
        """The one that would actually hurt: overwriting an override
        somebody has already written and edited."""
        session = _session(tmp_path, {"agents": {"reviewer": "theirs"}})
        mine = session.project_dir / CONFIG_DIR / "agents" / "reviewer.md"
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text("mine, carefully edited")

        result = await EjectCommand(session).run("agents reviewer")

        assert mine.read_text() == "mine, carefully edited"
        assert "already exists" in result.content

    @pytest.mark.asyncio
    async def test_a_name_the_group_does_not_ship(self, tmp_path: Path):
        session = _session(tmp_path, {"agents": {"reviewer": "theirs"}})
        result = await EjectCommand(session).run("agents nonexistent")

        assert result.is_error()
        assert "nonexistent" in result.content
        # And says what it does ship, so the answer is in the message
        # rather than in another command.
        assert "reviewer" in result.content

    @pytest.mark.asyncio
    async def test_a_kind_that_cannot_be_overridden(self, tmp_path: Path):
        """Hooks and rules are additive, so there is nothing to copy —
        and saying why beats listing what is allowed."""
        session = _session(tmp_path, {"agents": {"a": "x"}})
        result = await EjectCommand(session).run("hooks pre-pr-review")

        assert result.is_error()
        assert "additive" in result.content

    @pytest.mark.asyncio
    async def test_no_group_at_all(self, tmp_path: Path):
        session = _session(tmp_path)
        result = await EjectCommand(session).run("agents reviewer")

        assert result.is_error()
        assert "ships no agents" in result.content

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", ["", "agents", "agents reviewer extra"])
    async def test_usage_when_the_arguments_are_wrong(self, tmp_path: Path, args: str):
        result = await EjectCommand(_session(tmp_path)).run(args)
        assert result.is_error()
        assert "Usage:" in result.content


class TestItTakesEffectNow:
    @pytest.mark.asyncio
    async def test_the_pools_are_rebuilt(self, tmp_path: Path):
        """Ejecting is nearly always a prelude to editing, so waiting for
        the next start would make the command feel like it failed."""
        session = _session(tmp_path, {"agents": {"reviewer": "theirs"}})
        calls: list[bool] = []
        session.reload_group_agents = lambda: calls.append(True) or True

        await EjectCommand(session).run("agents reviewer")
        assert calls == [True]

    @pytest.mark.asyncio
    async def test_a_failed_rebuild_does_not_lose_the_copy(self, tmp_path: Path):
        """The file landing is the thing that matters; a rebuild that
        cannot run is worth a sentence, not a failure."""
        session = _session(tmp_path, {"agents": {"reviewer": "theirs"}})

        def boom():
            raise RuntimeError("pool is unhappy")

        session.reload_group_agents = boom

        result = await EjectCommand(session).run("agents reviewer")

        assert (session.project_dir / CONFIG_DIR / "agents" / "reviewer.md").is_file()
        assert not result.is_error()
        assert "Restart" in result.content


class TestTheKindTable:
    def test_it_only_offers_kinds_a_person_would_edit(self):
        """Configuration the server owns is not ejectable: a project can
        declare its own without a copy to start from."""
        assert set(EJECTABLE) == {
            "agents",
            "skills",
            "commands",
            "output-styles",
            "workflows",
        }

    def test_every_kind_has_a_filename_template(self):
        for kind, (subdir, filename) in EJECTABLE.items():
            assert subdir, kind
            assert "{name}" in filename, kind

    def test_the_command_is_registered(self):
        from igni_core.backend.builtin_command_registry import BUILTIN_REGISTRY

        assert "eject" in BUILTIN_REGISTRY.names()
        assert BUILTIN_REGISTRY.get("/eject") is not None


class TestANameIsOnlyAName:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["../secret", "..", "sub/dir", "a\\b", "../../outside"])
    async def test_a_name_that_is_a_path_is_refused_and_writes_nothing(
        self, tmp_path: Path, name: str
    ):
        """``../secret`` used to resolve to a file next to the group's
        agents directory and be copied to a path outside the project's
        ``agents`` directory."""
        session = _session(tmp_path, {"agents": {"reviewer": "theirs"}})
        (tmp_path / "group-policy" / "secret.md").write_text("not an agent")
        (tmp_path / "group-policy" / "secret").write_text("not an agent")
        before = sorted(p for p in tmp_path.rglob("*") if p.is_file())

        result = await EjectCommand(session).run(f"agents {name}")

        assert result.is_error()
        assert "not an entry name" in result.content
        assert sorted(p for p in tmp_path.rglob("*") if p.is_file()) == before


class TestWhatCanGoWrong:
    @pytest.mark.asyncio
    async def test_a_write_that_fails_is_an_error_and_claims_no_copy(
        self, tmp_path: Path, monkeypatch
    ):
        session = _session(tmp_path, {"agents": {"reviewer": "theirs"}})
        reloads: list[bool] = []
        session.reload_group_agents = lambda: reloads.append(True) or True

        def refuse(self, *args, **kwargs):
            raise PermissionError("read-only file system")

        monkeypatch.setattr(Path, "write_text", refuse)

        result = await EjectCommand(session).run("agents reviewer")

        assert result.is_error()
        assert "Could not write" in result.content
        assert "read-only file system" in result.content
        assert "Copied" not in result.content
        assert reloads == []

    @pytest.mark.asyncio
    async def test_a_kind_is_matched_in_any_case(self, tmp_path: Path):
        session = _session(tmp_path, {"output-styles": {"terse": "short"}})
        result = await EjectCommand(session).run("OUTPUT-Styles terse")
        assert not result.is_error()
        assert (session.project_dir / CONFIG_DIR / "output-styles" / "terse.md").is_file()

    @pytest.mark.asyncio
    async def test_a_shipped_directory_with_nothing_in_it_counts_as_shipping_nothing(
        self, tmp_path: Path
    ):
        session = _session(tmp_path, {"agents": {"reviewer": "x"}})
        (tmp_path / "group-policy" / "agents" / "reviewer.md").unlink()

        result = await EjectCommand(session).run("agents reviewer")

        assert result.is_error()
        assert "ships no agents. Nothing to eject." in result.content

    @pytest.mark.asyncio
    async def test_a_skill_is_listed_by_its_directory_name(self, tmp_path: Path):
        session = _session(tmp_path, {"skills": {"deploy": "body", "lint": "body"}})
        result = await EjectCommand(session).run("skills nope")
        assert "It ships: deploy, lint." in result.content

    @pytest.mark.asyncio
    async def test_a_rebuild_that_reports_nothing_asks_for_a_restart(self, tmp_path: Path):
        session = _session(tmp_path, {"agents": {"reviewer": "theirs"}})
        session.reload_group_agents = lambda: False

        result = await EjectCommand(session).run("agents reviewer")

        assert not result.is_error()
        assert "Restart the session" in result.content

    @pytest.mark.asyncio
    async def test_a_rebuild_that_works_does_not_ask_for_a_restart(self, tmp_path: Path):
        session = _session(tmp_path, {"agents": {"reviewer": "theirs"}})
        result = await EjectCommand(session).run("agents reviewer")
        assert "Restart" not in result.content

    @pytest.mark.asyncio
    async def test_unicode_survives_the_copy(self, tmp_path: Path):
        session = _session(tmp_path, {"agents": {"reviewer": "héllo — 日本語"}})
        await EjectCommand(session).run("agents reviewer")
        target = session.project_dir / CONFIG_DIR / "agents" / "reviewer.md"
        assert target.read_text(encoding="utf-8") == "héllo — 日本語"

    @pytest.mark.asyncio
    async def test_the_slash_shim_builds_the_command_from_the_handler(self, tmp_path: Path):
        from igni_core.backend.cmd_eject import cmd_eject

        session = _session(tmp_path, {"agents": {"reviewer": "theirs"}})
        result = await cmd_eject(SimpleNamespace(session=session), "agents reviewer")
        assert not result.is_error()
