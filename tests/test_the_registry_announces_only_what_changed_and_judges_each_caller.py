"""The installed set: what a watcher is told, and who may call what.

Watchers hear every real transition and nothing else; a watcher that
raises costs the others nothing. A page's call is judged by
``authorise_caller`` (its own namespace, the host model's methods, a
grant) and an extension's own call by ``authorise``. Each registry
counts its own extensions' failures.
"""

from __future__ import annotations

import logging

import pytest

from igni_core.extensions.faults import ExtensionFaults
from igni_core.extensions.registry import (
    ExtensionMethod,
    ExtensionRefused,
    ExtensionRegistry,
    ExtensionState,
    InstalledExtension,
)
from tests.host_model_helpers import manifest


class Heard:
    """A watcher that keeps what it was told."""

    def __init__(self) -> None:
        self.told: list[tuple[str | None, str]] = []

    def __call__(self, entry: InstalledExtension | None, extension_id: str) -> None:
        self.told.append((entry.state.value if entry else None, extension_id))


@pytest.fixture
def heard() -> Heard:
    return Heard()


@pytest.fixture
def registry(heard: Heard) -> ExtensionRegistry:
    found = ExtensionRegistry()
    found.install(manifest("acme.guard"))
    found.watch(heard)
    return found


# ── The wire name ────────────────────────────────────────────────────


def test_the_method_is_the_last_segment_and_the_id_is_all_before_it() -> None:
    parsed = ExtensionMethod.parse("ext.acme.guard.scan")

    assert parsed == ExtensionMethod(extension_id="acme.guard", method="scan")
    assert parsed is not None and parsed.wire == "ext.acme.guard.scan"


@pytest.mark.parametrize(
    "wire", ["sessions.list", "extx.a.b", "ext.", "ext.x", "ext..m", "ext.x.", "ext.."]
)
def test_a_name_outside_the_namespace_or_missing_a_part_is_not_a_method(wire: str) -> None:
    assert ExtensionMethod.parse(wire) is None


# ── What a watcher is told ───────────────────────────────────────────


def test_installing_announces_and_replaces(registry: ExtensionRegistry, heard: Heard) -> None:
    entry = registry.install(manifest("acme.other"), state=ExtensionState.PREPARING, reason="wait")

    assert entry.state is ExtensionState.PREPARING and entry.reason == "wait"
    assert heard.told == [("PREPARING", "acme.other")]

    registry.install(manifest("acme.other"))

    assert registry.get("acme.other") is not entry
    assert registry.ids() == ["acme.guard", "acme.other"]
    assert heard.told[-1] == ("READY", "acme.other")


def test_the_ids_are_sorted() -> None:
    registry = ExtensionRegistry()
    for name in ("acme.c", "acme.a", "acme.b"):
        registry.install(manifest(name))

    assert registry.ids() == ["acme.a", "acme.b", "acme.c"]


def test_removing_announces_a_removal_only_when_there_was_one(
    registry: ExtensionRegistry, heard: Heard
) -> None:
    assert registry.remove("acme.absent") is False
    assert heard.told == []

    assert registry.remove("acme.guard") is True

    assert heard.told == [(None, "acme.guard")]
    assert registry.get("acme.guard") is None
    assert registry.remove("acme.guard") is False
    assert len(heard.told) == 1


def test_a_state_that_did_not_move_is_not_announced(
    registry: ExtensionRegistry, heard: Heard
) -> None:
    registry.set_state("acme.guard", ExtensionState.READY)
    assert heard.told == []

    registry.set_state("acme.guard", ExtensionState.FAILED, "it crashed")
    registry.set_state("acme.guard", ExtensionState.FAILED, "it crashed")
    assert heard.told == [("FAILED", "acme.guard")]

    registry.set_state("acme.guard", ExtensionState.FAILED, "it crashed again")
    assert len(heard.told) == 2
    entry = registry.get("acme.guard")
    assert entry is not None and entry.reason == "it crashed again"


def test_the_setters_ignore_an_extension_that_is_not_installed(
    registry: ExtensionRegistry, heard: Heard
) -> None:
    registry.set_state("acme.absent", ExtensionState.FAILED, "x")
    registry.set_problems("acme.absent", ["x"])
    registry.set_sandbox("acme.absent", "x")
    registry.set_faulty("acme.absent", "x")

    assert heard.told == []
    assert registry.ids() == ["acme.guard"]


