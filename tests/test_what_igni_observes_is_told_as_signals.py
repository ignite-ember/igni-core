"""``HostObservations``: the registry moving, the workspace's HEAD and files,
a session switched — each told to the extensions that may hear it, and to
no one else.

A real :class:`SignalBus` over the real host schema, with one sink per
extension; the grants come from real manifests.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.backend.workspace_watch import Head
from igni_core.core.activity import ActivityRegistry
from igni_core.extensions.busy import ExtensionBusy
from igni_core.extensions.events import HostTopic
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.observations import (
    ExtensionStateChanged,
    HostObservations,
    SelfStateChanged,
)
from igni_core.extensions.host.signal_values import Emitter
from igni_core.extensions.host.signals import SignalBus
from igni_core.extensions.registry import ExtensionState, InstalledExtension
from tests.host_model_helpers import Sink, manifest

STATE = HostTopic.EXTENSION_STATE_CHANGED.value
SELF = "self.state_changed"
HEAD = HostTopic.WORKSPACE_HEAD_CHANGED.value
FILES = HostTopic.WORKSPACE_FILES_CHANGED.value
SWITCHED = HostTopic.SESSION_SWITCHED.value


class Rig:
    """A bus, extensions listening on it, and the observations over it."""

    def __init__(self) -> None:
        self.grants: dict[str, HostGrants] = {}
        self.sinks: dict[str, Sink] = {}
        self.bus = SignalBus(HOST_SCHEMA, self.grants.get)
        self.registry = ActivityRegistry()
        self.busy = ExtensionBusy(self.registry)
        self.observations = HostObservations(Emitter(self.bus.emit), self.busy)

    def listen(self, extension_id: str, *wires: str, **permissions: Any) -> Sink:
        self.grants[extension_id] = HostGrants(manifest(extension_id, **permissions))
        sink = Sink()
        self.sinks[extension_id] = sink
        self.bus.subscribe(self.bus.attach(extension_id, sink), list(wires))
        return sink


def entry(extension_id: str, state: ExtensionState, reason: str = "") -> InstalledExtension:
    return InstalledExtension(manifest=manifest(extension_id), state=state, reason=reason)


def test_a_state_change_goes_to_whoever_sees_all_and_to_the_extension_itself() -> None:
    rig = Rig()
    watcher = rig.listen("acme.watcher", STATE, SELF, host=["extensions"])
    own = rig.listen("acme.own", STATE, SELF)
    stranger = rig.listen("acme.stranger", STATE, SELF)

    rig.observations.extension_changed(
        entry("acme.own", ExtensionState.FAILED, "its service died"), "acme.own"
    )

    told = {"extension_id": "acme.own", "state": "FAILED", "reason": "its service died"}
    assert watcher.got == [(STATE, told)], "the one that sees all hears it, but not the self signal"
    assert own.got == [(SELF, told)], "the extension hears its own, never the all-seeing one"
    assert stranger.got == []


def test_the_self_signal_is_only_for_the_extension_it_is_about() -> None:
    rig = Rig()
    other = rig.listen("acme.other", SELF)
    rig.listen("acme.own", SELF)

    rig.observations.extension_changed(entry("acme.own", ExtensionState.READY), "acme.own")

    assert other.got == []


def test_a_removed_extension_sends_nothing() -> None:
    rig = Rig()
    watcher = rig.listen("acme.watcher", STATE, SELF, host=["extensions"])

    rig.observations.extension_changed(None, "acme.gone")

    assert watcher.got == []


def held(rig: Rig) -> list[tuple[str, str]]:
    return sorted((b.holder, b.reason) for b in rig.registry.busy())


@pytest.mark.parametrize(
    "state", [ExtensionState.FAILED, ExtensionState.DISABLED, ExtensionState.PREPARING, None]
)
def test_an_extension_that_is_not_ready_is_no_longer_busy(state: ExtensionState | None) -> None:
    rig = Rig()
    rig.busy.busy("acme.own", "indexing")
    rig.busy.busy("acme.own", "syncing")
    rig.busy.busy("acme.other", "indexing")

    rig.observations.extension_changed(
        None if state is None else entry("acme.own", state), "acme.own"
    )

    assert held(rig) == [("acme.other", "indexing")]


def test_an_extension_that_is_ready_keeps_what_it_is_busy_with() -> None:
    rig = Rig()
    rig.busy.busy("acme.own", "indexing")

    rig.observations.extension_changed(entry("acme.own", ExtensionState.READY), "acme.own")

    assert held(rig) == [("acme.own", "indexing")]


def test_the_state_signals_are_the_published_shape() -> None:
    signal = ExtensionStateChanged.of_entry(
        entry("acme.own", ExtensionState.PREPARING, "starting"), "acme.own"
    )

    assert signal.WIRE == STATE
    assert SelfStateChanged.WIRE == SELF
    assert signal.model_dump() == {
        "extension_id": "acme.own",
        "state": "PREPARING",
        "reason": "starting",
    }


def test_head_goes_only_to_those_holding_the_workspace_grant() -> None:
    rig = Rig()
    granted = rig.listen("acme.reader", HEAD, workspace="read")
    refused = rig.listen("acme.blind", HEAD)

    rig.observations.head_changed(Head(commit_sha="abc123", branch="main"))

    assert granted.got == [(HEAD, {"commit_sha": "abc123", "branch": "main"})]
    assert refused.got == []


def changed(rig: Rig, paths: set[str], cap: int = 500) -> None:
    rig.observations.files_changed(frozenset(paths), cap)


def test_each_extension_sees_the_sorted_paths_its_grant_permits() -> None:
    rig = Rig()
    everything = rig.listen("acme.all", FILES, workspace="read")
    docs_only = rig.listen("acme.docs", FILES, workspace="read", workspacePaths=["docs/*"])

    changed(rig, {"src/b.py", "docs/z.md", "src/a.py", "docs/a.md"})

    assert everything.values(FILES) == [
        {
            "paths": ["docs/a.md", "docs/z.md", "src/a.py", "src/b.py"],
            "truncated": False,
        }
    ]
    assert docs_only.values(FILES) == [{"paths": ["docs/a.md", "docs/z.md"], "truncated": False}]


def test_an_extension_that_may_see_none_of_the_paths_is_told_nothing() -> None:
    rig = Rig()
    docs_only = rig.listen("acme.docs", FILES, workspace="read", workspacePaths=["docs/*"])
    blind = rig.listen("acme.blind", FILES)

    changed(rig, {"src/a.py"})
    changed(rig, set())

    assert docs_only.got == [] and blind.got == []


def test_exactly_the_cap_is_not_truncated_and_one_more_is() -> None:
    rig = Rig()
    reader = rig.listen("acme.reader", FILES, workspace="read")

    changed(rig, {"a", "b", "c"}, cap=3)
    changed(rig, {"a", "b", "c", "d"}, cap=3)

    assert reader.values(FILES) == [
        {"paths": ["a", "b", "c"], "truncated": False},
        {"paths": ["a", "b", "c"], "truncated": True},
    ]


def test_the_cap_counts_only_what_the_extension_may_see() -> None:
    rig = Rig()
    docs_only = rig.listen("acme.docs", FILES, workspace="read", workspacePaths=["docs/*"])

    changed(rig, {"docs/a", "docs/b", "src/1", "src/2", "src/3"}, cap=2)

    assert docs_only.values(FILES) == [{"paths": ["docs/a", "docs/b"], "truncated": False}]


class Recorded:
    """An emit that keeps what the observations handed the bus."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, str | None]] = []

    def __call__(self, wire: str, value: Any = None, *, window: str | None = None, **_: Any):
        self.calls.append((wire, value, window))
        return []


@pytest.mark.parametrize("window", ["w-main", None])
def test_a_session_switch_names_its_session_and_its_window(window: str | None) -> None:
    emit = Recorded()
    observations = HostObservations(Emitter(emit), ExtensionBusy(ActivityRegistry()))

    observations.session_switched("s-2", window)

    assert emit.calls == [(SWITCHED, {"session_id": "s-2", "window": window}, window)]


def test_a_session_switch_reaches_a_listener_with_the_session_grant() -> None:
    rig = Rig()
    heard = rig.listen("acme.one", SWITCHED, host=["sessions"])

    rig.observations.session_switched("s-2", "w-main")

    assert heard.values(SWITCHED) == [{"session_id": "s-2", "window": "w-main"}]
