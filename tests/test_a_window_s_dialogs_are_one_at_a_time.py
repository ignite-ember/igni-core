"""The dialogs a window has open, and the answers that wait for them."""

from __future__ import annotations

import asyncio

import pytest

from igni_core.extensions.host.window_dialogs import DialogBook


async def test_a_dialog_has_its_own_id_and_an_answer_to_wait_on() -> None:
    book = DialogBook()

    first, answer = book.open("w-1", {"title": "Sure?"}, source="acme.one")
    second, _ = book.open("w-2", {"title": "Again?"})

    assert first.id.startswith("d-")
    assert len(first.id) == len("d-") + 12
    assert first.id != second.id
    assert (first.spec, first.source, second.source) == ({"title": "Sure?"}, "acme.one", None)
    assert not answer.done()


async def test_closing_the_open_dialog_answers_it_and_returns_it() -> None:
    book = DialogBook()
    dialog, answer = book.open("w-1", {})

    closed = book.close("w-1", dialog.id, {"ok": True})

    assert closed == dialog
    assert await answer == {"ok": True}
    # It is no longer open: closing it again is a stale answer.
    assert book.close("w-1", dialog.id, {"ok": False}) is None


async def test_a_window_with_no_dialog_or_another_one_changes_nothing() -> None:
    book = DialogBook()
    assert book.close("w-1", "d-none", None) is None

    dialog, answer = book.open("w-1", {})

    assert book.close("w-1", "d-stale", "late") is None
    assert book.close("w-2", dialog.id, "elsewhere") is None
    assert not answer.done()
    assert book.close("w-1", dialog.id, "now") == dialog
    assert await answer == "now"


async def test_a_dialog_opened_over_another_leaves_the_first_for_its_caller_to_close() -> None:
    book = DialogBook()
    first, first_answer = book.open("w-1", {})
    second, _ = book.open("w-1", {})

    # Only the newer one is open: the older one's id is a stale answer.
    assert book.close("w-1", first.id, "x") is None
    assert not first_answer.done()
    assert book.close("w-1", second.id, "y") == second


async def test_shutting_down_leaves_every_waiting_answer_dismissed() -> None:
    book = DialogBook()
    _, one = book.open("w-1", {})
    dialog, two = book.open("w-2", {})
    book.close("w-2", dialog.id, "answered")

    book.cancel()

    assert one.result() is None
    assert two.result() == "answered"
    book.cancel()  # nothing left to cancel


async def test_an_answer_is_not_set_twice() -> None:
    book = DialogBook()
    dialog, answer = book.open("w-1", {})
    answer.set_result("early")

    assert book.close("w-1", dialog.id, "late") == dialog
    assert answer.result() == "early"


def test_a_dialog_cannot_be_opened_with_no_loop_to_answer_on() -> None:
    with pytest.raises(RuntimeError):
        DialogBook().open("w-1", {})


async def test_a_waiting_answer_can_be_awaited_after_cancel() -> None:
    book = DialogBook()
    _, answer = book.open("w-1", {})
    book.cancel()

    assert await asyncio.wait_for(answer, 1) is None
