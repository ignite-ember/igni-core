"""``EphemeralAgentStore``: what lands on disk, what is refused, and where.

An ephemeral agent is a ``.md`` file in ``<project>/.igni/agents.tmp`` that
is read back as an agent. The name and description come from the model, so
they must never reach outside that directory or break the file's header.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from igni_core.core.agents import AgentPool
from igni_core.core.agents.ephemeral import EphemeralAgentStore
from igni_core.core.agents.schemas import AgentPriority
from igni_core.core.paths import CONFIG_DIR


@pytest.fixture(autouse=True)
def _no_real_agent():
    with patch("igni_core.core.agents.builder.AgentBuilder.build") as build:
        build.return_value = MagicMock()
        yield build


@pytest.fixture
def pool(tmp_path, settings) -> AgentPool:
    p = AgentPool(settings=settings, project_dir=tmp_path)
    p.init_ephemeral(tmp_path)
    return p


def _tmp(project: Path) -> Path:
    return project / CONFIG_DIR / "agents.tmp"


def _register(pool: AgentPool, name: str = "helper", **fields):
    fields.setdefault("description", "Helps")
    fields.setdefault("system_prompt", "Help.")
    return pool.ephemeral.register(name=name, **fields)


class TestWhatIsWritten:
    def test_the_file_has_the_header_then_the_prompt(self, pool, tmp_path) -> None:
        _register(pool, tools=["Bash", "Edit"], model="m1", system_prompt="You help.")
        assert (_tmp(tmp_path) / "helper.md").read_text(encoding="utf-8") == (
            "---\nname: helper\ndescription: Helps\ntools: Bash, Edit\nmodel: m1\n---\nYou help.\n"
        )

    @pytest.mark.parametrize("tools", [None, []])
    def test_no_tools_falls_back_to_the_defaults(self, pool, tmp_path, tools) -> None:
        _register(pool, tools=tools)
        written = (_tmp(tmp_path) / "helper.md").read_text(encoding="utf-8")
        assert "tools: Write, Edit, Bash\n" in written
        assert "model:" not in written
        assert pool.get_entry("helper").definition.tools == ["Write", "Edit", "Bash"]

    def test_an_unknown_tool_is_refused_before_a_file_lands(self, pool, tmp_path) -> None:
        with pytest.raises(ValueError, match="NoSuchTool"):
            _register(pool, tools=["Bash", "NoSuchTool"])
        assert list(_tmp(tmp_path).iterdir()) == []
        assert pool.get_entry("helper") is None

    @pytest.mark.parametrize(
        "description",
        [
            "Reviews: code and docs",
            "line one\nline two",
            "---\nname: hijacked",
            "# not a comment",
            "'quoted' and \"double\"",
            "- a list?",
            "null",
            "",
        ],
    )
    def test_a_description_comes_back_as_it_was_given(self, pool, description) -> None:
        _register(pool, description=description)
        definition = pool.get_entry("helper").definition
        assert definition.description == description
        assert definition.name == "helper"
        assert definition.tools == ["Write", "Edit", "Bash"]

    @pytest.mark.parametrize("name", ["reviewer: v2", "#first", "-dash", "null", "has space"])
    def test_a_name_that_needs_quoting_comes_back_as_given(self, pool, name) -> None:
        _register(pool, name=name)
        assert pool.get_entry(name).definition.name == name

    def test_a_plain_description_is_written_plain(self, pool, tmp_path) -> None:
        _register(pool, description="Reviews the diff, then reports")
        written = (_tmp(tmp_path) / "helper.md").read_text(encoding="utf-8")
        assert "\ndescription: Reviews the diff, then reports\n" in written

    def test_a_model_named_with_a_colon_comes_back_as_given(self, pool) -> None:
        _register(pool, model="vendor: weird #1")
        assert pool.get_entry("helper").definition.model == "vendor: weird #1"


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "name",
        ["../escape", "a/b", "a\\b", "..", ".", "", "line\nbreak", "nul\x00byte"],
    )
    def test_a_name_that_is_not_a_plain_file_name_writes_nothing(
        self, pool, tmp_path, name
    ) -> None:
        with pytest.raises(ValueError, match="agent name"):
            _register(pool, name=name)
        assert list(tmp_path.rglob("*.md")) == []
        assert pool.ephemeral.count == 0

    def test_a_name_in_the_pool_already_is_refused(self, pool) -> None:
        _register(pool)
        with pytest.raises(ValueError, match="already exists"):
            _register(pool)

    def test_the_limit_is_a_boundary_and_zero_means_none(self, tmp_path, settings) -> None:
        p = AgentPool(settings=settings, project_dir=tmp_path)
        p.init_ephemeral(tmp_path, max_ephemeral=0)
        with pytest.raises(ValueError, match=r"limit reached \(0\)"):
            _register(p)

    def test_raising_the_limit_lets_another_in(self, pool) -> None:
        pool.ephemeral.max_ephemeral = 1
        _register(pool, "one")
        with pytest.raises(ValueError, match="limit reached"):
            _register(pool, "two")
        pool.ephemeral.max_ephemeral = 2
        _register(pool, "two")
        assert pool.ephemeral.max_ephemeral == 2
        assert pool.ephemeral.count == 2


class TestWhatLivesThere:
    def test_a_restart_finds_the_leftovers_at_ephemeral_priority(self, tmp_path, settings) -> None:
        _tmp(tmp_path).mkdir(parents=True)
        (_tmp(tmp_path) / "left.md").write_text(
            "---\nname: left\ndescription: from a crash\n---\nbody\n", encoding="utf-8"
        )
        p = AgentPool(settings=settings, project_dir=tmp_path)
        p.init_ephemeral(tmp_path)
        assert p.get_entry("left").priority is AgentPriority.EPHEMERAL
        assert p.ephemeral.count == 1
        assert [d.name for d in p.ephemeral.list_agents()] == ["left"]

    def test_owns_is_true_only_for_definitions_from_its_directory(self, pool, tmp_path) -> None:
        _register(pool)
        ours = pool.get_entry("helper").definition
        outside = ours.model_copy(update={"source_path": tmp_path / "elsewhere" / "helper.md"})
        nowhere = ours.model_copy(update={"source_path": None})
        assert pool.ephemeral.owns(ours) is True
        assert pool.ephemeral.owns(outside) is False
        assert pool.ephemeral.owns(nowhere) is False

    def test_count_and_list_see_only_the_ephemerals(self, pool, tmp_path) -> None:
        permanent = tmp_path / CONFIG_DIR / "agents"
        permanent.mkdir(parents=True, exist_ok=True)
        (permanent / "stay.md").write_text(
            "---\nname: stay\ndescription: d\n---\nb\n", encoding="utf-8"
        )
        pool.load_directory(permanent, AgentPriority.PROJECT_EMBER)
        _register(pool, "tmp1")
        assert pool.get_entry("stay") is not None
        assert pool.ephemeral.count == 1
        assert [d.name for d in pool.ephemeral.list_agents()] == ["tmp1"]


class TestPromoteDiscardCleanup:
    def test_an_unknown_promote_target_is_refused_and_the_agent_stays(self, pool, tmp_path) -> None:
        _register(pool)
        with pytest.raises(ValueError, match="Unknown promote target 'team'"):
            pool.ephemeral.promote("helper", tmp_path, target="team")
        assert (_tmp(tmp_path) / "helper.md").exists()
        assert pool.get_entry("helper").priority is AgentPriority.EPHEMERAL

    def test_promote_to_the_project_retags_and_moves(self, pool, tmp_path) -> None:
        _register(pool)
        dest = pool.ephemeral.promote("helper", tmp_path)
        assert dest == tmp_path / CONFIG_DIR / "agents" / "helper.md"
        assert dest.exists() and not (_tmp(tmp_path) / "helper.md").exists()
        entry = pool.get_entry("helper")
        assert entry.priority is AgentPriority.PROJECT_EMBER
        assert entry.definition.source_path == dest
        assert pool.ephemeral.count == 0

    def test_promote_to_personal_goes_to_the_local_directory(self, pool, tmp_path) -> None:
        _register(pool)
        dest = pool.ephemeral.promote("helper", tmp_path, target="personal")
        assert dest == tmp_path / CONFIG_DIR / "agents.local" / "helper.md"
        assert pool.get_entry("helper").priority is AgentPriority.PROJECT_LOCAL

    def test_promote_never_replaces_a_file_already_there(self, pool, tmp_path) -> None:
        _register(pool)
        existing = tmp_path / CONFIG_DIR / "agents" / "helper.md"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text("the team's", encoding="utf-8")
        with pytest.raises(ValueError, match="already exists"):
            pool.ephemeral.promote("helper", tmp_path)
        assert existing.read_text(encoding="utf-8") == "the team's"
        assert (_tmp(tmp_path) / "helper.md").exists()

    def test_a_permanent_agent_is_not_an_ephemeral_to_promote_or_discard(
        self, pool, tmp_path
    ) -> None:
        _register(pool)
        pool.ephemeral.promote("helper", tmp_path)
        with pytest.raises(ValueError, match="not an ephemeral"):
            pool.ephemeral.promote("helper", tmp_path)
        with pytest.raises(ValueError, match="not an ephemeral"):
            pool.ephemeral.discard("helper")
        assert pool.get_entry("helper") is not None

    def test_an_unknown_agent_is_named_in_the_refusal(self, pool, tmp_path) -> None:
        with pytest.raises(ValueError, match="'ghost' not found"):
            pool.ephemeral.discard("ghost")
        with pytest.raises(ValueError, match="'ghost' not found"):
            pool.ephemeral.promote("ghost", tmp_path)

    def test_discard_removes_the_file_and_the_entry(self, pool, tmp_path) -> None:
        _register(pool)
        pool.ephemeral.discard("helper")
        assert not (_tmp(tmp_path) / "helper.md").exists()
        assert pool.get_entry("helper") is None

    def test_discard_when_the_file_is_already_gone_still_forgets_the_agent(
        self, pool, tmp_path
    ) -> None:
        _register(pool)
        (_tmp(tmp_path) / "helper.md").unlink()
        pool.ephemeral.discard("helper")
        assert pool.get_entry("helper") is None

    def test_cleanup_removes_every_ephemeral_and_counts_them(self, pool, tmp_path) -> None:
        _register(pool, "one")
        _register(pool, "two")
        (_tmp(tmp_path) / "two.md").unlink()
        assert pool.ephemeral.cleanup() == 2
        assert list(_tmp(tmp_path).iterdir()) == []
        assert pool.ephemeral.count == 0

    def test_cleanup_with_none_is_zero(self, pool) -> None:
        assert pool.ephemeral.cleanup() == 0

    def test_cleanup_spares_the_permanent(self, pool, tmp_path) -> None:
        _register(pool, "keep")
        pool.ephemeral.promote("keep", tmp_path)
        _register(pool, "drop")
        assert pool.ephemeral.cleanup() == 1
        assert pool.get_entry("keep") is not None
        assert (tmp_path / CONFIG_DIR / "agents" / "keep.md").exists()


def test_the_defaults_are_the_tools_the_registry_still_has() -> None:
    assert EphemeralAgentStore.DEFAULT_TOOLS == ("Write", "Edit", "Bash")
    assert EphemeralAgentStore.DEFAULT_MAX_EPHEMERAL == 5


class _PublicPool:
    """A pool that offers the pool's public surface and nothing else, so any
    reach into a private attribute is an ``AttributeError``."""

    __slots__ = ("_real",)

    PUBLIC = frozenset(
        {
            "get_entry",
            "upsert_entry",
            "get",
            "iter_entries",
            "load_directory",
            "remove",
            "missing",
            "prompt_loader",
        }
    )

    def __init__(self, real: AgentPool) -> None:
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name: str):
        if name not in self.PUBLIC:
            raise AttributeError(f"{name} is not part of the pool's public surface")
        return getattr(object.__getattribute__(self, "_real"), name)


def test_the_store_works_through_the_pools_public_surface_alone(tmp_path, settings) -> None:
    real = AgentPool(settings=settings, project_dir=tmp_path)
    store = EphemeralAgentStore(tmp_path, _PublicPool(real))  # type: ignore[arg-type]
    store.init()
    store.register("helper", "Helps", "Help.")
    assert store.count == 1
    assert [d.name for d in store.list_agents()] == ["helper"]
    store.promote("helper", tmp_path, target="personal")
    assert store.count == 0
    store.register("again", "Helps", "Help.")
    store.discard("again")
    store.register("last", "Helps", "Help.")
    assert store.cleanup() == 1