def test_problems_sandbox_and_faulty_announce_only_when_they_change(
    registry: ExtensionRegistry, heard: Heard
) -> None:
    entry = registry.get("acme.guard")
    assert entry is not None
    assert (entry.problems, entry.sandbox, entry.faulty) == ([], "", "")

    registry.set_problems("acme.guard", [])
    registry.set_sandbox("acme.guard", "")
    registry.set_faulty("acme.guard", "")
    assert heard.told == []

    registry.set_problems("acme.guard", ["a hint differs"])
    registry.set_problems("acme.guard", ["a hint differs"])
    registry.set_sandbox("acme.guard", "sandboxed (seatbelt)")
    registry.set_sandbox("acme.guard", "sandboxed (seatbelt)")
    registry.set_faulty("acme.guard", "not responding")
    registry.set_faulty("acme.guard", "not responding")

    assert len(heard.told) == 3
    assert entry.problems == ["a hint differs"]
    assert entry.sandbox == "sandboxed (seatbelt)"
    assert entry.faulty == "not responding"

    registry.set_faulty("acme.guard", "")
    assert entry.faulty == "" and len(heard.told) == 4


def test_a_problem_list_is_kept_as_a_copy() -> None:
    registry = ExtensionRegistry()
    registry.install(manifest("acme.guard"))
    problems = ["one"]

    registry.set_problems("acme.guard", problems)
    problems.append("two")

    entry = registry.get("acme.guard")
    assert entry is not None and entry.problems == ["one"]


