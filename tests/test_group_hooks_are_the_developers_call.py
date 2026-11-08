"""Whether the hooks a group ships run on a developer's machine is the developer's call.

F39. An organisation's group policy ships hooks, and igni runs them with
``bash -c`` as the developer, outside the agent's permission system:
``--strict`` denied the agent's shell and did nothing about them. The person a
developer needs protecting from there is whoever administers the
organisation, who controls the server, so the control has to be on the
developer's machine and theirs alone.

It is ``hooks.group: run | ask | refuse``:

* set only by ``~/.igni/config.yaml`` and the command line — a project, the
  group policy and the managed file are ignored (``DeveloperOnlyTier``);
* ``--strict`` means at least ``ask``; with nobody to answer, ``ask`` refuses;
* enforced in ``HookHandlerRegistry.run``, the one place a hook reaches its
  handler, and a dispatcher handed no gate runs no group hook;
* ``ask`` is put once per version of a hook, and asked again when it changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from igni_core.cli.options import CliOptions
from igni_core.core.config.cli_overrides import CliOverrides
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.schemas.orchestration import GroupHookMode
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.group_hook_decisions import GroupHookDecisions
from igni_core.core.hooks.group_hook_gate import APPROVAL_NAME, GroupHookGate
from igni_core.core.hooks.loader import HookLoader
from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.hooks.schemas import HookDefinition, HookLoadResult, MergeStrategy

SRC = Path(__file__).resolve().parents[1] / "src" / "igni_core"


# ── helpers ──────────────────────────────────────────────────────────


def _group_hook(marker: Path, *, text: str = "ran") -> HookDefinition:
    hook = HookDefinition(type="command", command=f"echo {text} >> {marker}", timeout=5000)
    return hook.mark_from_group("/data/group-policy/hooks/settings.json")


def _own_hook(marker: Path) -> HookDefinition:
    return HookDefinition(type="command", command=f"echo ran >> {marker}", timeout=5000)


class Asker:
    """The session's approval channel, answering ``answer`` and recording calls."""

    def __init__(self, answer: bool | None) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any], str, list[str]]] = []

    async def __call__(
        self, name: str, details: dict[str, Any], reason: str, path: list[str]
    ) -> bool | None:
        self.calls.append((name, details, reason, path))
        return self.answer


def _gate(tmp_path: Path, mode: GroupHookMode, asker: Asker | None = None) -> GroupHookGate:
    return GroupHookGate(
        mode=mode,
        ask=asker,
        decisions=GroupHookDecisions(tmp_path / "decisions.json"),
    )


