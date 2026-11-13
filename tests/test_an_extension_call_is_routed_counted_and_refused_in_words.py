"""``ExtensionRpc``: the ``ext.`` namespace and the slash commands, routed to the bound service.

A call is authorised first and routed second, so a refusal never
depends on whether a service is reachable. Every call is counted: a
silence toward the extension being faulty, any answer — a tool's own
error included — clearing it. A service that fails tells the health
monitor at once.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from igni_core.extensions.faults import ExtensionFaults
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.registry import ExtensionRefused, ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.service import ExtensionServiceError

EXT = "acme.guard"


def guard_manifest() -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": EXT,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "name": EXT,
            "trust": "unsigned",
            "dev": {"command": ["python", "server.py"]},
            "permissions": {"rpc": [f"ext.{EXT}.*"]},
            "contributes": {
                "commands": [
                    {"command": "/compliance", "desc": "Check compliance"},
                    {"command": "/audit", "desc": "Audit"},
                ],
            },
        }
    )


class Service:
    """Answers each call with what it was given, or fails as told."""

    def __init__(self, fail: ExtensionServiceError | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def call(self, method: str, args: dict[str, Any]) -> Any:
        self.calls.append((method, args))
        if self.fail is not None:
            raise self.fail
        return {"method": method, "args": args}


@pytest.fixture
def registry() -> ExtensionRegistry:
    found = ExtensionRegistry()
    found.install(guard_manifest())
    return found


@pytest.fixture
def rpc(registry: ExtensionRegistry) -> ExtensionRpc:
    return ExtensionRpc(registry)


@pytest.fixture
def failures(rpc: ExtensionRpc) -> list[str]:
    told: list[str] = []
    rpc.watch(told.append)
    return told


# ── What is the namespace's ──────────────────────────────────────────


def test_anything_under_ext_is_owned_even_when_malformed() -> None:
    assert ExtensionRpc.owns("ext.acme.guard.scan")
    assert ExtensionRpc.owns("ext.")
    assert ExtensionRpc.owns("ext.x")
    assert not ExtensionRpc.owns("extx.a.b")
    assert not ExtensionRpc.owns("sessions.list")


def test_the_rpc_exposes_the_registry_it_was_given(
    rpc: ExtensionRpc, registry: ExtensionRegistry
) -> None:
    assert rpc.registry is registry


# ── dispatch ─────────────────────────────────────────────────────────


async def test_a_call_is_routed_by_its_final_segment_and_returns_the_answer(
    rpc: ExtensionRpc,
) -> None:
    service = Service()
    rpc.bind(EXT, service)

    answer = await rpc.dispatch(caller=EXT, method=f"ext.{EXT}.scan", args={"path": "/x"})

    assert answer == {"method": "scan", "args": {"path": "/x"}}
    assert service.calls == [("scan", {"path": "/x"})]


async def test_a_refusal_does_not_wait_for_a_service(rpc: ExtensionRpc) -> None:
    """Not bound, and not allowed: the author is told about the permission, not the service."""
    with pytest.raises(ExtensionRefused, match="may not call"):
        await rpc.dispatch(caller="acme.other", method=f"ext.{EXT}.scan", args={})


async def test_without_a_bound_service_it_says_igni_is_at_fault(rpc: ExtensionRpc) -> None:
    with pytest.raises(ExtensionRefused) as caught:
        await rpc.dispatch(caller=EXT, method=f"ext.{EXT}.scan", args={})

    assert str(caught.value) == (
        "'acme.guard' is installed but has no service bound, so igni cannot reach it. "
        "This is a fault in igni, not in the extension's manifest."
    )


async def test_an_unbound_service_is_no_longer_reached(rpc: ExtensionRpc) -> None:
    service = Service()
    rpc.bind(EXT, service)
    rpc.unbind(EXT)
    rpc.unbind(EXT)  # nothing bound: not an error

    with pytest.raises(ExtensionRefused, match="no service bound"):
        await rpc.dispatch(caller=EXT, method=f"ext.{EXT}.scan", args={})

    assert service.calls == []


async def test_a_service_error_is_refused_with_its_message_and_the_monitor_is_told(
    rpc: ExtensionRpc, failures: list[str]
) -> None:
    rpc.bind(EXT, Service(fail=ExtensionServiceError("'scan' reported: nope")))

    with pytest.raises(ExtensionRefused) as caught:
        await rpc.dispatch(caller=EXT, method=f"ext.{EXT}.scan", args={})

    assert str(caught.value) == "'scan' reported: nope"
    assert failures == [EXT]


async def test_a_failure_with_nobody_watching_is_still_a_refusal(rpc: ExtensionRpc) -> None:
    rpc.bind(EXT, Service(fail=ExtensionServiceError("gone", unanswered=True)))

    with pytest.raises(ExtensionRefused, match="gone"):
        await rpc.dispatch(caller=EXT, method=f"ext.{EXT}.scan", args={})


# ── Counting ─────────────────────────────────────────────────────────


async def test_silences_make_it_faulty_and_the_next_answer_clears_it(
    rpc: ExtensionRpc, registry: ExtensionRegistry
) -> None:
    silent = Service(fail=ExtensionServiceError("did not answer within 60s", unanswered=True))
    rpc.bind(EXT, silent)
    for _ in range(ExtensionFaults.LIMIT):
        with pytest.raises(ExtensionRefused):
            await rpc.dispatch(caller=EXT, method=f"ext.{EXT}.scan", args={})
    entry = registry.get(EXT)
    assert entry is not None and "its call scan: did not answer within 60s" in entry.faulty

    rpc.bind(EXT, Service())
    await rpc.dispatch(caller=EXT, method=f"ext.{EXT}.scan", args={})

    assert entry.faulty == ""


async def test_an_answer_between_silences_starts_the_count_again(
    rpc: ExtensionRpc, registry: ExtensionRegistry
) -> None:
    silent = Service(fail=ExtensionServiceError("silent", unanswered=True))
    for service in [silent, silent, Service(), silent, silent]:
        rpc.bind(EXT, service)
        with contextlib.suppress(ExtensionRefused):
            await rpc.dispatch(caller=EXT, method=f"ext.{EXT}.scan", args={})

    assert not registry.faults.faulty(EXT)


async def test_the_count_is_kept_per_registry() -> None:
    first, second = ExtensionRegistry(), ExtensionRegistry()
    for registry in (first, second):
        registry.install(guard_manifest())
    rpcs = [ExtensionRpc(first), ExtensionRpc(second)]
    silent = Service(fail=ExtensionServiceError("silent", unanswered=True))
    for rpc in rpcs:
        rpc.bind(EXT, silent)

    for _ in range(ExtensionFaults.LIMIT):
        with pytest.raises(ExtensionRefused):
            await rpcs[0].dispatch(caller=EXT, method=f"ext.{EXT}.scan", args={})

    assert first.faults.faulty(EXT) and not second.faults.faulty(EXT)


# ── Slash commands ───────────────────────────────────────────────────


async def test_a_command_runs_as_the_tool_of_that_name_with_the_text_as_args(
    rpc: ExtensionRpc,
) -> None:
    service = Service()
    rpc.bind(EXT, service)

    answer = await rpc.invoke_command(EXT, "compliance", "the whole repo")

    assert answer == {"method": "compliance", "args": {"args": "the whole repo"}}


@pytest.mark.parametrize(
    "typed", ["/compliance", "//Compliance", "  COMPLIANCE ", "/audit", "Audit"]
)
async def test_a_declared_command_is_found_whatever_its_slash_or_case(
    rpc: ExtensionRpc, typed: str
) -> None:
    service = Service()
    rpc.bind(EXT, service)

    await rpc.invoke_command(EXT, typed, "")

    assert service.calls[0][0] == typed.lstrip("/").strip().lower()
    assert service.calls[0][1] == {"args": ""}


async def test_a_command_of_an_extension_that_is_not_installed_is_refused(
    rpc: ExtensionRpc,
) -> None:
    with pytest.raises(ExtensionRefused) as caught:
        await rpc.invoke_command("acme.ghost", "compliance", "")

    assert str(caught.value) == "no extension 'acme.ghost' is installed."


@pytest.mark.parametrize(
    ("reason", "said"),
    [
        (
            "",
            "'acme.guard' is FAILED, not READY, so '/compliance' cannot run.",
        ),
        (
            "licence expired",
            "'acme.guard' is FAILED, not READY, so '/compliance' cannot run. licence expired",
        ),
    ],
)
async def test_a_command_of_an_extension_that_is_not_ready_says_why(
    rpc: ExtensionRpc, registry: ExtensionRegistry, reason: str, said: str
) -> None:
    registry.set_state(EXT, ExtensionState.FAILED, reason)
    service = Service()
    rpc.bind(EXT, service)

    with pytest.raises(ExtensionRefused) as caught:
        await rpc.invoke_command(EXT, "//compliance", "")

    assert str(caught.value) == said
    assert service.calls == []


async def test_a_command_the_manifest_does_not_declare_is_refused_with_the_fix(
    rpc: ExtensionRpc,
) -> None:
    service = Service()
    rpc.bind(EXT, service)

    with pytest.raises(ExtensionRefused) as caught:
        await rpc.invoke_command(EXT, "/Purge", "")

    assert str(caught.value) == (
        "'acme.guard' does not declare '/purge'. Add it to "
        "contributes.commands in the manifest and reinstall."
    )
    assert service.calls == []


async def test_a_command_with_no_bound_service_is_refused(rpc: ExtensionRpc) -> None:
    with pytest.raises(ExtensionRefused) as caught:
        await rpc.invoke_command(EXT, "compliance", "")

    assert str(caught.value) == (
        "'acme.guard' is installed but has no service bound, so igni cannot reach it."
    )


async def test_a_command_that_fails_is_refused_the_monitor_is_told_and_it_is_counted(
    rpc: ExtensionRpc, registry: ExtensionRegistry, failures: list[str]
) -> None:
    rpc.bind(EXT, Service(fail=ExtensionServiceError("did not answer", unanswered=True)))

    for _ in range(ExtensionFaults.LIMIT):
        with pytest.raises(ExtensionRefused, match="did not answer"):
            await rpc.invoke_command(EXT, "compliance", "")

    assert failures == [EXT] * ExtensionFaults.LIMIT
    assert registry.faults.faulty(EXT)


async def test_a_command_the_tool_reports_an_error_for_has_still_been_answered(
    rpc: ExtensionRpc, registry: ExtensionRegistry
) -> None:
    registry.faults.failed(EXT, "x")
    registry.faults.failed(EXT, "x")
    rpc.bind(EXT, Service(fail=ExtensionServiceError("'compliance' reported: nope")))

    with pytest.raises(ExtensionRefused, match="reported: nope"):
        await rpc.invoke_command(EXT, "compliance", "")
    registry.faults.failed(EXT, "x")

    assert not registry.faults.faulty(EXT), "the count started again at the answer"