def test_a_watcher_that_raises_is_logged_and_the_others_still_hear(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = ExtensionRegistry()
    later = Heard()

    def broken(entry: InstalledExtension | None, extension_id: str) -> None:
        raise RuntimeError("watcher bug")

    registry.watch(broken)
    registry.watch(later)

    with caplog.at_level(logging.ERROR):
        registry.install(manifest("acme.guard"))

    assert later.told == [("READY", "acme.guard")]
    assert "a registry watcher failed for acme.guard" in caplog.text
    assert registry.get("acme.guard") is not None


# ── Each registry counts its own failures ────────────────────────────


def test_two_registries_do_not_share_one_extension_s_failures() -> None:
    first, second = ExtensionRegistry(), ExtensionRegistry()
    for registry in (first, second):
        registry.install(manifest("acme.guard"))

    for _ in range(ExtensionFaults.LIMIT):
        first.faults.failed("acme.guard", "its call scan")

    assert first.faults.faulty("acme.guard")
    assert not second.faults.faulty("acme.guard")
    assert first.faults is first.faults and first.faults is not second.faults
    entry = second.get("acme.guard")
    assert entry is not None and entry.faulty == ""

    # One more failure on the second is its first, not a fourth.
    second.faults.failed("acme.guard", "its call scan")
    assert not second.faults.faulty("acme.guard")


# ── Who may call what ────────────────────────────────────────────────

PAGE = {"host.model", "host.token"}
POOL = {"sessions.list", "windows.open"}


def judge(registry: ExtensionRegistry, caller: str, method: str) -> None:
    registry.authorise_caller(caller=caller, method=method, page_methods=PAGE, pool_methods=POOL)


def test_a_caller_that_is_not_installed_speaks_for_nobody() -> None:
    with pytest.raises(ExtensionRefused) as caught:
        judge(ExtensionRegistry(), "acme.ghost", "host.model")

    assert str(caught.value) == "no extension 'acme.ghost' is installed, so nothing speaks for it."


def test_a_page_may_reach_its_namespace_and_the_host_model_without_a_grant() -> None:
    registry = ExtensionRegistry()
    registry.install(manifest("acme.guard"))

    judge(registry, "acme.guard", "ext.acme.guard.scan")
    judge(registry, "acme.guard", "host.model")
    # The page rule is checked before the pool's: a method in both is a page's.
    registry.authorise_caller(
        caller="acme.guard",
        method="host.model",
        page_methods=PAGE,
        pool_methods=PAGE,
    )


def test_a_page_never_reaches_a_pool_call_even_with_a_grant() -> None:
    registry = ExtensionRegistry()
    registry.install(manifest("acme.guard", rpc=["sessions.list", "sessions.*"]))

    with pytest.raises(ExtensionRefused) as caught:
        judge(registry, "acme.guard", "sessions.list")

    assert str(caught.value) == "an extension's page does not reach 'sessions.list'."


def test_any_other_base_method_needs_a_grant_and_the_refusal_names_the_fix() -> None:
    registry = ExtensionRegistry()
    registry.install(manifest("acme.guard", rpc=["files.read", "memory.*"]))

    judge(registry, "acme.guard", "files.read")
    judge(registry, "acme.guard", "memory.search")
    with pytest.raises(ExtensionRefused) as caught:
        judge(registry, "acme.guard", "files.write")

    assert str(caught.value) == (
        "'acme.guard' is not permitted to call 'files.write'. "
        "Add it to permissions.rpc in the manifest and reinstall."
    )


# ── An extension's own call ──────────────────────────────────────────


def test_a_request_that_does_not_say_who_it_is_from_is_refused() -> None:
    registry = ExtensionRegistry()
    registry.install(manifest("acme.guard", rpc=["ext.acme.guard.*"]))

    with pytest.raises(ExtensionRefused, match="did not say which extension it came from"):
        registry.authorise(caller=None, method="ext.acme.guard.scan")


def test_a_malformed_name_is_refused_with_the_spelling() -> None:
    with pytest.raises(ExtensionRefused, match=r"'ext.x' is not an extension method"):
        ExtensionRegistry().authorise(caller="x", method="ext.x")


def test_an_extension_reaches_its_own_namespace_and_no_other() -> None:
    registry = ExtensionRegistry()
    registry.install(manifest("acme.guard", rpc=["ext.*"]))
    registry.install(manifest("acme.other", rpc=["ext.*"]))

    with pytest.raises(ExtensionRefused) as caught:
        registry.authorise(caller="acme.guard", method="ext.acme.other.scan")

    assert str(caught.value) == (
        "'acme.guard' may not call 'ext.acme.other.scan'. "
        "An extension reaches its own namespace and no other."
    )


def test_a_call_to_an_extension_that_went_away_has_nowhere_to_go() -> None:
    with pytest.raises(ExtensionRefused, match="no extension 'acme.guard' is installed"):
        ExtensionRegistry().authorise(caller="acme.guard", method="ext.acme.guard.scan")


@pytest.mark.parametrize(
    ("state", "reason", "said"),
    [
        (
            ExtensionState.PREPARING,
            "",
            "'acme.guard' is PREPARING, not READY, so 'ext.acme.guard.scan' cannot be served.",
        ),
        (
            ExtensionState.DISABLED,
            "switched off",
            "'acme.guard' is DISABLED, not READY, so 'ext.acme.guard.scan' cannot be served. switched off",
        ),
        (
            ExtensionState.FAILED,
            "licence expired",
            "'acme.guard' is FAILED, not READY, so 'ext.acme.guard.scan' cannot be served. licence expired",
        ),
    ],
)
def test_an_extension_that_is_not_ready_says_what_it_is_and_why(
    state: ExtensionState, reason: str, said: str
) -> None:
    registry = ExtensionRegistry()
    registry.install(manifest("acme.guard", rpc=["ext.acme.guard.*"]), state=state, reason=reason)

    with pytest.raises(ExtensionRefused) as caught:
        registry.authorise(caller="acme.guard", method="ext.acme.guard.scan")

    assert str(caught.value) == said


def test_a_call_needs_a_grant_and_the_whole_namespace_can_be_granted() -> None:
    registry = ExtensionRegistry()
    registry.install(manifest("acme.guard", rpc=["ext.acme.guard.scan"]))

    parsed = registry.authorise(caller="acme.guard", method="ext.acme.guard.scan")
    assert parsed == ExtensionMethod(extension_id="acme.guard", method="scan")
    with pytest.raises(ExtensionRefused) as caught:
        registry.authorise(caller="acme.guard", method="ext.acme.guard.purge")
    assert (
        'Add it to permissions.rpc in the manifest — "ext.acme.guard.*" grants the whole namespace'
        in str(caught.value)
    )

    registry.install(manifest("acme.guard", rpc=["ext.acme.guard.*"]))
    assert registry.authorise(caller="acme.guard", method="ext.acme.guard.purge").method == "purge"