def _fire(hooks: list[HookDefinition], gate: GroupHookGate | None) -> None:
    executor = HookExecutor({"SessionStart": hooks}, group_hook_gate=gate)
    asyncio.run(executor.execute("SessionStart", {"event": "SessionStart"}))


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Every process the hook handler starts, recorded and still started."""
    calls: list[tuple[Any, ...]] = []
    real = asyncio.create_subprocess_exec

    async def recording(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    return calls


def _pack(payload: dict[str, Any]) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g1",
        group_name="Platform",
        fetched_at=datetime.now(UTC),
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="policy",
                content=json.dumps(payload),
                content_type="json",
                enabled=True,
            )
        ],
    )


def _user_config(text: str) -> None:
    config = Path.home() / ".igni" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text, encoding="utf-8")


# ── the setting ──────────────────────────────────────────────────────


class TestTheSettingIsTheDevelopers:
    def test_default_is_run(self, tmp_path: Path):
        """Today's behaviour stays the default."""
        assert SettingsLoader.load(project_dir=tmp_path).hooks.group == "run"

    def test_the_users_own_config_sets_it(self, tmp_path: Path):
        _user_config("hooks:\n  group: refuse\n")

        assert SettingsLoader.load(project_dir=tmp_path).hooks.group == "refuse"

    def test_strict_means_ask(self, tmp_path: Path):
        overrides = CliOverrides.from_options(CliOptions(strict=True))

        assert overrides.hooks.group == "ask"
        assert SettingsLoader.load(overrides, project_dir=tmp_path).hooks.group == "ask"

    def test_strict_does_not_loosen_a_stricter_choice(self, tmp_path: Path):
        _user_config("hooks:\n  group: refuse\n")
        overrides = CliOverrides.from_options(CliOptions(strict=True))

        assert SettingsLoader.load(overrides, project_dir=tmp_path).hooks.group == "refuse"

    def test_a_group_policy_cannot_set_it(self, tmp_path: Path, caplog):
        _user_config("hooks:\n  group: ask\n")
        pack = _pack({"hooks": {"group": "run"}})

        with caplog.at_level(logging.WARNING):
            settings = SettingsLoader.load(project_dir=tmp_path, group_policy_fetcher=lambda: pack)

        assert settings.hooks.group == "ask"
        assert "hooks.group may be set only" in caplog.text

    def test_a_group_policy_cannot_override_strict(self, tmp_path: Path):
        overrides = CliOverrides.from_options(CliOptions(strict=True))
        pack = _pack({"hooks": {"group": "run"}})

        settings = SettingsLoader.load(
            overrides, project_dir=tmp_path, group_policy_fetcher=lambda: pack
        )

        assert settings.hooks.group == "ask"

    def test_a_project_cannot_set_it(self, tmp_path: Path):
        _user_config("hooks:\n  group: refuse\n")
        project = tmp_path / ".igni"
        project.mkdir()
        (project / "config.yaml").write_text("hooks:\n  group: run\n", encoding="utf-8")
        (project / "config.local.yaml").write_text("hooks:\n  group: run\n", encoding="utf-8")

        assert SettingsLoader.load(project_dir=tmp_path).hooks.group == "refuse"

    def test_the_managed_file_cannot_set_it(self, tmp_path: Path, monkeypatch):
        managed = tmp_path / "managed.yaml"
        managed.write_text("hooks:\n  group: run\n", encoding="utf-8")
        monkeypatch.setattr(
            "igni_core.core.config.managed_policy.ManagedPolicySource.platform_path",
            staticmethod(lambda: managed),
        )
        _user_config("hooks:\n  group: ask\n")

        assert SettingsLoader.load(project_dir=tmp_path).hooks.group == "ask"

    def test_a_typo_reads_as_ask_not_run(self, tmp_path: Path):
        _user_config("hooks:\n  group: refues\n")

        assert SettingsLoader.load(project_dir=tmp_path).hooks.group == "ask"


# ── which hooks are the group's ──────────────────────────────────────


