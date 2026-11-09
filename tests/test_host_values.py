"""The host's typed values keep the shapes and coercions the members always sent.

Each projection (``from_entry``, ``from_status``, ``from_row``, ...) is
the old hand-built dict, moved onto the model it now makes: these hold
it to the same defaults and the same ``or 0`` / ``or ""`` coercions. The
window tables are held to every op and every signalled field.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any, get_args

from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.signal_values import Emitter
from igni_core.extensions.host.signals import Content
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.values import (
    Account,
    CommandOutcome,
    NotificationCounts,
    NotificationEntry,
    SessionInfo,
    SessionStatus,
    UpdateInfo,
)
from igni_core.extensions.host.window_rpc import WindowModelRpc
from igni_core.extensions.host.window_signals import WindowSignals
from igni_core.extensions.host.windows import FIELDS, WindowChange, WindowRequest, Windows
from tests.host_model_helpers import manifest


def _grants(extension_id: str, *, everything: bool) -> HostGrants:
    host = ["notifications"] if everything else ["chat.read"]
    return HostGrants(manifest(extension_id, host=host))


class _Bus:
    def __init__(self) -> None:
        self.told: list[tuple[str, Any]] = []

    def __call__(
        self,
        wire: str,
        value: Any = None,
        *,
        content: Content | None = None,
        only: str | None = None,
        window: str | None = None,
    ) -> list[str]:
        self.told.append((wire, value))
        return []


# ── Values ────────────────────────────────────────────────────────


def test_a_session_entry_with_nothing_in_it_is_all_defaults():
    assert SessionInfo.from_entry({}).model_dump() == {
        "session_id": "",
        "name": "",
        "created_at": 0,
        "updated_at": 0,
        "run_count": 0,
        "summary": "",
        "agent_name": "",
    }


def test_a_session_entry_s_counts_are_numbers_and_none_is_zero():
    info = SessionInfo.from_entry(
        {"session_id": "s1", "created_at": "12", "updated_at": None, "run_count": 3}
    )

    assert (info.created_at, info.updated_at, info.run_count) == (12, 0, 3)


def test_a_status_defaults_to_a_configured_model_in_default_mode():
    status = SessionStatus.from_status({}, "boot")

    assert status.model_dump() == {
        "session_id": "boot",
        "model": "",
        "model_configured": True,
        "permission_mode": "default",
        "input_tokens": 0,
        "output_tokens": 0,
        "context_tokens": 0,
        "max_context": 0,
    }
    assert SessionStatus.from_status({"model_configured": False}, "boot").model_configured is False


def test_an_account_is_signed_in_with_an_email_or_a_connection():
    assert Account.from_status({}).model_dump() == {
        "signed_in": False,
        "email": "",
        "name": "",
        "org": "",
    }
    assert Account.from_status({"cloud_email": "a@b.c"}).signed_in
    assert Account.from_status({"cloud_connected": True, "cloud_name": None}).model_dump() == {
        "signed_in": True,
        "email": "",
        "name": "",
        "org": "",
    }


def test_a_command_answer_reads_only_the_fields_it_has():
    assert CommandOutcome.from_answer(object()).model_dump() == {
        "kind": "info",
        "content": "",
        "display_content": "",
    }
    answer = SimpleNamespace(kind="error", content="no such style")
    assert CommandOutcome.from_answer(answer).kind == "error"


def test_an_update_is_there_only_when_one_is_available():
    found = {
        "available": True,
        "current_version": "1.0",
        "latest_version": "1.1",
        "download_url": "https://igni.example/1.1",
    }

    assert UpdateInfo.from_check({**found, "available": False}) is None
    assert UpdateInfo.from_check(None) is None
    assert UpdateInfo.from_check(found) == UpdateInfo(
        current_version="1.0", latest_version="1.1", download_url="https://igni.example/1.1"
    )


def test_a_notification_s_time_is_iso_when_it_is_a_time_and_as_given_otherwise():
    at = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)

    assert NotificationEntry.from_row({"created_at": at}).created_at == at.isoformat()
    assert NotificationEntry.from_row({"created_at": 1700000000}).created_at == 1700000000
    assert NotificationEntry.from_row({}).created_at is None


def test_a_notification_keeps_an_object_target_and_drops_anything_else():
    target = {"page": "acme.home", "params": {"id": "1"}, "extra": True}

    assert NotificationEntry.from_row({"target": target}).target == target
    assert NotificationEntry.from_row({"target": "acme.home"}).target is None
    assert NotificationEntry.from_row({"actor": None, "read": 1}).model_dump()["actor"] == ""


def test_counts_are_all_of_them_with_the_grant_and_its_own_without():
    counts = NotificationCounts.from_counts(
        {"unread": None, "by_source": {"acme.one": "2", "acme.two": 3}}
    )

    assert counts.model_dump() == {"unread": 0, "by_source": {"acme.one": 2, "acme.two": 3}}
    assert counts.visible_to(_grants("acme.one", everything=True)) is counts
    assert counts.visible_to(_grants("acme.one", everything=False)).model_dump() == {
        "unread": 2,
        "by_source": {"acme.one": 2},
    }
    assert counts.visible_to(_grants("acme.none", everything=False)).model_dump() == {
        "unread": 0,
        "by_source": {},
    }


# ── The window tables ─────────────────────────────────────────────


def test_every_window_model_op_is_answered():
    ops = set(get_args(WindowRequest.model_fields["op"].annotation))

    windows = Windows(None, types=SchemaTypes(HOST_SCHEMA), push=lambda change: None)

    assert WindowModelRpc(windows).ops() == ops


def test_every_window_field_but_the_folder_and_the_focus_is_signalled():
    signals = WindowSignals(Emitter(_Bus()), lambda session_id, window: None)

    assert signals.fields() == set(FIELDS) - {"project_dir", "focused"}


def test_a_dialog_opened_carries_no_answer_and_a_dialog_closed_always_does():
    bus = _Bus()
    signals = WindowSignals(Emitter(bus), lambda session_id, window: None)
    dialog = {"id": "d-1", "spec": {}, "source": None}

    signals.changed(
        WindowChange(window="w", field="dialog", rev=1, event="dialog_opened", item=dialog)
    )
    signals.changed(
        WindowChange(window="w", field="dialog", rev=2, event="dialog_closed", item=dialog)
    )

    assert bus.told == [
        ("ui.dialog_opened", {"window": "w", "dialog": dialog}),
        ("ui.dialog_closed", {"window": "w", "dialog": dialog, "answer": None}),
    ]


def test_a_scroll_is_told_whole_with_its_window():
    bus = _Bus()
    signals = WindowSignals(Emitter(bus), lambda session_id, window: None)
    scroll = {"message_id": None, "at_bottom": True, "offset": 12}

    signals.changed(WindowChange(window="w", field="scroll", value=scroll, rev=1))

    assert bus.told == [("ui.scrolled", {**scroll, "window": "w"})]
