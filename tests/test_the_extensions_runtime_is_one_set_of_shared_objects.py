"""The process-level extension objects are built once, wired to each other, and shared.

A second session must not see a different set of extensions, so the
registry, its RPC, the health monitor, the hooks and the contributions
are one set. These build it the way the backend does and drive it
through what a user sees: an extension whose call fails is checked at
once, one that keeps failing is restarted and its person told (when the
backend asked for that), an extension's command that is igni's own is
switched off, and an extension reports what holds for it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from igni_core.backend.command_handler import CommandHandler
from igni_core.backend.extensions_runtime import ExtensionsRuntime
from igni_core.core.mcp.schemas import MCPConnectResult
from igni_core.core.prompts import PromptFragments, PromptLoader
from igni_core.core.prompts.contribution_models import EditOp, PromptEdit
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.extensions.faults import ExtensionFaults
from igni_core.extensions.install_service import server_name
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.registry import ExtensionRefused, ExtensionState
from igni_core.extensions.service import ExtensionServiceError
from igni_core.extensions.store import ExtensionStore
from igni_core.notifications.extension_notices import FaultNotices


class _Session:
    async def list_tools(self) -> list[Any]:
        return []


class _Client:
    async def get_session_for_run(self) -> _Session:
        return _Session()


class _Mcp:
    """What the extensions' services connect through: records what is asked of it.

    ``alive`` says whether an extension's connection answers a probe.
    """

    def __init__(self, *, alive: bool = False) -> None:
        self.alive = alive
        self.reconnects = True
        self.disconnected: list[str] = []
        self.connected: list[str] = []

    def get_client(self, name: str) -> _Client | None:
        return _Client() if self.alive else None

    async def disconnect_one(self, name: str) -> None:
        self.disconnected.append(name)

    async def connect(self, name: str) -> Any:
        self.connected.append(name)
        return MCPConnectResult(ok=self.reconnects, reason="it will not start")

    def get_tools(self, name: str) -> dict[str, Any]:
        return {}


class _Sink:
    """Where a person's notices land."""

    def __init__(self) -> None:
        self.kept: list[tuple[str, Any]] = []

    def keep_local(self, source: str, notice: Any) -> None:
        self.kept.append((source, notice))


class _Dead:
    """A service that never answers."""

    async def call(self, method: str, args: dict[str, Any]) -> Any:
        raise ExtensionServiceError("timed out", unanswered=True)


def _manifest(extension_id: str = "acme.guard", commands: list[str] | None = None):
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "name": extension_id,
            "trust": "unsigned",
            "dev": {"command": ["python", "server.py"]},
            "contributes": {
                "pages": [{"kind": f"{extension_id}.home", "label": "Home"}],
                "commands": [{"command": c, "desc": "does a thing"} for c in commands or ["/scan"]],
            },
        }
    )


@pytest.fixture
def mcp() -> _Mcp:
    return _Mcp()


@pytest.fixture
def prompts() -> PromptContributions:
    return PromptContributions()


@pytest.fixture
def runtime(tmp_path: Path, mcp: _Mcp, prompts: PromptContributions) -> ExtensionsRuntime:
    return ExtensionsRuntime.build(
        mcp=mcp,
        plugins_dir=tmp_path / "plugins",
        store=ExtensionStore(),
        prompts=prompts,
        prompt_loader=PromptLoader(PromptFragments()),
    )


async def _until(condition, what: str) -> None:
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never happened: {what}")


def test_every_part_is_the_same_object_each_time_it_is_asked_for(runtime, prompts):
    assert runtime.registry is runtime.rpc.registry
    assert runtime.registry is runtime.registry
    assert runtime.hooks is runtime.hooks
    assert runtime.processes is runtime.processes
    assert runtime.origins is runtime.origins
    assert runtime.health is runtime.health
    assert runtime.contributions is runtime.contributions
    assert runtime.prompts is prompts