class TestTheLoaderMarksTheGroupsHooks:
    def _write(self, path: Path, hook: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"Stop": [hook]}}), encoding="utf-8")

    def test_only_the_group_file_is_marked(self, tmp_path: Path):
        group = tmp_path / "group-policy" / "hooks"
        self._write(group / "settings.json", {"type": "command", "command": "echo group"})
        self._write(Path.home() / ".igni" / "settings.json", {"command": "echo mine"})

        hooks = HookLoader(tmp_path, group_dir=group).load().registry.for_event("Stop")

        by_command = {h.command: h for h in hooks}
        assert by_command["echo group"].from_group
        assert by_command["echo group"].group_source == str(group / "settings.json")
        assert not by_command["echo mine"].from_group

    def test_a_settings_file_cannot_disclaim_it(self, tmp_path: Path):
        """The mark is private: a group file saying otherwise changes nothing,
        and a user file cannot claim it either."""
        group = tmp_path / "group-policy" / "hooks"
        self._write(
            group / "settings.json",
            {"command": "echo group", "_group_source": "", "from_group": False},
        )

        (hook,) = HookLoader(tmp_path, group_dir=group).load().registry.for_event("Stop")

        assert hook.from_group

    def test_a_group_installed_plugins_hooks_are_the_groups(self, tmp_path: Path):
        plugin = tmp_path / "plugin"
        (plugin / "hooks").mkdir(parents=True)
        (plugin / "hooks" / "hooks.json").write_text(
            json.dumps({"Stop": [{"command": "echo plugin"}]}), encoding="utf-8"
        )
        loader = HookLoader(tmp_path)

        group_registry = HookRegistry.from_empty()
        loader.load_plugin_hooks(plugin, group_registry, from_group=True)
        own_registry = HookRegistry.from_empty()
        loader.load_plugin_hooks(plugin, own_registry)

        assert group_registry.for_event("Stop")[0].from_group
        assert not own_registry.for_event("Stop")[0].from_group

    def test_the_plugin_loader_passes_the_group_root(self, tmp_path: Path):
        from igni_core.core.plugins.loader import GROUP_POLICY_ROOT, PluginLoader

        seen: dict[str, bool] = {}

        class Recorder:
            def load_plugin_hooks(self, root: Path, registry: Any, *, from_group: bool = False):
                seen[root.name] = from_group
                return HookLoadResult(registry=registry, warnings=[])

        class Plugin:
            def __init__(self, name: str, root: str) -> None:
                self.name, self.has_hooks = name, True
                self.root_path = tmp_path / name
                self.source = type("S", (), {"root": root})()

        loader = PluginLoader.__new__(PluginLoader)
        loader._plugins = {
            "org": Plugin("org", GROUP_POLICY_ROOT),
            "mine": Plugin("mine", "user-igni"),
        }
        loader.apply_to_hooks(Recorder(), HookRegistry.from_empty())  # type: ignore[arg-type]

        assert seen == {"org": True, "mine": False}

    def test_merge_from_dict_marks_only_when_told(self, tmp_path: Path):
        registry = HookRegistry.from_empty()
        registry.merge_from_dict(
            {"Stop": [{"command": "a"}]}, source=tmp_path, strategy=MergeStrategy.APPEND
        )
        registry.merge_from_dict(
            {"Stop": [{"command": "b"}]},
            source=tmp_path,
            strategy=MergeStrategy.APPEND,
            from_group=True,
        )

        assert [h.from_group for h in registry.for_event("Stop")] == [False, True]


# ── what happens at dispatch ─────────────────────────────────────────


class TestRun:
    def test_the_default_runs_a_group_hook(self, tmp_path: Path):
        marker = tmp_path / "ran"

        _fire([_group_hook(marker)], _gate(tmp_path, "run"))

        assert marker.read_text().strip() == "ran"


class TestRefuse:
    def test_refuse_never_spawns_a_process(self, tmp_path: Path, spawned):
        marker = tmp_path / "ran"

        _fire([_group_hook(marker)], _gate(tmp_path, "refuse"))

        assert spawned == []
        assert not marker.exists()

    def test_the_developers_own_hooks_still_run(self, tmp_path: Path, spawned):
        mine, theirs = tmp_path / "mine", tmp_path / "theirs"

        _fire([_own_hook(mine), _group_hook(theirs)], _gate(tmp_path, "refuse"))

        assert mine.exists()
        assert not theirs.exists()
        assert len(spawned) == 1

    def test_a_refused_hook_does_not_block(self, tmp_path: Path):
        """Skipped like a hook that failed — it neither runs nor decides."""
        hook = HookDefinition(type="command", command="exit 2").mark_from_group("g")
        executor = HookExecutor({"PreToolUse": [hook]}, group_hook_gate=_gate(tmp_path, "refuse"))

        result = asyncio.run(
            executor.execute("PreToolUse", {"event": "PreToolUse", "tool_name": "x"})
        )

        assert result.should_continue

    def test_every_hook_type_is_gated(self, tmp_path: Path):
        """``http`` and ``mcp_tool`` reach outside too; a prompt hook speaks
        into the conversation. None of them is the developer's."""
        gate = _gate(tmp_path, "refuse")
        for hook in (
            HookDefinition(type="http", url="http://127.0.0.1:9/x"),
            HookDefinition(type="prompt", text="hi"),
            HookDefinition(type="mcp_tool", mcp_server="s", mcp_tool="t"),
        ):
            assert not asyncio.run(gate.permits(hook.mark_from_group("g"), "Stop"))


