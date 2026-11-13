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

import pytest

from igni_core.backend.workspace_watch import Head
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.signal_values import Emitter
from igni_core.extensions.host.signals import Content
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.values import (
    Account,
    CommandOutcome,
    ExtensionInfo,
    HeadInfo,
    NotificationCounts,
    NotificationEntry,
    SessionInfo,
    SessionStatus,
    UpdateInfo,
    Withheld,
)
from igni_core.extensions.host.window_changes import WindowChange
from igni_core.extensions.host.window_field import WindowFieldName
from igni_core.extensions.host.window_rpc import WindowModelRpc, WindowRequest
from igni_core.extensions.host.window_signals import WindowSignals
from igni_core.extensions.host.window_values import Dialog, Scroll
from igni_core.extensions.host.windows import Windows
from igni_core.extensions.impact import SwitchedOff
from igni_core.extensions.registry import ExtensionState, InstalledExtension
from igni_core.protocol import messages as msg
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


def test_a_typed_status_update_is_copied_field_for_field_to_the_status_and_the_account():
    update = msg.StatusUpdate(
        input_tokens=5,
        output_tokens=6,
        context_tokens=7,
        max_context=8,
        model="m-1",
        model_configured=False,
        cloud_connected=True,
        cloud_org="acme",
        cloud_email="a@b.c",
        cloud_name="Ada é",
        permission_mode="plan",
    )

    assert SessionStatus.from_update(update).model_dump() == {
        "session_id": "",
        "model": "m-1",
        "model_configured": False,
        "permission_mode": "plan",
        "input_tokens": 5,
        "output_tokens": 6,
        "context_tokens": 7,
        "max_context": 8,
    } | {"session_id": update.session_id}
    assert Account.from_update(update).model_dump() == {
        "signed_in": True,
        "email": "a@b.c",
        "name": "Ada é",
        "org": "acme",
    }


def test_a_typed_account_is_signed_in_with_an_email_or_a_connection_alone():
    assert Account.from_update(msg.StatusUpdate()).signed_in is False
    assert Account.from_update(msg.StatusUpdate(cloud_email="a@b.c")).signed_in is True
    assert Account.from_update(msg.StatusUpdate(cloud_connected=True)).model_dump() == {
        "signed_in": True,
        "email": "",
        "name": "",
        "org": "",
    }
    assert Account.from_status({"cloud_connected": True, "cloud_email": ""}).signed_in is True
    assert Account.from_status({"cloud_email": None}).signed_in is False


def test_a_git_head_is_told_by_its_commit_and_its_branch():
    assert HeadInfo.of(Head(commit_sha="abc123", branch="HEAD")).model_dump() == {
        "commit_sha": "abc123",
        "branch": "HEAD",
    }


def test_an_available_update_with_a_missing_field_is_an_error_not_a_half_answer():
    for missing in ("current_version", "latest_version", "download_url"):
        found = {
            "available": True,
            "current_version": "1.0",
            "latest_version": "1.1",
            "download_url": "https://igni.example/1.1",
        }
        del found[missing]

        with pytest.raises(KeyError, match=missing):
            UpdateInfo.from_check(found)
    assert UpdateInfo.from_check({"current_version": "1.0"}) is None
    assert UpdateInfo.from_check([("available", True)]) is None


def test_a_claim_that_is_withheld_says_why():
    assert Withheld.of(SwitchedOff(claim="tool:shell", reason="acme.one has it first")) == Withheld(
        claim="tool:shell", reason="acme.one has it first"
    )


def test_an_installed_extension_is_told_with_its_state_its_reason_and_what_it_lacks():
    entry = InstalledExtension(
        manifest=manifest("acme.one", host=["chat.read"]),
        state=ExtensionState.FAILED,
        reason="licence expired",
        switched_off=[SwitchedOff(claim="tool:a", reason="taken")],
        dropped=[SwitchedOff(claim="edit:b", reason="no section")],
        faulty="timed out",
    )

    info = ExtensionInfo.from_entry(entry, "http://127.0.0.1:1/x")

    assert info.model_dump() == {
        "id": "acme.one",
        "name": "acme.one",
        "version": "1.0.0",
        "state": "FAILED",
        "reason": "licence expired",
        "web_url": "http://127.0.0.1:1/x",
        "switched_off": [{"claim": "tool:a", "reason": "taken"}],
        "dropped": [{"claim": "edit:b", "reason": "no section"}],
        "faulty": "timed out",
    }
    plain_one = ExtensionInfo.from_entry(
        InstalledExtension(manifest=manifest("acme.two"), state=ExtensionState.READY), None
    )
    assert (plain_one.state, plain_one.reason, plain_one.web_url) == ("READY", "", None)
    assert (plain_one.switched_off, plain_one.dropped, plain_one.faulty) == ([], [], "")


def test_counts_with_no_sources_are_empty_and_a_count_that_is_not_a_number_is_refused():
    assert NotificationCounts.from_counts({}).model_dump() == {"unread": 0, "by_source": {}}
    assert NotificationCounts.from_counts({"unread": "4", "by_source": None}).unread == 4
    with pytest.raises(ValueError):
        NotificationCounts.from_counts({"by_source": {"acme": "many"}})


def test_a_session_entry_with_names_in_any_script_keeps_them_as_text():
    info = SessionInfo.from_entry({"session_id": 7, "name": "Ünï ✓"})

    assert (info.session_id, info.name, info.summary) == ("7", "Ünï ✓", "")


# ── The window tables ─────────────────────────────────────────────


def test_every_window_model_op_is_answered():
    ops = set(get_args(WindowRequest.model_fields["op"].annotation))

    windows = Windows(None, types=SchemaTypes(HOST_SCHEMA), push=lambda change: None)

    assert WindowModelRpc(windows).ops() == ops


def test_every_window_field_but_the_folder_and_the_focus_is_signalled():
    signals = WindowSignals(Emitter(_Bus()), lambda session_id, window: None)

    assert signals.fields() == {name.value for name in WindowFieldName} - {
        "project_dir",
        "focused",
    }


def test_a_dialog_opened_carries_no_answer_and_a_dialog_closed_always_does():
    bus = _Bus()
    signals = WindowSignals(Emitter(bus), lambda session_id, window: None)
    dialog = Dialog(id="d-1", spec={}, source=None)

    signals.changed(
        WindowChange(window="w", field="dialog", rev=1, event="dialog_opened", item=dialog)
    )
    signals.changed(
        WindowChange(window="w", field="dialog", rev=2, event="dialog_closed", item=dialog)
    )

    # The dialog says what it is (`type`), so a client can tell a dialog
    # from a toast without reading the event's name.
    assert bus.told == [
        (
            "ui.dialog_opened",
            {
                "window": "w",
                "dialog": {"type": "dialog", "id": "d-1", "spec": {}, "source": None},
            },
        ),
        (
            "ui.dialog_closed",
            {
                "window": "w",
                "dialog": {"type": "dialog", "id": "d-1", "spec": {}, "source": None},
                "answer": None,
            },
        ),
    ]


def test_a_scroll_is_told_whole_with_its_window():
    bus = _Bus()
    signals = WindowSignals(Emitter(bus), lambda session_id, window: None)
    scroll = Scroll(message_id=None, at_bottom=True, offset=12)

    signals.changed(WindowChange(window="w", field="scroll", value=scroll, rev=1))

    assert bus.told == [("ui.scrolled", {**scroll.model_dump(), "window": "w"})]