def test_two_builds_share_nothing(tmp_path, mcp):
    def build() -> ExtensionsRuntime:
        return ExtensionsRuntime.build(
            mcp=mcp,
            plugins_dir=tmp_path,
            store=ExtensionStore(),
            prompts=PromptContributions(),
            prompt_loader=PromptLoader(PromptFragments()),
        )

    first, second = build(), build()
    first.registry.install(_manifest(), state=ExtensionState.READY)

    assert second.registry.get("acme.guard") is None
    assert first.registry is not second.registry


@pytest.mark.asyncio
async def test_a_failed_call_has_the_extension_checked_at_once(runtime, mcp):
    mcp.reconnects = False  # so the state the check finds is the one left standing
    runtime.registry.install(_manifest(), state=ExtensionState.READY)
    runtime.rpc.bind("acme.guard", _Dead())

    with pytest.raises(ExtensionRefused):
        await runtime.rpc.invoke_command("acme.guard", "/scan", "")

    # The monitor probed: there is no connection for it, so it is FAILED
    # long before the monitor's own interval.
    await _until(
        lambda: runtime.registry.get("acme.guard").state is ExtensionState.FAILED,
        "the extension is no longer READY",
    )
    assert "not connected" in runtime.registry.get("acme.guard").reason


def test_a_command_that_is_igni_s_own_is_switched_off_for_the_extension(runtime):
    own = CommandHandler.builtin_names()[0]
    assert own

    runtime.registry.install(_manifest(commands=[f"/{own}"]), state=ExtensionState.PREPARING)

    off = runtime.registry.get("acme.guard").switched_off
    assert off, "an extension cannot take a command igni already has"


def test_two_extensions_claiming_one_command_collide(runtime):
    runtime.registry.install(_manifest("acme.one", ["/same"]), state=ExtensionState.PREPARING)
    runtime.registry.install(_manifest("acme.two", ["/same"]), state=ExtensionState.PREPARING)

    assert runtime.contributions.resolve().collisions


@pytest.mark.asyncio
async def test_without_attach_faults_a_dead_extension_is_not_restarted(runtime, mcp):
    mcp.alive = True  # the monitor's own probe finds it connected; only the fault count is at issue
    runtime.registry.install(_manifest(), state=ExtensionState.READY)
    runtime.rpc.bind("acme.guard", _Dead())

    for _ in range(ExtensionFaults.LIMIT):
        with pytest.raises(ExtensionRefused):
            await runtime.rpc.invoke_command("acme.guard", "/scan", "")
    await asyncio.sleep(0.1)

    assert runtime.registry.faults.faulty("acme.guard")
    assert mcp.connected == []


@pytest.mark.asyncio
async def test_an_extension_that_keeps_failing_is_restarted_and_the_person_told(runtime, mcp):
    mcp.alive = True
    sink = _Sink()
    runtime.attach_faults(sink)  # type: ignore[arg-type]
    runtime.registry.install(_manifest(), state=ExtensionState.READY)
    runtime.rpc.bind("acme.guard", _Dead())

    for _ in range(ExtensionFaults.LIMIT):
        with pytest.raises(ExtensionRefused):
            await runtime.rpc.invoke_command("acme.guard", "/scan", "")

    await _until(lambda: mcp.connected, "the extension's service is reconnected")
    assert mcp.connected == [server_name("acme.guard")]
    [(source, notice)] = sink.kept
    assert source == "acme.guard"
    assert notice.kind == FaultNotices.KIND
    assert "isn't responding" in notice.title


@pytest.mark.asyncio
async def test_the_condition_receiver_works_over_the_runtime_s_own_tables(runtime, prompts):
    prompts.set_owner(
        "acme.guard",
        "acme.guard",
        edits=[
            PromptEdit(target="main", op=EditOp.APPEND, name="extra", text="Hi.", when="covered")
        ],
    )
    runtime.registry.install(_manifest(), state=ExtensionState.READY)
    receiver = runtime.condition_receiver()
    assert prompts.edits("main") == []

    receiver(server_name("acme.guard"), {"conditions": {"covered": True}})

    assert [owner for owner, _ in prompts.edits("main")] == ["acme.guard"]
    assert runtime.condition_receiver() is not receiver
