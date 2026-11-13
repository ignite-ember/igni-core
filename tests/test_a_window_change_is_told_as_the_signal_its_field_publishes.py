"""A change to a window's model is told as the ``ui.*`` signal its field publishes.

The person moving a window and an extension writing ``host.ui`` make the
same change, and it reaches subscribers as the same signal: its wire, the
window it is about, and the field's new value. These drive
:class:`WindowSignals` with the changes the model announces and read what
lands on the bus.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.extensions.host.signal_values import Emitter
from igni_core.extensions.host.window_changes import WindowChange
from igni_core.extensions.host.window_signals import (
    PanelChanged,
    Scrolled,
    ToastDismissed,
    ToastShown,
    WindowSignals,
)
from igni_core.extensions.host.window_values import Dialog, Scroll, Toast, WindowInfo


class Bus:
    def __init__(self) -> None:
        self.told: list[tuple[str, Any, str | None]] = []

    def __call__(
        self,
        wire: str,
        value: Any = None,
        *,
        content: Any = None,
        only: Any = None,
        window: Any = None,
    ) -> list[str]:
        self.told.append((wire, value, window))
        return []


class Sessions:
    def __init__(self) -> None:
        self.switched: list[tuple[str, str | None]] = []

    def __call__(self, session_id: str, window: str | None) -> None:
        self.switched.append((session_id, window))


def rig() -> tuple[WindowSignals, Bus, Sessions]:
    bus, sessions = Bus(), Sessions()
    return WindowSignals(Emitter(bus), sessions), bus, sessions


def test_a_page_stack_change_is_told_as_navigated_with_its_pages() -> None:
    signals, bus, _ = rig()
    pages = [{"kind": "plugins", "label": "Plugins", "params": {"a": "1"}}]

    signals.changed(WindowChange(window="w-1", field="route", value=pages, rev=1))

    assert bus.told == [("ui.navigated", {"window": "w-1", "pages": pages}, "w-1")]


@pytest.mark.parametrize(
    ("field", "wire", "value"),
    [
        ("panel", "ui.panel_changed", {"kind": "search", "params": {}}),
        ("sidebar", "ui.sidebar_changed", {"open": True, "width": 300, "section": "links"}),
        ("preview", "ui.preview_changed", {"path": "a.py"}),
        ("selection", "ui.selection_changed", {"message_id": "m", "text": None, "file": None}),
        ("composer", "ui.composer_changed", {"text": "hi", "attachments": [], "mode": "shell"}),
    ],
)
def test_a_field_is_told_under_its_own_name_with_its_new_value_and_window(
    field: str, wire: str, value: dict[str, Any]
) -> None:
    signals, bus, _ = rig()

    signals.changed(WindowChange(window="w-2", field=field, value=value, rev=3))

    assert bus.told == [(wire, {"window": "w-2", field: value}, "w-2")]


@pytest.mark.parametrize(
    ("field", "wire"), [("panel", "ui.panel_changed"), ("preview", "ui.preview_changed")]
)
def test_a_panel_or_preview_that_was_cleared_is_still_told(field: str, wire: str) -> None:
    signals, bus, _ = rig()

    signals.changed(WindowChange(window="w", field=field, value=None, rev=2))

    assert bus.told == [(wire, {"window": "w", field: None}, "w")]


def test_a_panel_told_keeps_what_else_it_carries() -> None:
    told = PanelChanged.of_value("w", {"kind": "search", "query": "x"}).model_dump()

    assert told == {"window": "w", "panel": {"kind": "search", "params": {}, "query": "x"}}


def test_a_scroll_is_told_whole_with_its_extras_and_its_window() -> None:
    told = Scrolled.of_scroll("w", Scroll(message_id="m", at_bottom=False, offset=4)).model_dump()

    assert told == {"message_id": "m", "at_bottom": False, "offset": 4, "window": "w"}


def test_a_toast_shown_or_dismissed_is_told_with_the_toast_and_its_window() -> None:
    signals, bus, _ = rig()
    toast = Toast(id="t-1", text="hi", kind="info")
    plain = toast.model_dump()

    signals.changed(
        WindowChange(
            window="w", field="toasts", value=[plain], rev=1, event="toast_shown", item=toast
        )
    )
    signals.changed(
        WindowChange(
            window="w", field="toasts", value=[], rev=2, event="toast_dismissed", item=toast
        )
    )

    assert bus.told == [
        ("ui.toast_shown", {"window": "w", "toast": plain}, "w"),
        ("ui.toast_dismissed", {"window": "w", "toast": plain}, "w"),
    ]
    assert ToastShown.WIRE != ToastDismissed.WIRE


def test_a_toast_change_that_is_neither_shown_nor_dismissed_is_not_told() -> None:
    signals, bus, _ = rig()

    signals.changed(WindowChange(window="w", field="toasts", value=[], rev=1))
    signals.changed(WindowChange(window="w", field="toasts", value=[], rev=2, event="toast_moved"))

    assert bus.told == []


def test_a_dialog_event_that_is_neither_opened_nor_closed_is_not_told() -> None:
    signals, bus, _ = rig()

    signals.changed(WindowChange(window="w", field="dialog", value=None, rev=1))
    signals.changed(
        WindowChange(
            window="w", field="dialog", value=None, rev=2, event="dialog_moved", item=Dialog(id="d")
        )
    )

    assert bus.told == []


def test_a_dialog_closed_tells_the_answer_it_was_given() -> None:
    signals, bus, _ = rig()
    dialog = Dialog(id="d-1", spec={"title": "ok?"})

    signals.changed(
        WindowChange(
            window="w", field="dialog", rev=1, event="dialog_closed", item=dialog, answer="yes"
        )
    )

    assert bus.told == [
        (
            "ui.dialog_closed",
            {"window": "w", "dialog": dialog.model_dump(), "answer": "yes"},
            "w",
        )
    ]


def test_a_session_change_switches_the_session_and_an_empty_one_tells_nothing() -> None:
    signals, bus, sessions = rig()

    signals.changed(WindowChange(window="w", field="session", value="s-1", rev=1))
    signals.changed(WindowChange(window="w", field="session", value="", rev=2))
    signals.changed(WindowChange(window="w", field="session", value=None, rev=3))

    assert sessions.switched == [("s-1", "w")]
    assert bus.told == []


def test_the_folder_the_focus_and_an_unknown_field_tell_no_ui_signal() -> None:
    signals, bus, sessions = rig()

    signals.changed(WindowChange(window="w", field="project_dir", value="/p", rev=1))
    signals.changed(WindowChange(window="w", field="focused", value=True, rev=2))
    signals.changed(WindowChange(window="w", field="nonsense", value=1, rev=3))

    assert bus.told == []
    assert sessions.switched == []


def test_a_window_opening_closing_or_taking_the_focus_is_told_with_its_info() -> None:
    signals, bus, _ = rig()
    info = WindowInfo(id="w-1", kind="tauri", focused=True, session_id="s-1", project_dir="/p")

    signals.window_moved("opened", info)
    signals.window_moved("closed", info)
    signals.window_moved("focus_changed", info)

    plain = info.model_dump()
    assert bus.told == [
        ("windows.opened", plain, None),
        ("windows.closed", plain, None),
        ("windows.focus_changed", plain, None),
    ]
