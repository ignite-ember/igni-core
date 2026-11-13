"""``Caller``: who is asking the host model, and where it lives."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.extensions.host.caller import Caller


class Quiet:
    def deliver(self, message: object) -> None:
        return None


def test_a_process_can_subscribe_and_has_no_home_window() -> None:
    subscriber = Quiet()

    caller = Caller.process("acme.reviewer", subscriber)  # type: ignore[arg-type]

    assert caller.extension_id == "acme.reviewer"
    assert caller.subscriber is subscriber, "kept as it was, not copied or validated"
    assert caller.home_window is None


def test_a_page_has_the_window_that_frames_it() -> None:
    subscriber = Quiet()

    caller = Caller.page("acme.reviewer", subscriber, "w-main")  # type: ignore[arg-type]

    assert caller.subscriber is subscriber
    assert caller.home_window == "w-main"


def test_a_page_without_a_window_has_no_home_window() -> None:
    assert Caller.page("acme.reviewer", Quiet(), None).home_window is None  # type: ignore[arg-type]


def test_a_caller_that_cannot_subscribe_has_no_subscriber() -> None:
    caller = Caller(extension_id="acme.reviewer")

    assert caller.subscriber is None and caller.home_window is None


def test_a_caller_cannot_be_changed_once_made() -> None:
    caller = Caller(extension_id="acme.reviewer")

    with pytest.raises(ValidationError):
        caller.home_window = "w-other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        caller.extension_id = "someone.else"  # type: ignore[misc]
