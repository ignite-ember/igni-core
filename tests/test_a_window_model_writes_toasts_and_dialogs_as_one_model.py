"""``Windows`` and ``window_model``, driven directly.

The facade over the registry, the state, the fields, the dialog book and
the notifier is held to what a window and an extension see: which writes
are refused and how it says so, which are no change, what a toast and a
dialog hand back, what shutdown does to a waiting dialog, and what each
``window_model`` op answers — including when the request is wrong.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.backend.headless_extensions import RunState
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.window_changes import WindowChange
from igni_core.extensions.host.window_errors import WindowModelError
from igni_core.extensions.host.window_rpc import (
    DialogAnswered,
    DialogShown,
    Hydrated,
    Revised,
    ToastAdded,
    WindowModelRpc,
    WindowRequest,
    Written,
)
from igni_core.extensions.host.window_values import MAX_TOASTS, Dialog, Page, Toast
from igni_core.extensions.host.windows import Windows


class Rig:
    def __init__(self) -> None:
        self.store = RunState()
        self.pushed: list[WindowChange] = []
        self.opened: list[tuple[str, Any]] = []
        self.windows = Windows(self.store, types=SchemaTypes(HOST_SCHEMA), push=self.pushed.append)
        self.windows.on_window(lambda what, info: self.opened.append((what, info)))
        self.rpc = WindowModelRpc(self.windows)

    def ask(self, op: str, window: str = "w-1", **args: Any) -> Any:
        return self.rpc.handle(WindowRequest(op=op, window=window, **args))  # type: ignore[arg-type]


# ── Writing ───────────────────────────────────────────────────────


def test_a_write_the_model_cannot_take_says_why_and_changes_nothing() -> None:
    rig = Rig()

    with pytest.raises(WindowModelError, match="'toasts' changes through its own operations"):
        rig.windows.write("w", "toasts", [])
    with pytest.raises(WindowModelError, match="'dialog' changes through its own operations"):
        rig.windows.write("w", "dialog", None)
    with pytest.raises(WindowModelError, match="no field 'nope'"):
        rig.windows.write("w", "nope", 1)
    with pytest.raises(WindowModelError, match="sidebar: expected Sidebar"):
        rig.windows.write("w", "sidebar", {"open": "maybe"})

    assert rig.pushed == []
    assert rig.windows.rev("w") == 0


def test_a_write_of_the_value_already_there_is_not_a_change() -> None:
    rig = Rig()
    first = rig.windows.write("w", "panel", {"kind": "search"})

    again = rig.windows.write("w", "panel", {"kind": "search"})

    assert (first.rev, again.rev) == (1, 1)
    assert len(rig.pushed) == 1
    assert rig.windows.rev("w") == 1


def test_a_page_stack_written_twice_is_news_both_times() -> None:
    rig = Rig()
    route = [{"kind": "plugins", "label": "Plugins", "params": {}}]

    rig.windows.write("w", "route", route)
    rig.windows.write("w", "route", route)

    assert [c.field for c in rig.pushed] == ["route", "route"]
    assert rig.windows.rev("w") == 2


def test_binding_a_session_announces_the_session_and_then_its_draft() -> None:
    rig = Rig()

    rig.windows.write("w", "session", "s-1", token="tk")

    assert [(c.field, c.rev, c.token) for c in rig.pushed] == [
        ("session", 1, "tk"),
        ("composer", 2, "tk"),
    ]
    assert rig.windows.session("w") == "s-1"


def test_a_session_is_none_for_an_unbound_window_and_after_it_is_cleared() -> None:
    rig = Rig()
    assert rig.windows.session("nobody") is None

    rig.windows.write("w", "session", "s-1")
    rig.windows.write("w", "session", "")

    assert rig.windows.session("w") is None


def test_two_windows_keep_their_own_model_and_revision() -> None:
    rig = Rig()

    rig.windows.write("a", "panel", {"kind": "search"})

    assert rig.windows.get("a", "panel").kind == "search"
    assert rig.windows.get("b", "panel") is None
    assert (rig.windows.rev("a"), rig.windows.rev("b")) == (1, 0)


# ── Toasts ────────────────────────────────────────────────────────


def test_a_toast_is_shown_with_the_hosts_id_and_found_by_it() -> None:
    rig = Rig()

    shown = rig.windows.toast("w", Toast(id="mine", text="hi", kind="info", source="acme"))

    assert shown.id.startswith("t-")
    assert len(shown.id) == 14
    assert shown.id != "mine"
    assert shown.source == "acme"
    assert rig.windows.toast_of("w", shown.id) == shown
    assert rig.windows.toast_of("w", "t-nope") is None
    assert rig.windows.toast_of("other", shown.id) is None


def test_a_toast_with_no_style_is_info_and_its_target_page_is_labelled() -> None:
    rig = Rig()

    shown = rig.windows.toast(
        "w",
        Toast(text="hi", kind="", target=Page(kind="acme.home", params={"id": "1"})),
    )

    assert shown.kind == "info"
    assert shown.target is not None
    assert shown.target.model_dump() == {
        "kind": "acme.home",
        "label": "acme.home",
        "params": {"id": "1"},
    }


def test_a_toast_dismissed_is_the_one_that_went_and_an_unknown_one_is_nothing() -> None:
    rig = Rig()
    shown = rig.windows.toast("w", Toast(text="hi", kind="info"))
    rev = rig.windows.rev("w")

    assert rig.windows.dismiss_toast("w", "t-nope") is None
    assert rig.windows.rev("w") == rev
    assert rig.windows.dismiss_toast("w", shown.id) == shown
    assert rig.windows.dismiss_toast("w", shown.id) is None
    assert rig.windows.get("w", "toasts") == []


def test_the_toast_over_the_cap_pushes_the_oldest_out_and_says_so_first() -> None:
    rig = Rig()
    ids = [rig.windows.toast("w", Toast(text=str(n), kind="info")).id for n in range(MAX_TOASTS)]
    rig.pushed.clear()

    rig.windows.toast("w", Toast(text="last", kind="info"))

    assert [(c.event, c.item.id if c.item else None) for c in rig.pushed][0] == (
        "toast_dismissed",
        ids[0],
    )
    assert [c.event for c in rig.pushed] == ["toast_dismissed", "toast_shown"]
    assert [t.text for t in rig.windows.get("w", "toasts")] == [
        *(str(n) for n in range(1, MAX_TOASTS)),
        "last",
    ]


# ── Dialogs ───────────────────────────────────────────────────────


async def test_a_dialog_is_answered_once_and_the_waiting_future_gets_the_answer() -> None:
    rig = Rig()
    dialog, answer = rig.windows.open_dialog("w", {"title": "ok?"}, source="acme")

    assert dialog.source == "acme"
    assert not answer.done()
    assert rig.windows.answer_dialog("w", dialog.id, "yes") is True
    assert await answer == "yes"
    assert rig.windows.answer_dialog("w", dialog.id, "again") is False
    assert rig.windows.get("w", "dialog") is None


async def test_a_dialog_opened_over_another_closes_the_first_unanswered() -> None:
    rig = Rig()
    first, first_answer = rig.windows.open_dialog("w", "one")

    second, _ = rig.windows.open_dialog("w", "two")

    assert await first_answer is None
    assert [(c.event, c.item.id if c.item else None) for c in rig.pushed if c.event][-3:] == [
        ("dialog_opened", first.id),
        ("dialog_closed", first.id),
        ("dialog_opened", second.id),
    ]
    assert rig.windows.answer_dialog("w", first.id, "late") is False
    assert rig.windows.get("w", "dialog") == second


async def test_shutting_down_leaves_nobody_waiting_on_a_dialog() -> None:
    rig = Rig()
    _, one = rig.windows.open_dialog("a", "one")
    _, two = rig.windows.open_dialog("b", "two")

    rig.windows.cancel_dialogs()

    assert await one is None
    assert await two is None


# ── window_model ──────────────────────────────────────────────────


def test_a_request_with_a_key_it_does_not_know_an_empty_window_or_an_unknown_op_is_refused() -> (
    None
):
    with pytest.raises(ValidationError):
        WindowRequest.model_validate({"op": "hydrate", "window": "w", "extra": 1})
    with pytest.raises(ValidationError):
        WindowRequest.model_validate({"op": "hydrate", "window": ""})
    with pytest.raises(ValidationError):
        WindowRequest.model_validate({"op": "explode", "window": "w"})
    with pytest.raises(ValidationError):
        WindowRequest.model_validate({"op": "hydrate"})


def test_hydrating_opens_the_window_and_answers_its_whole_model() -> None:
    rig = Rig()

    answer = rig.ask("hydrate", connection_id="ws-1", kind="tauri")

    assert isinstance(answer, Hydrated)
    assert answer.op == "hydrate"
    assert (answer.rev, answer.unset) == (0, ["sidebar"])
    assert answer.state.composer.mode == "chat"
    assert rig.windows.registry.known("w-1")
    record = rig.windows.registry.record("w-1")
    assert (record.connection_id, record.kind) == ("ws-1", "tauri")
    assert [(what, info.id) for what, info in rig.opened] == [("opened", "w-1")]


async def test_a_window_that_never_hydrated_is_opened_by_whatever_it_asks_first() -> None:
    for op, args in [
        ("write", {"field": "panel", "value": {"kind": "search"}}),
        ("toast", {"value": {"text": "hi", "kind": "info"}}),
        ("open_dialog", {"value": {"title": "x"}}),
        ("dismiss_toast", {"value": "t-1"}),
        ("answer_dialog", {"dialog_id": "d-1"}),
    ]:
        rig = Rig()
        assert not rig.windows.registry.known("w-1")

        rig.ask(op, **args)

        assert rig.windows.registry.known("w-1"), op
        assert [what for what, _ in rig.opened] == ["opened"], op


def test_a_write_says_its_revision_and_whether_it_changed_anything() -> None:
    rig = Rig()

    first = rig.ask("write", field="panel", value={"kind": "search"}, token="t")
    again = rig.ask("write", field="panel", value={"kind": "search"})

    assert first == Written(rev=1, changed=True)
    assert again == Written(rev=1, changed=False)
    assert [c.token for c in rig.pushed] == ["t"]


def test_a_write_the_model_refuses_is_refused_by_the_rpc() -> None:
    rig = Rig()

    with pytest.raises(WindowModelError, match="no field 'nope'"):
        rig.ask("write", field="nope", value=1)
    with pytest.raises(WindowModelError, match="changes through its own operations"):
        rig.ask("write", field="toasts", value=[])


def test_a_toast_is_answered_with_its_host_id_and_the_revision() -> None:
    rig = Rig()

    answer = rig.ask("toast", value={"text": "hi", "kind": "success", "body": "more"})

    assert isinstance(answer, ToastAdded)
    assert answer.op == "toast"
    assert answer.toast.id.startswith("t-")
    assert (answer.toast.text, answer.toast.kind, answer.toast.body) == ("hi", "success", "more")
    assert answer.rev == rig.windows.rev("w-1") == 1


@pytest.mark.parametrize(
    "value", [None, {}, {"text": "no style"}, "hi", {"text": "t", "kind": "i", "target": "x"}]
)
def test_a_toast_that_is_not_one_is_refused_in_the_models_own_words(value: Any) -> None:
    rig = Rig()

    with pytest.raises(WindowModelError) as refusal:
        rig.ask("toast", value=value)

    assert str(refusal.value).startswith("toast: expected Toast: ")
    assert rig.windows.rev("w-1") == 0


def test_dismissing_a_toast_answers_the_new_revision_and_an_unknown_one_is_no_change() -> None:
    rig = Rig()
    shown = rig.ask("toast", value={"text": "hi", "kind": "info"}).toast

    unknown = rig.ask("dismiss_toast", value="t-nope")
    missing = rig.ask("dismiss_toast", value=None)
    gone = rig.ask("dismiss_toast", value=shown.id)

    assert unknown == Revised(rev=1)
    assert missing == Revised(rev=1)
    assert gone == Revised(rev=2)
    assert gone.op == "dismiss_toast"
    assert rig.windows.get("w-1", "toasts") == []


async def test_opening_and_answering_igni_s_own_dialog() -> None:
    rig = Rig()

    opened = rig.ask("open_dialog", value={"title": "ok?"})
    replaced = rig.ask("open_dialog", value=None)
    stale = rig.ask("answer_dialog", dialog_id=opened.dialog.id, value="yes")
    taken = rig.ask("answer_dialog", dialog_id=replaced.dialog.id, value="no")
    empty = rig.ask("answer_dialog", dialog_id="", value="no")

    assert isinstance(opened, DialogShown)
    assert opened.dialog == Dialog(id=opened.dialog.id, spec={"title": "ok?"}, source=None)
    assert opened.rev == 1
    assert replaced.dialog.spec is None
    assert replaced.rev == 3  # the first dialog's closing is a change between
    assert stale == DialogAnswered(answered=False, rev=3)
    assert taken == DialogAnswered(answered=True, rev=4)
    assert empty == DialogAnswered(answered=False, rev=4)
    assert taken.op == "answer_dialog"


def test_every_reply_says_which_request_it_answers() -> None:
    rig = Rig()

    ops = [
        rig.ask("hydrate").op,
        rig.ask("write", field="panel", value={"kind": "s"}).op,
        rig.ask("toast", value={"text": "t", "kind": "info"}).op,
    ]

    assert ops == ["hydrate", "write", "toast"]
