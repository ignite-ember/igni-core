"""The panel controllers project what a session holds into what the window shows.

Agents, hooks, skills, output styles and the slash-command catalog each
read the session and hand back typed records. What is pinned here is the
projection: the field defaults, the order, the empty cases, and the
catalog's four sources, in order, with a failing source emptied rather
than raised.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from igni_core.backend import panels
from igni_core.backend.command_handler import CommandHandler
from igni_core.backend.panels import (
    AgentsPanelController,
    HooksPanelController,
    OutputStylesCatalog,
    SkillsPanelController,
    SlashCommandsCatalog,
)
from igni_core.core.agents import AgentPool
from igni_core.core.agents.schemas import AgentDefinition
from igni_core.core.agents.tool_check import UnresolvedAgentTool, UnresolvedAgentTools
from igni_core.core.contributed_commands import ContributedCommands
from igni_core.core.hooks.schemas import HookDefinition
from igni_core.core.output_styles.loader import OutputStyle
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.session.output_style_selection import OutputStyleSelection
from igni_core.core.skills import SkillPool


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def _no_unknown() -> UnresolvedAgentTools:
    return UnresolvedAgentTools()


@pytest.fixture
def pool(tmp_path: Path, settings):
    with patch("igni_core.core.agents.builder.AgentBuilder.build", return_value=MagicMock()):
        yield AgentPool(settings=settings, project_dir=tmp_path)


# ── Package surface ───────────────────────────────────────────


def test_the_package_exports_each_controller():
    assert sorted(panels.__all__) == [
        "AgentsPanelController",
        "HooksPanelController",
        "OutputStylesCatalog",
        "SkillsPanelController",
        "SlashCommandsCatalog",
    ]
    for name in panels.__all__:
        assert getattr(panels, name).__module__.startswith("igni_core.backend.panels.")


# ── Agents ────────────────────────────────────────────────────


def _agents_session(pool, unknown=None, tmp_path=Path(".")):
    return SimpleNamespace(
        pool=pool,
        project_dir=tmp_path,
        agent_tool_check=unknown or _no_unknown(),
    )


def test_an_empty_pool_snapshots_to_nothing(pool):
    assert AgentsPanelController(_agents_session(pool)).snapshot() == []  # type: ignore[arg-type]


def test_an_agent_without_model_color_or_path_shows_empty_strings(pool):
    pool.list_agents = lambda: [  # type: ignore[method-assign]
        AgentDefinition(name="bare", description="d", tools=["t"], tags=["x"], system_prompt="P")
    ]
    [info] = AgentsPanelController(_agents_session(pool)).snapshot()  # type: ignore[arg-type]
    assert (info.model, info.color, info.source_path) == ("", "", "")
    assert info.tools == ["t"]
    assert info.tags == ["x"]
    assert info.system_prompt == "P"
    assert info.is_ephemeral is False
    assert info.unknown_tools == []


def test_an_agent_with_model_color_and_path_shows_them_and_its_unknown_tools(pool, tmp_path):
    src = tmp_path / "a.md"
    pool.list_agents = lambda: [  # type: ignore[method-assign]
        AgentDefinition(
            name="a",
            description="d",
            model="m1",
            color="red",
            mcp_servers=["srv"],
            can_orchestrate=False,
            source_path=src,
        ),
        AgentDefinition(name="b", description="d"),
    ]
    check = UnresolvedAgentTools(entries=[UnresolvedAgentTool(agent="a", tools=["nope"])])
    infos = AgentsPanelController(_agents_session(pool, check)).snapshot()  # type: ignore[arg-type]
    assert [i.name for i in infos] == ["a", "b"]
    a, b = infos
    assert (a.model, a.color, a.source_path) == ("m1", "red", str(src))
    assert a.mcp_servers == ["srv"]
    assert a.can_orchestrate is False
    assert a.unknown_tools == ["nope"]
    assert b.unknown_tools == []


def test_only_agents_the_ephemeral_store_owns_are_flagged(pool, tmp_path):
    pool.init_ephemeral(tmp_path)
    pool.ephemeral.register(name="scratch", description="d", system_prompt="p")
    perm = AgentDefinition(name="perm", description="d")
    pool.list_agents = lambda: [  # type: ignore[method-assign]
        pool.get_entry("scratch").definition,
        perm,
    ]
    infos = AgentsPanelController(_agents_session(pool)).snapshot()  # type: ignore[arg-type]
    assert {i.name: i.is_ephemeral for i in infos} == {"scratch": True, "perm": False}


def test_discarding_an_ephemeral_agent_says_ok(pool, tmp_path):
    pool.init_ephemeral(tmp_path)
    pool.ephemeral.register(name="scratch", description="d", system_prompt="p")
    result = AgentsPanelController(_agents_session(pool)).discard("scratch")  # type: ignore[arg-type]
    assert (result.ok, result.name, result.reason) == (True, "scratch", "")
    assert not pool.ephemeral.owns(AgentDefinition(name="scratch", description="d"))


def test_discarding_an_unknown_agent_is_a_refusal_not_an_exception(pool, tmp_path):
    pool.init_ephemeral(tmp_path)
    result = AgentsPanelController(_agents_session(pool)).discard("ghost")  # type: ignore[arg-type]
    assert result.ok is False
    assert result.name == "ghost"
    assert result.reason


def test_discarding_without_an_ephemeral_store_is_a_refusal(pool):
    result = AgentsPanelController(_agents_session(pool)).discard("x")  # type: ignore[arg-type]
    assert result.ok is False
    assert "not initialized" in result.reason


def test_promoting_without_an_ephemeral_store_is_a_refusal(pool):
    result = AgentsPanelController(_agents_session(pool)).promote("x")  # type: ignore[arg-type]
    assert result.ok is False
    assert result.dest == ""


def test_an_unexpected_error_in_discard_is_not_swallowed(pool):
    store = MagicMock()
    store.discard.side_effect = OSError("disk")
    pool._ephemeral = store
    with pytest.raises(OSError):
        AgentsPanelController(_agents_session(pool)).discard("x")  # type: ignore[arg-type]


# ── Hooks ─────────────────────────────────────────────────────


def test_hooks_snapshot_lists_every_hook_in_map_order():
    session = SimpleNamespace(
        hooks_map={
            "PreToolUse": [
                HookDefinition(type="command", command="a.sh", matcher="Bash", timeout=500),
                HookDefinition(type="http", url="https://u:p@h/x", headers={"B": "1", "A": "2"}),
            ],
            "Stop": [],
            "SessionStart": [HookDefinition(type="command", command="c.sh", background=True)],
        }
    )
    views = HooksPanelController(session).snapshot()  # type: ignore[arg-type]
    assert [(v.event, v.type) for v in views] == [
        ("PreToolUse", "command"),
        ("PreToolUse", "http"),
        ("SessionStart", "command"),
    ]
    assert (views[0].command, views[0].matcher, views[0].timeout_ms) == ("a.sh", "Bash", 500)
    assert views[1].url == "https://h/x"
    assert views[1].header_names == ["A", "B"]
    assert views[2].background is True


def test_hooks_snapshot_of_an_empty_map_is_empty():
    assert HooksPanelController(SimpleNamespace(hooks_map={})).snapshot() == []  # type: ignore[arg-type]


@pytest.mark.parametrize("count", [0, 3])
def test_reloading_hooks_toasts_the_active_count(count):
    session = SimpleNamespace(reload_hooks=lambda: count)
    info = HooksPanelController(session).reload()  # type: ignore[arg-type]
    assert info.text == f"Reloaded hooks — {count} active hook(s) across all events."


# ── Output styles ─────────────────────────────────────────────


def _style(name: str, description: str = "") -> OutputStyle:
    return OutputStyle(name=name, path=Path(f"{name}.md"), description=description, body="b")


def test_styles_are_sorted_by_name_whatever_the_discovery_order():
    selection = OutputStyleSelection(
        {n: _style(n, f"about {n}") for n in ["zeta", "alpha", "Mid", "ünï"]}, active="alpha"
    )
    result = OutputStylesCatalog(SimpleNamespace(output_style=selection)).snapshot()  # type: ignore[arg-type]
    assert [s.name for s in result.styles] == ["Mid", "alpha", "zeta", "ünï"]
    assert result.styles[1].description == "about alpha"
    assert result.active == "alpha"


def test_no_styles_and_nothing_active_is_empty_not_none():
    result = OutputStylesCatalog(SimpleNamespace(output_style=OutputStyleSelection())).snapshot()  # type: ignore[arg-type]
    assert result.styles == []
    assert result.active == ""


# ── Skills ────────────────────────────────────────────────────


def _skill(root: Path, name: str, extra: str = "", body: str = "Do it.\n") -> None:
    _write(
        root / name / "SKILL.md",
        f"---\nname: {name}\ndescription: about {name}\n{extra}---\n{body}",
    )


@pytest.fixture
def skills(tmp_path: Path):
    root = tmp_path / "skills"
    _skill(root, "plain")
    _skill(root, "hidden", "user-invocable: false\n")
    _skill(root, "agented", "agent: reviewer\nargument-hint: '<x>'\n")
    pool = SkillPool()
    pool.load_directory(root)
    return pool, root


def test_skills_snapshot_maps_every_field_and_defaults_absent_ones_to_empty(skills):
    pool, root = skills
    infos = {i.name: i for i in SkillsPanelController(SimpleNamespace(skill_pool=pool)).snapshot()}  # type: ignore[arg-type]
    assert set(infos) == {"plain", "hidden", "agented"}
    plain = infos["plain"]
    assert (plain.agent, plain.description, plain.body.strip()) == ("", "about plain", "Do it.")
    assert plain.source_dir == str(root / "plain")
    assert infos["hidden"].user_invocable is False
    assert infos["agented"].agent == "reviewer"
    assert infos["agented"].argument_hint == "<x>"


def test_skills_names_and_pool_serve_autocomplete(skills):
    pool, _ = skills
    panel = SkillsPanelController(SimpleNamespace(skill_pool=pool))  # type: ignore[arg-type]
    assert sorted(panel.names()) == ["agented", "hidden", "plain"]
    assert panel.pool() is pool


def test_an_empty_skill_pool_has_no_snapshot_and_no_names():
    panel = SkillsPanelController(SimpleNamespace(skill_pool=SkillPool()))  # type: ignore[arg-type]
    assert panel.snapshot() == []
    assert panel.names() == []


# ── Slash-command catalog ─────────────────────────────────────


@pytest.fixture
def catalog_session(tmp_path: Path, monkeypatch, skills):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    pool, _ = skills
    return SimpleNamespace(
        project_dir=tmp_path,
        settings=SimpleNamespace(rules=SimpleNamespace(cross_tool_support=False)),
        skill_pool=pool,
    )


def _invoke(*_a, **_k):  # pragma: no cover - never called
    raise AssertionError


def test_the_catalog_lists_builtin_then_markdown_then_skill_then_extension(
    catalog_session, tmp_path
):
    _write(tmp_path / CONFIG_DIR / "commands" / "mdone.md", "Body\n")
    commands = ContributedCommands()
    commands.register("compliance", owner="acme.scan", description="scan", invoke=_invoke)

    entries = SlashCommandsCatalog(catalog_session, commands).entries()  # type: ignore[arg-type]

    sources = [e.source for e in entries]
    assert sources == sorted(sources, key=["builtin", "markdown", "skill", "extension"].index)
    builtin = [e for e in entries if e.source == "builtin"]
    assert [e.name for e in builtin] == CommandHandler.builtin_names()
    assert builtin[0].description == CommandHandler.describe(builtin[0].name)
    assert [e.name for e in entries if e.source == "markdown"] == ["mdone"]
    # a skill that is not user-invocable is not offered as a command
    assert sorted(e.name for e in entries if e.source == "skill") == ["agented", "plain"]
    [ext] = [e for e in entries if e.source == "extension"]
    assert (ext.name, ext.owner, ext.description) == ("compliance", "acme.scan", "scan")


def test_a_command_the_registry_dropped_leaves_the_catalog(catalog_session):
    commands = ContributedCommands()
    commands.register("compliance", owner="acme.scan", description="scan", invoke=_invoke)
    catalog = SlashCommandsCatalog(catalog_session, commands)  # type: ignore[arg-type]
    assert [e.name for e in catalog.entries() if e.source == "extension"] == ["compliance"]
    commands.remove_owner("acme.scan")
    assert [e for e in catalog.entries() if e.source == "extension"] == []


def test_the_default_catalog_reads_the_process_wide_registry(catalog_session):
    from igni_core.core.contributed_commands import contributed_commands

    assert SlashCommandsCatalog(catalog_session)._commands is contributed_commands  # type: ignore[arg-type]


def test_claude_commands_are_listed_only_when_cross_tool_support_is_on(catalog_session, tmp_path):
    _write(tmp_path / "home" / ".claude" / "commands" / "claude_only.md", "Body\n")
    catalog = SlashCommandsCatalog(catalog_session, ContributedCommands())  # type: ignore[arg-type]
    assert "claude_only" not in [e.name for e in catalog.entries() if e.source == "markdown"]
    catalog_session.settings.rules.cross_tool_support = True
    assert "claude_only" in [e.name for e in catalog.entries() if e.source == "markdown"]


def test_a_failing_markdown_discovery_empties_that_source_only(catalog_session, caplog):
    catalog_session.settings = None  # reading the setting raises
    with caplog.at_level("DEBUG"):
        entries = SlashCommandsCatalog(catalog_session, ContributedCommands()).entries()  # type: ignore[arg-type]
    assert [e for e in entries if e.source == "markdown"] == []
    assert [e for e in entries if e.source == "builtin"]
    assert [e for e in entries if e.source == "skill"]
    assert "markdown discovery failed" in caplog.text


def test_a_failing_skill_pool_empties_that_source_only(catalog_session, caplog):
    catalog_session.skill_pool = SimpleNamespace(
        list_skills=MagicMock(side_effect=RuntimeError("boom"))
    )
    with caplog.at_level("DEBUG"):
        entries = SlashCommandsCatalog(catalog_session, ContributedCommands()).entries()  # type: ignore[arg-type]
    assert [e for e in entries if e.source == "skill"] == []
    assert [e for e in entries if e.source == "builtin"]
    assert "skill enumeration failed" in caplog.text


def test_a_selection_with_no_active_style_shows_an_empty_name():
    selection = SimpleNamespace(styles={"a": _style("a")}, active=None)
    result = OutputStylesCatalog(SimpleNamespace(output_style=selection)).snapshot()  # type: ignore[arg-type]
    assert result.active == ""
    assert [s.name for s in result.styles] == ["a"]
