"""A change to a window's model is told once: to the window, then to every listener.

The notifier is the one place a change is told. A push or a listener that
fails is logged and does not stop the others, and a window's revision
counts its own changes only.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from igni_core.extensions.host.window_changes import ChangeNotifier, WindowChange
from igni_core.extensions.host.window_values import Dialog, Toast, WindowInfo


def _notifier(told: list[str]) -> ChangeNotifier:
    return ChangeNotifier(lambda change: told.append(f"push:{change.window}:{change.rev}"))


def test_the_window_is_told_before_the_listeners_and_in_the_order_they_came() -> None:
    told: list[str] = []
    notifier = _notifier(told)
    notifier.on_change(lambda change: told.append(f"first:{change.field}"))
    notifier.on_change(lambda change: told.append(f"second:{change.field}"))

    change = notifier.announce("w-1", "route", [], token="t1")

    assert told == ["push:w-1:1", "first:route", "second:route"]
    assert (change.window, change.field, change.value, change.rev, change.token) == (
        "w-1",
        "route",
        [],
        1,
        "t1",
    )


def test_a_window_s_revision_counts_its_own_changes_and_an_unknown_window_has_none() -> None:
    notifier = _notifier([])

    assert notifier.rev("w-1") == 0
    notifier.announce("w-1", "route", [])
    notifier.announce("w-1", "focused", True)
    notifier.announce("w-2", "focused", True)

    assert (notifier.rev("w-1"), notifier.rev("w-2"), notifier.rev("w-3")) == (2, 1, 0)


def test_a_push_that_fails_is_logged_and_the_listeners_still_hear_the_change(
    caplog: pytest.LogCaptureFixture,
) -> None:
    heard: list[int] = []

    def push(change: WindowChange) -> None:
        raise RuntimeError("the window is gone")

    notifier = ChangeNotifier(push)
    notifier.on_change(lambda change: heard.append(change.rev))

    with caplog.at_level(logging.ERROR):
        change = notifier.announce("w-1", "focused", True)

    assert heard == [1]
    assert change.rev == 1
    assert "pushing a window change failed" in caplog.text


def test_a_listener_that_fails_is_logged_and_the_later_ones_still_hear_the_change(
    caplog: pytest.LogCaptureFixture,
) -> None:
    heard: list[str] = []

    def broken(change: WindowChange) -> None:
        raise RuntimeError("bad listener")

    notifier = _notifier(heard)
    notifier.on_change(broken)
    notifier.on_change(lambda change: heard.append("later"))

    with caplog.at_level(logging.ERROR):
        notifier.announce("w-1", "focused", True)

    assert heard == ["push:w-1:1", "later"]
    assert "a window-change listener failed" in caplog.text


def test_a_window_moving_is_told_to_every_window_listener_even_when_one_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    heard: list[tuple[str, str]] = []

    def broken(what: str, window: WindowInfo) -> None:
        raise RuntimeError("bad listener")

    notifier = _notifier([])
    notifier.on_window(broken)
    notifier.on_window(lambda what, window: heard.append((what, window.id)))

    with caplog.at_level(logging.ERROR):
        notifier.window_moved("closed", WindowInfo(id="w-1"))

    assert heard == [("closed", "w-1")]
    assert "a window listener failed" in caplog.text


def test_a_toast_or_a_dialog_travels_with_its_event_and_a_dismissed_dialog_has_no_answer() -> None:
    notifier = _notifier([])
    dialog = Dialog(id="d-1", spec={"title": "Sure?"})

    shown = notifier.announce(
        "w-1", "toasts", [], event="toast_shown", item=Toast(text="Saved", kind="success")
    )
    closed = notifier.announce(
        "w-1", "dialog", None, event="dialog_closed", item=dialog, answer=None
    )

    assert shown.event == "toast_shown"
    assert isinstance(shown.item, Toast)
    assert closed.item == dialog
    assert closed.answer is None
    assert WindowChange(window="w-1", field="route", rev=1).token == ""


def test_a_change_names_its_window_field_and_revision() -> None:
    with pytest.raises(ValidationError):
        WindowChange.model_validate({"field": "route", "rev": 1})
    with pytest.raises(ValidationError):
        WindowChange.model_validate({"window": "w-1", "field": "route", "rev": "next"})
    assert WindowChange(window="w-1", field="route", rev=1).value is None
