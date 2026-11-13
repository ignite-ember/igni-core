"""What an extension's contributions do at their edges, in the tables they were given.

:mod:`igni_core.extensions.contributions` folds a READY extension's
commands, anchors, prompt overrides and edits into the tables it is
handed — one set per deployment, not a process-wide one — and takes
them out again. These tests wire it to local tables and a fake RPC, and
read what the agent would see from the tables.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.contributed_commands import ContributedCommands
from igni_core.core.contributed_commands import contributed_commands as process_wide_commands
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.fragments import PromptFragments
from igni_core.core.prompts.loader import PromptLoader
from igni_core.extensions import contributions as contributions_module
from igni_core.extensions.contributions import (
    ConditionsNotice,
    ExtensionConditionReceiver,
    ExtensionContributions,
)
from igni_core.extensions.host.hooks import ExtensionHooks
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState


class FakeRpc:
    """The extension RPC: answers a tool call with what it was given, and a command too."""

    def __init__(self, answer: Any = "tool text", *, hang: bool = False) -> None:
        self.answer = answer
        self.hang = hang
        self.methods: list[str] = []
        self.commands: list[tuple[str, str, str]] = []

    async def dispatch(self, *, caller: str, method: str, args: dict[str, Any]) -> Any:
        self.methods.append(method)
        if self.hang:
            await asyncio.sleep(3600)
        return self.answer

    async def invoke_command(self, extension_id: str, command: str, text: str) -> Any:
        self.commands.append((extension_id, command, text))
        return self.answer


def _manifest(extension_id: str = "acme.scanner", **contributes: Any) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["true"]},
            "contributes": contributes,
        }
    )


class Rig:
    def __init__(
        self,
        rpc: Any = None,
        *,
        bundles: dict[str, Path] | None = None,
        plugins: Path | None = None,
    ) -> None:
        self.registry = ExtensionRegistry()
        self.fragments = PromptFragments()
        self.prompts = PromptContributions()
        self.commands = ContributedCommands()
        self.hooks = ExtensionHooks(registry=self.registry)
        self.priority: list[str] = []
        self.rpc = rpc if rpc is not None else FakeRpc()
        self.contributions = ExtensionContributions(
            registry=self.registry,
            rpc=self.rpc,
            bundles=bundles,
            plugins_dir=plugins,
            priority=lambda: self.priority,
            prompt_loader=PromptLoader(self.fragments),
            prompts=self.prompts,
            commands=self.commands,
            hooks=self.hooks,
        )


def _anchor(source: str, origin: str | None = "x") -> dict[str, Any]:
    fragment: dict[str, Any] = {"anchor": "ACME_NOTE", "source": source}
    if origin is not None:
        fragment["from"] = origin
    return fragment


# ── The tables are the ones it was given ───────────────────────────


def test_a_commands_table_handed_in_is_the_one_that_gets_the_command():
    rig = Rig()
    manifest = _manifest(commands=[{"command": "/scan", "desc": "Scan it"}])
    rig.registry.install(manifest, state=ExtensionState.PREPARING)
    rig.contributions.attach()
    rig.registry.install(manifest, state=ExtensionState.READY)

    # No event loop here: only the commands are applied, and they land in the table given.
    command = rig.commands.get("scan")
    assert command is not None and command.owner == "acme.scanner"
    assert command.description == "Scan it"
    assert process_wide_commands.get("scan") is None


def test_with_no_event_loop_only_commands_are_applied_and_tool_text_is_skipped():
    rig = Rig()
    manifest = _manifest(
        commands=[{"command": "/scan", "desc": "d"}], promptFragments=[_anchor("tool", "note")]
    )
    rig.contributions.attach()
    rig.registry.install(manifest, state=ExtensionState.READY)
    assert rig.commands.get("scan") is not None
    assert rig.fragments.owner_of("ACME_NOTE") is None
    assert rig.rpc.methods == []


async def test_a_contributed_command_runs_through_the_rpc_and_answers_in_text():
    rpc = FakeRpc(answer=42)
    rig = Rig(rpc)
    await rig.contributions.apply(_manifest(commands=[{"command": "/scan", "desc": "d"}]))
    command = rig.commands.get("/scan")
    assert command is not None
    assert await command.invoke("now") == "42"
    assert rpc.commands == [("acme.scanner", "scan", "now")]


async def test_a_command_igni_or_another_extension_holds_is_not_taken_over():
    rig = Rig()
    rig.commands.register("scan", owner="other.ext", description="theirs", invoke=None)  # type: ignore[arg-type]
    await rig.contributions.apply(_manifest(commands=[{"command": "/scan", "desc": "mine"}]))
    held = rig.commands.get("scan")
    assert held is not None and held.owner == "other.ext"


def test_resolving_tells_the_injected_prompts_and_hooks_the_priority():
    rig = Rig()
    rig.registry.install(_manifest("a.one"), state=ExtensionState.READY)
    rig.registry.install(_manifest("b.two"), state=ExtensionState.READY)
    rig.priority = ["b.two", "a.one"]
    rig.contributions.resolve()
    assert rig.prompts._priority == ["b.two", "a.one"]
    assert rig.hooks._priority == ["b.two", "a.one"]


async def test_a_priority_change_refolds_only_when_who_gets_what_moved():
    rig = Rig()
    manifest_a = _manifest("a.one", promptFragments=[_anchor("file", "n.md")])
    manifest_b = _manifest("b.two", promptFragments=[_anchor("file", "n.md")])
    rig.contributions._bundles = {}
    rig.registry.install(manifest_a, state=ExtensionState.READY)
    rig.registry.install(manifest_b, state=ExtensionState.READY)
    rig.priority = ["a.one", "b.two"]
    rig.contributions.priority_changed()
    await asyncio.sleep(0)
    await asyncio.gather(*rig.contributions._pending)
    folded: list[str] = []
    rig.contributions._schedule = lambda entry: folded.append(entry.id)  # type: ignore[method-assign]

    rig.contributions.priority_changed()
    assert folded == [], "the same order moves nothing"

    rig.priority = ["b.two", "a.one"]
    rig.contributions.priority_changed()
    assert sorted(folded) == ["a.one", "b.two"]
    assert [o.reason for o in rig.registry.get("a.one").switched_off][0].startswith(
        "lower priority than b.two"
    )


# ── Where the text comes from ──────────────────────────────────────


async def test_a_tool_that_never_answers_is_given_up_on_after_the_deadline(monkeypatch):
    monkeypatch.setattr(contributions_module, "FRAGMENT_TIMEOUT_S", 0.05)
    rig = Rig(FakeRpc(hang=True))
    await asyncio.wait_for(
        rig.contributions.apply(_manifest(promptFragments=[_anchor("tool", "slow")])), timeout=5
    )
    assert rig.rpc.methods == ["ext.acme.scanner.slow"]
    assert rig.fragments.owner_of("ACME_NOTE") is None


async def test_a_tool_that_answers_with_something_else_than_text_is_read_as_text():
    rig = Rig(FakeRpc(answer={"a": 1}))
    await rig.contributions.apply(_manifest(promptFragments=[_anchor("tool", "note")]))
    assert rig.fragments.expand("{{ACME_NOTE}}") == "{'a': 1}"


async def test_a_source_with_no_from_gives_nothing_and_does_not_stop_the_rest():
    rig = Rig()
    manifest = _manifest(
        promptFragments=[_anchor("tool", None)], commands=[{"command": "/scan", "desc": "d"}]
    )
    await rig.contributions.apply(manifest)
    assert rig.fragments.owner_of("ACME_NOTE") is None
    assert rig.rpc.methods == []
    assert rig.commands.get("scan") is not None


async def test_a_file_source_of_an_extension_with_no_known_bundle_gives_nothing():
    rig = Rig()
    await rig.contributions.apply(_manifest(promptFragments=[_anchor("file", "n.md")]))
    assert rig.fragments.owner_of("ACME_NOTE") is None


# ── Agent overrides ────────────────────────────────────────────────

AGENT = "---\nname: explorer\ndescription: x\n---\n<!-- section:body -->\nbody\n<!-- /section:body -->\n"


def _agent_manifest(text_from: str, plugin: str = "plugin") -> ExtensionManifest:
    return _manifest(
        plugin=plugin,
        prompt={
            "overrides": [
                {
                    "target": "agent:explorer",
                    "sections": ["body"],
                    "source": "file",
                    "from": text_from,
                }
            ]
        },
    )


async def test_an_agent_override_falls_back_to_the_bundle_when_the_plugin_is_not_installed(
    tmp_path,
):
    bundle = tmp_path / "bundle"
    (bundle / "plugin").mkdir(parents=True)
    (bundle / "plugin" / "explorer.md").write_text(AGENT)
    rig = Rig(bundles={"acme.scanner": bundle}, plugins=tmp_path / "plugins")
    await rig.contributions.apply(_agent_manifest("plugin/explorer.md", plugin="/plugin/"))
    held = rig.prompts.override("agent:explorer")
    assert held is not None
    assert held[1].path == (bundle / "plugin" / "explorer.md").resolve()


async def test_an_agent_override_with_no_frontmatter_is_refused_and_shown(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "explorer.md").write_text("<!-- section:body -->\nb\n<!-- /section:body -->\n")
    rig = Rig(bundles={"acme.scanner": bundle})
    rig.registry.install(_agent_manifest("explorer.md"), state=ExtensionState.PREPARING)
    await rig.contributions.apply(_agent_manifest("explorer.md"))
    assert rig.prompts.override("agent:explorer") is None
    [refused] = rig.contributions.switched_off("acme.scanner")
    assert refused.claim == "prompt:agent:explorer"
    assert "no frontmatter" in refused.reason
    assert rig.registry.get("acme.scanner").switched_off == [refused]


async def test_an_override_text_that_is_not_sections_at_all_is_refused_with_why(tmp_path):
    (tmp_path / "main.md").write_text("<!-- section:who -->\nunclosed\n")
    rig = Rig(bundles={"acme.scanner": tmp_path})
    manifest = _manifest(
        prompt={
            "overrides": [
                {"target": "main", "sections": ["who"], "source": "file", "from": "main.md"}
            ]
        }
    )
    await rig.contributions.apply(manifest)
    assert rig.prompts.override("main") is None
    [refused] = rig.contributions.switched_off("acme.scanner")
    assert refused.reason.startswith("its text is not a prompt of named sections")


# ── Conditions ─────────────────────────────────────────────────────


def test_a_conditions_notice_forbids_other_fields_and_more_than_sixty_four_conditions():
    assert ConditionsNotice.model_validate({"conditions": {"a": True}}).refresh is None
    with pytest.raises(ValueError):
        ConditionsNotice.model_validate({"conditions": {}, "other": 1})
    ConditionsNotice.model_validate({"conditions": {f"c{i}": True for i in range(64)}})
    with pytest.raises(ValueError):
        ConditionsNotice.model_validate({"conditions": {f"c{i}": True for i in range(65)}})


def _held(rig: Rig) -> Any:
    return rig.prompts._owners.get("acme.scanner")


def test_a_malformed_notice_changes_nothing_and_a_valid_one_asks_for_a_refetch_only_if_told():
    rig = Rig()
    rig.registry.install(_manifest(), state=ExtensionState.READY)
    refetched: list[str] = []
    receiver = ExtensionConditionReceiver(
        registry=rig.registry, prompts=rig.prompts, refetch=refetched.append
    )
    receiver("ext:acme.scanner", {"conditions": {"a": True}, "surprise": 1})
    receiver("ext:acme.scanner", {"conditions": "yes"})
    receiver("ext:acme.scanner", {"conditions": {f"c{i}": True for i in range(65)}})
    assert _held(rig) is None and refetched == []

    receiver("ext:acme.scanner", {"conditions": {}, "refresh": True})
    assert refetched == ["acme.scanner"] and _held(rig) is None, "no conditions, nothing held"

    receiver("ext:acme.scanner", {"conditions": {"a": True}, "refresh": False})
    assert refetched == ["acme.scanner"]
    assert _held(rig).holding == frozenset({"a"})

    ExtensionConditionReceiver(registry=rig.registry, prompts=rig.prompts)(
        "ext:acme.scanner", {"conditions": {}, "refresh": True}
    )


def test_an_extension_that_is_gone_cannot_speak_for_conditions():
    rig = Rig()
    receiver = ExtensionConditionReceiver(registry=rig.registry, prompts=rig.prompts)
    receiver("ext:acme.scanner", {"conditions": {"a": True}})
    assert _held(rig) is None


def test_a_server_that_is_not_an_extension_cannot_speak_for_an_installed_one():
    rig = Rig()
    rig.registry.install(_manifest(), state=ExtensionState.READY)
    receiver = ExtensionConditionReceiver(registry=rig.registry, prompts=rig.prompts)
    receiver("abc:acme.scanner", {"conditions": {"a": True}})
    receiver("acme.scanner", {"conditions": {"a": True}})
    assert _held(rig) is None


async def test_a_bundled_file_that_is_not_text_or_not_there_gives_nothing(tmp_path):
    (tmp_path / "bin.md").write_bytes(b"\xff\xfe\x00\xc3\x28")
    rig = Rig(bundles={"acme.scanner": tmp_path})
    await rig.contributions.apply(_manifest(promptFragments=[_anchor("file", "bin.md")]))
    assert rig.fragments.owner_of("ACME_NOTE") is None
    await rig.contributions.apply(_manifest(promptFragments=[_anchor("file", "missing.md")]))
    assert rig.fragments.owner_of("ACME_NOTE") is None


async def test_a_file_that_is_not_text_does_not_stop_the_extension_s_other_contributions(
    tmp_path,
):
    (tmp_path / "bin.md").write_bytes(b"\xff\xfe\x00\xc3\x28")
    rig = Rig(bundles={"acme.scanner": tmp_path})
    await rig.contributions.apply(
        _manifest(
            promptFragments=[_anchor("file", "bin.md")],
            commands=[{"command": "/scan", "desc": "d"}],
        )
    )
    assert rig.commands.get("scan") is not None
