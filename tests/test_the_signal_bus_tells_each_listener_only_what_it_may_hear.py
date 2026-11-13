"""The signal bus delivers a signal to the subscribers entitled to it.

Driven on a small schema of its own so each rule is seen alone: the
grant is checked where the signal is dispatched, ``host.changed`` is
built from the signals that report a property, and a producer can ask
whether anyone would hear before it does the work.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.signals import CHANGED, Change, SignalBus, SubscribeOutcome
from igni_core.extensions.host.spec import (
    HostSchema,
    ObjectSpec,
    PermissionSpec,
    field,
    method,
    prop,
    signal,
    typedef,
)
from igni_core.extensions.host.wire import Op, Request, SignalMessage
from tests.host_model_helpers import manifest

SCHEMA = HostSchema(
    version="1.0.0",
    permissions=(
        PermissionSpec(name="chat.read", doc="Read chat."),
        PermissionSpec(name="files.read", doc="Read files."),
    ),
    types=(
        typedef("Note", "A note.", field("text", "str")),
        typedef(
            "Change",
            "A change.",
            field("path", "str"),
            field("value", "json"),
            field("window", "str", optional=True),
        ),
    ),
    objects=(
        ObjectSpec(
            path="",
            doc="Root.",
            permission=None,
            properties=(prop("mood", "str", "Mood.", changed_by=["ping"]),),
            signals=(
                signal("ping", "Note", "Ping."),
                signal("changed", "Change", "Anything changed."),
            ),
        ),
        ObjectSpec(
            path="chat",
            doc="Chat.",
            permission="chat.read",
            properties=(
                prop("busy", "bool", "Busy.", changed_by=["state"]),
                prop("secret", "str", "Secret.", changed_by=["state"], permission="files.read"),
            ),
            signals=(
                signal("state", "Note", "State."),
                signal("later", "Note", "Later.", stage=2),
            ),
            methods=(method("post", "Post."),),
        ),
        ObjectSpec(
            path="ui",
            doc="UI.",
            permission="chat.read",
            per_window=True,
            properties=(prop("zoom", "int", "Zoom.", changed_by=["zoomed"]),),
            signals=(signal("zoomed", "Note", "Zoomed."),),
        ),
        ObjectSpec(
            path="other",
            doc="Other.",
            permission="chat.read",
            properties=(prop("tick", "int", "Tick.", changed_by=["chat.state"]),),
        ),
    ),
)


class Ear:
    def __init__(self) -> None:
        self.heard: list[SignalMessage] = []

    def deliver(self, message: SignalMessage) -> None:
        self.heard.append(message)


class BrokenEar:
    def deliver(self, message: SignalMessage) -> None:
        raise RuntimeError("socket closed")


class Bus:
    """A bus whose extensions are granted by the table below."""

    def __init__(self, schema: HostSchema = SCHEMA, **granted: list[str]) -> None:
        self.granted = granted
        self.bus = SignalBus(schema, self._grants)

    def _grants(self, extension_id: str) -> HostGrants | None:
        if extension_id not in self.granted:
            return None
        return HostGrants(manifest(f"acme.{extension_id}", host=self.granted[extension_id]))

    def listen(self, extension_id: str, *wires: str):
        ear = Ear()
        subscriber = self.bus.attach(extension_id, ear)
        outcome = self.bus.subscribe(subscriber, list(wires))
        return ear, subscriber, outcome


def _changes(ear: Ear) -> list[dict[str, Any]]:
    return [m.value for m in ear.heard if m.signal == CHANGED]


# ── Subscribing ──────────────────────────────────────────────────


def test_a_subscription_says_what_is_heard_what_is_not_granted_and_what_is_unknown():
    bus = Bus(acme=["chat.read"])

    _, subscriber, outcome = bus.listen("acme", "chat.state", "nope", "ping")

    assert outcome.model_dump() == {
        "subscribed": ["chat.state", "ping"],
        "not_granted": [],
        "unknown": ["nope"],
    }
    assert list(outcome.model_dump()) == ["subscribed", "not_granted", "unknown"]
    assert subscriber.wires == {"chat.state", "ping"}


def test_an_ungranted_signal_is_recorded_as_not_granted_and_never_arrives():
    bus = Bus(acme=["files.read"])

    ear, subscriber, outcome = bus.listen("acme", "chat.state")

    assert outcome == SubscribeOutcome(not_granted=["chat.state"])
    assert subscriber.wires == {"chat.state"}
    assert bus.bus.emit("chat.state", {"text": "x"}) == []
    assert ear.heard == []


def test_an_extension_that_is_not_installed_hears_nothing_and_counts_as_not_granted():
    bus = Bus(acme=["chat.read"])

    ear, _, outcome = bus.listen("ghost", "ping")

    assert outcome.not_granted == ["ping"]
    assert bus.bus.emit("ping", {"text": "x"}) == []
    assert ear.heard == []


def test_subscribing_twice_to_a_wire_tells_it_once():
    bus = Bus(acme=["chat.read"])
    ear, subscriber, _ = bus.listen("acme", "ping")
    bus.bus.subscribe(subscriber, ["ping"])

    bus.bus.emit("ping", {"text": "x"})

    assert [m.signal for m in ear.heard] == ["ping"]


def test_answering_a_subscribe_gives_the_outcome_and_an_unsubscribe_gives_nothing():
    bus = Bus(acme=["chat.read"])
    ear, subscriber, _ = bus.listen("acme")

    outcome = bus.bus.answer(
        subscriber, Request(id=1, op=Op.SUBSCRIBE, signals=["ping", "chat.state", "x"])
    )
    assert outcome is not None
    assert outcome.subscribed == ["ping", "chat.state"] and outcome.unknown == ["x"]

    assert bus.bus.answer(subscriber, Request(id=2, op=Op.UNSUBSCRIBE, signals=["ping"])) is None
    assert subscriber.wires == {"chat.state"}
    bus.bus.emit("ping", {"text": "x"})
    assert ear.heard == []


def test_unsubscribing_from_an_unknown_wire_changes_nothing():
    bus = Bus(acme=["chat.read"])
    _, subscriber, _ = bus.listen("acme", "ping")

    bus.bus.unsubscribe(subscriber, ["nope"])

    assert subscriber.wires == {"ping"}


def test_a_detached_subscriber_is_told_nothing_and_detaching_twice_is_quiet():
    bus = Bus(acme=["chat.read"])
    ear, subscriber, _ = bus.listen("acme", "ping")

    bus.bus.detach(subscriber)
    bus.bus.detach(subscriber)
    bus.bus.detach(bus.bus.attach("acme", Ear()))

    assert bus.bus.emit("ping", {"text": "x"}) == []
    assert ear.heard == []


# ── Delivery ─────────────────────────────────────────────────────


def test_a_signal_reaches_each_granted_subscriber_and_names_them_in_the_return():
    bus = Bus(acme=["chat.read"], beta=["chat.read"], gamma=["files.read"])
    acme, _, _ = bus.listen("acme", "chat.state")
    beta, _, _ = bus.listen("beta", "chat.state")
    gamma, _, _ = bus.listen("gamma", "chat.state")

    told = bus.bus.emit("chat.state", {"text": "x"})

    assert told == ["acme", "beta"]
    assert acme.heard == [SignalMessage(signal="chat.state", value={"text": "x"})]
    assert beta.heard == acme.heard
    assert gamma.heard == []


def test_emitting_something_that_is_not_a_signal_raises_key_error():
    bus = Bus(acme=["chat.read"])

    with pytest.raises(KeyError, match="chat.busy"):
        bus.bus.emit("chat.busy")
    with pytest.raises(KeyError):
        bus.bus.emit("nope")


def test_a_stage_two_signal_is_in_the_schema_and_never_sent():
    bus = Bus(acme=["chat.read"])
    ear, _, outcome = bus.listen("acme", "chat.later", CHANGED)

    assert outcome.subscribed == ["chat.later", CHANGED]
    assert bus.bus.emit("chat.later", {"text": "x"}) == []
    assert ear.heard == []


def test_an_addressed_signal_tells_only_the_extension_it_names():
    bus = Bus(acme=["chat.read"], beta=["chat.read"])
    acme, _, _ = bus.listen("acme", "ping")
    beta, _, _ = bus.listen("beta", "ping")

    assert bus.bus.emit("ping", {"text": "x"}, only="beta") == ["beta"]
    assert bus.bus.emit("ping", {"text": "x"}, only="nobody") == []

    assert acme.heard == []
    assert len(beta.heard) == 1


def test_each_listener_hears_the_view_its_grants_allow():
    bus = Bus(acme=["chat.read"], beta=["chat.read", "files.read"])
    acme, _, _ = bus.listen("acme", "chat.state")
    beta, _, _ = bus.listen("beta", "chat.state")

    def content(grants: HostGrants) -> Any:
        return {"text": "full" if grants.allows("files.read") else "part"}

    assert bus.bus.emit("chat.state", content=content) == ["acme", "beta"]

    assert acme.heard[0].value == {"text": "part"}
    assert beta.heard[0].value == {"text": "full"}


def test_a_view_of_none_or_one_that_raises_is_heard_by_that_listener_only(caplog):
    bus = Bus(acme=["chat.read"], beta=["chat.read", "files.read"])
    acme, _, _ = bus.listen("acme", "chat.state")
    beta, _, _ = bus.listen("beta", "chat.state")

    def nothing_for_acme(grants: HostGrants) -> Any:
        return None if not grants.allows("files.read") else {"text": "x"}

    assert bus.bus.emit("chat.state", content=nothing_for_acme) == ["beta"]

    def raises_for_acme(grants: HostGrants) -> Any:
        if not grants.allows("files.read"):
            raise RuntimeError("cannot read")
        return {"text": "y"}

    with caplog.at_level("ERROR"):
        assert bus.bus.emit("chat.state", content=raises_for_acme) == ["beta"]

    assert acme.heard == []
    assert [m.value for m in beta.heard] == [{"text": "x"}, {"text": "y"}]
    assert "content for a signal failed" in caplog.text


def test_a_falsy_view_is_still_a_view():
    bus = Bus(acme=["chat.read"])
    ear, _, _ = bus.listen("acme", "chat.state")

    assert bus.bus.emit("chat.state", content=lambda _g: []) == ["acme"]

    assert ear.heard[0].value == []


def test_a_sink_that_raises_is_logged_and_the_others_are_still_told(caplog):
    bus = Bus(acme=["chat.read"], beta=["chat.read"])
    subscriber = bus.bus.attach("acme", BrokenEar())
    bus.bus.subscribe(subscriber, ["ping"])
    beta, _, _ = bus.listen("beta", "ping")

    with caplog.at_level("ERROR"):
        told = bus.bus.emit("ping", {"text": "x"})

    assert told == ["acme", "beta"]
    assert len(beta.heard) == 1
    assert "delivering ping to acme failed" in caplog.text


def test_a_subscriber_that_detaches_while_it_is_told_does_not_upset_the_round():
    bus = Bus(acme=["chat.read"], beta=["chat.read"])

    class Leaves:
        def __init__(self) -> None:
            self.subscriber = None

        def deliver(self, message: SignalMessage) -> None:
            bus.bus.detach(self.subscriber)

    leaves = Leaves()
    leaves.subscriber = bus.bus.attach("acme", leaves)
    bus.bus.subscribe(leaves.subscriber, ["ping"])
    beta, _, _ = bus.listen("beta", "ping")

    assert bus.bus.emit("ping", {"text": "x"}) == ["acme", "beta"]
    assert len(beta.heard) == 1


# ── host.changed ─────────────────────────────────────────────────


def test_a_signal_is_also_told_as_the_change_of_each_property_it_reports():
    bus = Bus(acme=["chat.read", "files.read"])
    ear, _, _ = bus.listen("acme", CHANGED)

    told = bus.bus.emit("chat.state", {"text": "x"})

    assert told == []  # not subscribed to the wire itself
    assert _changes(ear) == [
        {"path": "chat.busy", "value": {"text": "x"}, "window": None},
        {"path": "chat.secret", "value": {"text": "x"}, "window": None},
        {"path": "other.tick", "value": {"text": "x"}, "window": None},
    ]
    assert all(m.signal == CHANGED for m in ear.heard)


def test_a_change_reaches_a_listener_only_for_the_properties_it_may_see():
    bus = Bus(acme=["chat.read"])
    ear, _, _ = bus.listen("acme", CHANGED)

    bus.bus.emit("chat.state", {"text": "x"})

    assert [c["path"] for c in _changes(ear)] == ["chat.busy", "other.tick"]


def test_a_change_of_a_root_property_has_a_bare_path_and_a_window_is_carried():
    bus = Bus(acme=["chat.read"])
    ear, _, _ = bus.listen("acme", CHANGED)

    bus.bus.emit("ping", {"text": "hi"})
    bus.bus.emit("ui.zoomed", {"text": "z"}, window="w2")

    assert _changes(ear) == [
        {"path": "mood", "value": {"text": "hi"}, "window": None},
        {"path": "ui.zoom", "value": {"text": "z"}, "window": "w2"},
    ]
    assert Change(path="a", value=1).model_dump() == {"path": "a", "value": 1, "window": None}


def test_a_change_carries_the_view_the_listener_is_allowed():
    bus = Bus(acme=["chat.read"], beta=["chat.read", "files.read"])
    acme, _, _ = bus.listen("acme", CHANGED)
    beta, _, _ = bus.listen("beta", CHANGED)

    bus.bus.emit(
        "chat.state",
        content=lambda g: {"text": "full" if g.allows("files.read") else "part"},
    )

    assert _changes(acme)[0]["value"] == {"text": "part"}
    assert _changes(beta)[0]["value"] == {"text": "full"}


def test_a_listener_with_no_view_hears_no_change_either():
    bus = Bus(acme=["chat.read"])
    ear, _, _ = bus.listen("acme", CHANGED)

    bus.bus.emit("chat.state", content=lambda _g: None)

    assert ear.heard == []


def test_a_signal_is_told_both_as_itself_and_as_a_change_to_one_that_wants_both():
    bus = Bus(acme=["chat.read"])
    ear, _, _ = bus.listen("acme", CHANGED, "chat.state")

    assert bus.bus.emit("chat.state", {"text": "x"}) == ["acme"]

    assert [m.signal for m in ear.heard] == ["chat.state", CHANGED, CHANGED]


def test_a_change_goes_to_an_addressed_extension_only():
    bus = Bus(acme=["chat.read"], beta=["chat.read"])
    acme, _, _ = bus.listen("acme", CHANGED)
    beta, _, _ = bus.listen("beta", CHANGED)

    bus.bus.emit("chat.state", {"text": "x"}, only="acme")

    assert acme.heard and beta.heard == []


def test_a_property_pointing_at_a_missing_signal_or_object_is_skipped():
    schema = HostSchema.model_construct(
        version="1.0.0",
        permissions=SCHEMA.permissions,
        types=SCHEMA.types,
        objects=(
            SCHEMA.objects[0],
            ObjectSpec(
                path="chat",
                doc="Chat.",
                permission="chat.read",
                properties=(
                    prop("a", "bool", "A.", changed_by=["gone"]),
                    prop("b", "bool", "B.", changed_by=["nowhere.state"]),
                    prop("c", "bool", "C.", changed_by=["state"]),
                ),
                signals=(signal("state", "Note", "State."),),
            ),
        ),
    )
    bus = Bus(schema, acme=["chat.read"])
    ear, _, _ = bus.listen("acme", CHANGED)

    bus.bus.emit("chat.state", {"text": "x"})

    assert [c["path"] for c in _changes(ear)] == ["chat.c"]


# ── wanted ───────────────────────────────────────────────────────


def test_a_signal_is_wanted_only_when_an_entitled_subscriber_would_hear_it():
    bus = Bus(acme=["chat.read"], poor=["files.read"])

    assert bus.bus.wanted("chat.state") is False
    _, poor, _ = bus.listen("poor", "chat.state")
    assert bus.bus.wanted("chat.state") is False
    _, acme, _ = bus.listen("acme", "chat.state")
    assert bus.bus.wanted("chat.state") is True
    assert bus.bus.wanted("ping") is False
    bus.bus.unsubscribe(acme, ["chat.state"])
    assert bus.bus.wanted("chat.state") is False
    bus.bus.detach(poor)


def test_a_change_subscription_makes_every_signal_it_could_report_wanted():
    bus = Bus(acme=["chat.read"])
    _, subscriber, _ = bus.listen("acme", CHANGED)

    assert bus.bus.wanted("chat.state") is True
    bus.bus.detach(subscriber)
    assert bus.bus.wanted("chat.state") is False


def test_an_unknown_wire_is_never_wanted():
    bus = Bus(acme=["chat.read"])
    bus.listen("acme", CHANGED)

    assert bus.bus.wanted("nope") is False


def test_an_uninstalled_subscriber_does_not_make_a_signal_wanted():
    bus = Bus(acme=["chat.read"])
    bus.listen("acme", "ping")
    del bus.granted["acme"]

    assert bus.bus.wanted("ping") is False