class TestAsk:
    def test_ask_shows_the_command_and_source_and_runs_on_yes(self, tmp_path: Path):
        marker = tmp_path / "ran"
        asker = Asker(True)
        hook = _group_hook(marker)

        _fire([hook], _gate(tmp_path, "ask", asker))

        assert marker.exists()
        ((name, details, reason, _),) = asker.calls
        assert name == APPROVAL_NAME
        assert details["hook"] == hook.command
        assert details["source"] == "/data/group-policy/hooks/settings.json"
        assert details["event"] == "SessionStart"
        assert "outside the agent's permissions" in reason

    def test_no_means_it_does_not_run(self, tmp_path: Path, spawned):
        marker = tmp_path / "ran"

        _fire([_group_hook(marker)], _gate(tmp_path, "ask", Asker(False)))

        assert spawned == []
        assert not marker.exists()

    def test_asked_once_per_hook(self, tmp_path: Path):
        marker = tmp_path / "ran"
        asker = Asker(True)
        gate = _gate(tmp_path, "ask", asker)

        _fire([_group_hook(marker)], gate)
        _fire([_group_hook(marker)], gate)

        assert len(asker.calls) == 1
        assert marker.read_text().split() == ["ran", "ran"]

    def test_the_answer_outlives_the_session(self, tmp_path: Path):
        marker = tmp_path / "ran"
        _fire([_group_hook(marker)], _gate(tmp_path, "ask", Asker(False)))

        later = Asker(True)
        _fire([_group_hook(marker)], _gate(tmp_path, "ask", later))

        assert later.calls == []
        assert not marker.exists()

    def test_a_changed_hook_is_asked_about_again(self, tmp_path: Path):
        marker = tmp_path / "ran"
        asker = Asker(True)
        gate = _gate(tmp_path, "ask", asker)

        _fire([_group_hook(marker, text="v1")], gate)
        asker.answer = False
        _fire([_group_hook(marker, text="v2")], gate)

        assert [c[1]["hook"] for c in asker.calls] == [
            f"echo v1 >> {marker}",
            f"echo v2 >> {marker}",
        ]
        assert marker.read_text().split() == ["v1"]

    def test_two_dispatches_at_once_ask_once(self, tmp_path: Path):
        asker = Asker(True)
        gate = _gate(tmp_path, "ask", asker)
        hook = HookDefinition(type="prompt", text="hi").mark_from_group("g")

        async def both() -> list[bool]:
            return list(
                await asyncio.gather(gate.permits(hook, "Stop"), gate.permits(hook, "Stop"))
            )

        assert asyncio.run(both()) == [True, True]
        assert len(asker.calls) == 1


class TestTheDecisionsFile:
    def test_an_answer_is_kept_with_what_the_hook_did(self, tmp_path: Path):
        path = tmp_path / "decisions.json"
        hook = HookDefinition(type="http", url="http://x").mark_from_group("g")

        GroupHookDecisions(path).set(hook.digest(), "refuse", hook)

        assert GroupHookDecisions(path).get(hook.digest()) == "refuse"
        record = json.loads(path.read_text(encoding="utf-8"))["hooks"][hook.digest()]
        assert record["what"] == "POST http://x"
        assert record["source"] == "g"

    def test_an_unreadable_file_asks_again(self, tmp_path: Path):
        path = tmp_path / "decisions.json"
        path.write_text('{"hooks": {"abc": {"decision": "maybe"}}}', encoding="utf-8")

        assert GroupHookDecisions(path).get("abc") is None

    def test_one_bad_entry_costs_only_its_own_answer(self, tmp_path: Path):
        path = tmp_path / "decisions.json"
        kept = {
            "decision": "refuse",
            "what": "x",
            "source": "g",
            "decided_at": "2026-01-01T00:00:00Z",
        }
        path.write_text(
            json.dumps({"version": 1, "hooks": {"A": kept, "B": {"decision": "maybe"}}}),
            encoding="utf-8",
        )
        hook = HookDefinition(type="prompt", text="hi").mark_from_group("g")

        assert GroupHookDecisions(path).get("A") == "refuse"
        assert GroupHookDecisions(path).get("B") is None

        GroupHookDecisions(path).set(hook.digest(), "run", hook)

        hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
        assert hooks["A"] == kept
        assert hooks["B"] == {"decision": "maybe"}
        assert GroupHookDecisions(path).get("A") == "refuse"
        assert GroupHookDecisions(path).get(hook.digest()) == "run"


class TestAskWithNobodyThere:
    def test_a_headless_run_refuses(self, tmp_path: Path, spawned):
        """The channel says ``None`` when no client is listening."""
        marker = tmp_path / "ran"

        _fire([_group_hook(marker)], _gate(tmp_path, "ask", Asker(None)))

        assert spawned == []

    def test_no_channel_at_all_refuses(self, tmp_path: Path, spawned):
        _fire([_group_hook(tmp_path / "ran")], _gate(tmp_path, "ask", None))

        assert spawned == []

    def test_an_unanswered_refusal_is_not_remembered(self, tmp_path: Path):
        """Nobody said no, so the next run with someone there asks."""
        marker = tmp_path / "ran"
        _fire([_group_hook(marker)], _gate(tmp_path, "ask", Asker(None)))

        later = Asker(True)
        _fire([_group_hook(marker)], _gate(tmp_path, "ask", later))

        assert len(later.calls) == 1
        assert marker.exists()

    def test_a_failing_channel_refuses(self, tmp_path: Path, spawned):
        async def broken(*_: Any) -> bool | None:
            raise RuntimeError("relay gone")

        gate = GroupHookGate(
            mode="ask", ask=broken, decisions=GroupHookDecisions(tmp_path / "d.json")
        )
        _fire([_group_hook(tmp_path / "ran")], gate)

        assert spawned == []


# ── no path around it ────────────────────────────────────────────────


class TestNoPathAroundTheGate:
    def test_a_dispatcher_with_no_gate_runs_no_group_hook(self, tmp_path: Path, spawned):
        mine, theirs = tmp_path / "mine", tmp_path / "theirs"

        _fire([_own_hook(mine), _group_hook(theirs)], None)

        assert mine.exists()
        assert not theirs.exists()

    def test_only_the_handler_registry_calls_a_handler(self):
        """Every ``handler.run(hook, …)`` in the source is the one in
        ``HookHandlerRegistry.run``, after the gate. A second call site would
        be a path a group hook could take without being checked."""
        # ``self._handler_registry.run(hook, …)`` is the executor going
        # through the registry, which is the point; anything else is not.
        pattern = re.compile(r"(?<!_handler_registry)\.run\(\s*hook\b")
        sites = [
            str(path.relative_to(SRC))
            for path in SRC.rglob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        ]

        assert sites == ["core/hooks/handlers/registry.py"]

    def test_the_session_hands_its_executors_a_gate(self):
        """:class:`SessionHooks` is the one place a session builds an executor."""
        source = (SRC / "core" / "session" / "hooks.py").read_text(encoding="utf-8")
        constructions = source.count("HookExecutor(")

        assert constructions >= 1
        assert source.count("group_hook_gate=self._gate") == constructions
        session_source = (SRC / "core" / "session" / "core.py").read_text(encoding="utf-8")
        assert "HookExecutor(" not in session_source

    def test_the_sessions_gate_reads_the_live_setting(self, tmp_path: Path):
        from igni_core.core.session.hooks import SessionHooks
        from igni_core.core.session.mcp_resolver import MCPToolResolver
        from igni_core.core.session.reminders import PendingReminderQueue

        async def nobody(*_: Any, **__: Any) -> bool | None:
            return None

        settings = SettingsLoader.load()
        hooks = SessionHooks(
            project_dir=tmp_path,
            settings=settings,
            group_hooks_dir=tmp_path / "group-hooks",
            reminders=PendingReminderQueue(),
            resolver=MCPToolResolver(lambda: None),
            ask=nobody,
        )
        gate = hooks.gate
        assert gate.mode() == "run"

        settings.hooks.group = "refuse"

        assert gate.mode() == "refuse"
        hooks.reload()
        assert hooks.gate is gate


class TestStrictSaysSo:
    def test_the_help_names_the_group_hooks(self):
        from igni_core.cli import cli

        strict = next(p for p in cli.params if p.name == "strict")
        help_text = " ".join((strict.help or "").split())

        assert "hooks.group: ask" in help_text
        assert "group policy" in help_text
