"""What a window's model holds: the values, their defaults and what they refuse.

``window_values`` are the models a client is handed whole from
``window_model`` and told on every push; ``window_state`` is where they
are kept. These hold each value to the shape it has on the wire: the
fallbacks a page and a composer apply, what a value keeps beyond what it
names, the type that tells a toast from a dialog, and the keys the
lasting fields are stored under.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.core.session.client_state import (
    DELETE_FAILED,
    WRITE_FAILED,
    ClientStateRead,
    ClientStateResult,
)
from igni_core.extensions.host.window_errors import WindowModelError, WindowStateError
from igni_core.extensions.host.window_state import (
    LastingState,
    TransientState,
    UiKeys,
    WindowState,
)
from igni_core.extensions.host.window_values import (
    MAX_TOASTS,
    MODES,
    Composer,
    Dialog,
    Page,
    Panel,
    Preview,
    Scroll,
    Selection,
    Sidebar,
    Toast,
    WindowInfo,
    WindowModelState,
    references,
    text_params,
)

# ── References ────────────────────────────────────────────────────


def test_a_draft_names_each_file_once_in_order_and_only_where_an_at_starts_a_word() -> None:
    text = "see @b.py and x@y.com, @ alone, @a.py @b.py @ü/é.py @code:12 mid@word"

    assert references(text) == ["b.py", "a.py", "ü/é.py"]
    assert references("") == []
    assert references("@") == []


def test_a_reference_at_the_start_of_a_line_counts() -> None:
    assert references("first\n@one.py\n@two.py") == ["one.py", "two.py"]


# ── Pages ─────────────────────────────────────────────────────────


def test_page_parameters_are_text_and_anything_else_is_none() -> None:
    assert text_params({1: 2, "a": None, "b": True}) == {"1": "2", "a": "None", "b": "True"}
    assert text_params(None) == {}
    assert text_params(["a"]) == {}
    assert text_params("a=b") == {}


def test_a_page_is_labelled_with_its_kind_when_it_has_no_label() -> None:
    assert Page.of({"kind": "plugins"}).label == "plugins"
    assert Page.of({"kind": "plugins", "label": ""}).label == "plugins"
    assert Page.of({"kind": "plugins", "label": None}).label == "plugins"
    assert Page.of({"kind": "plugins", "label": "Plugins"}).label == "Plugins"


def test_a_page_keeps_its_parameters_as_text() -> None:
    page = Page.of({"kind": "acme.home", "params": {"id": 7, "on": True}})

    assert page.model_dump() == {
        "kind": "acme.home",
        "label": "acme.home",
        "params": {"id": "7", "on": "True"},
    }
    assert Page.of({"kind": "acme.home", "params": "nope"}).params == {}


@pytest.mark.parametrize("page", [{}, {"label": "x"}, {"kind": 3}, {"kind": None}])
def test_a_page_without_a_text_kind_is_refused(page: dict[str, Any]) -> None:
    with pytest.raises(WindowModelError) as refusal:
        Page.of(page)

    assert "a page has no kind" in str(refusal.value)


def test_a_page_opened_by_navigating_is_labelled_with_its_own_kind() -> None:
    assert Page.named("acme.home", {"id": 1}).model_dump() == {
        "kind": "acme.home",
        "label": "acme.home",
        "params": {"id": "1"},
    }
    assert Page.named("acme.home", None).params == {}


def test_a_page_with_no_parameters_has_none_not_null() -> None:
    assert Page(kind="plugins", params=None).params == {}  # type: ignore[arg-type]


# ── The sidebar, the composer, the scroll ─────────────────────────


def test_a_sidebar_opened_or_closed_keeps_the_width_and_section_not_named() -> None:
    current = Sidebar(open=True, width=300, section="sessions")

    assert Sidebar.merged(current, open=False, width=None, section=None) == Sidebar(
        open=False, width=300, section="sessions"
    )
    assert Sidebar.merged(current, open=True, width=0, section="") == Sidebar(
        open=True, width=0, section=""
    )
    assert Sidebar.merged(Sidebar(), open=True, width=None, section=None) == Sidebar(open=True)


def test_an_attachment_the_draft_does_not_name_is_appended_as_a_reference() -> None:
    composer = Composer.with_attachments("look at", ["a.py", "b.py"], None, None)

    assert composer.text == "look at @a.py @b.py "
    assert composer.mode == "chat"
    # The attachments are the draft's own references, read from its text.
    assert composer.attachments == []


def test_an_attachment_follows_the_text_with_one_space_only_when_it_needs_one() -> None:
    assert Composer.with_attachments("", ["a.py"], None, None).text == "@a.py "
    assert Composer.with_attachments("hi ", ["a.py"], None, None).text == "hi @a.py "
    assert Composer.with_attachments("hi\n", ["a.py"], None, None).text == "hi\n@a.py "
    assert Composer.with_attachments("hi", ["a.py"], None, None).text == "hi @a.py "


def test_an_attachment_the_draft_already_names_is_not_added_again() -> None:
    text = "see @a.py now"

    assert Composer.with_attachments(text, ["a.py"], None, None).text == text
    assert Composer.with_attachments(text, ["a.py", "a.py"], None, None).text == text
    # A pill is not the file: naming the same path after `@code:` still adds it.
    assert Composer.with_attachments("@code:a.py", ["a.py"], None, None).text == "@code:a.py @a.py "


def test_the_composer_mode_is_the_one_named_else_the_one_there_else_chat() -> None:
    assert Composer.with_attachments("x", [], "shell", "command").mode == "shell"
    assert Composer.with_attachments("x", [], None, "command").mode == "command"
    assert Composer.with_attachments("x", [], "", "").mode == "chat"
    assert Composer.with_attachments("x", [], None, None).mode == "chat"


def test_a_draft_with_no_attachments_has_none_not_null() -> None:
    assert Composer(attachments=None).attachments == []  # type: ignore[arg-type]
    assert MODES == ("chat", "command", "shell")


def test_scrolling_to_the_bottom_is_at_the_bottom_and_to_anything_else_is_a_message() -> None:
    assert Scroll.to("bottom") == Scroll(message_id=None, at_bottom=True)
    assert Scroll.to("run-1") == Scroll(message_id="run-1", at_bottom=False)
    assert Scroll.to("Bottom") == Scroll(message_id="Bottom", at_bottom=False)


# ── What a value keeps beyond what it names ───────────────────────


def test_a_panel_a_preview_a_selection_a_scroll_and_a_toast_keep_what_else_they_carry() -> None:
    panel = Panel.model_validate({"kind": "search", "query": "x"})
    preview = Preview.model_validate({"path": "a.py", "line": 3})
    selection = Selection.model_validate({"text": "t", "range": [1, 2]})
    scroll = Scroll.model_validate({"at_bottom": False, "offset": 12})
    toast = Toast.model_validate({"text": "t", "kind": "info", "icon": "x"})

    assert panel.model_dump()["query"] == "x"
    assert preview.model_dump() == {"path": "a.py", "line": 3}
    assert selection.model_dump()["range"] == [1, 2]
    assert scroll.model_dump() == {"message_id": None, "at_bottom": False, "offset": 12}
    assert toast.model_dump()["icon"] == "x"


def test_the_values_with_room_for_more_publish_that_room_as_json_values() -> None:
    for model in (Panel, Preview, Selection, Scroll, Toast):
        schema = model.model_json_schema()
        assert schema["additionalProperties"], model.__name__
        assert schema["additionalProperties"] is not True, model.__name__
    # A dialog and a sidebar take nothing beyond what they name.
    assert "additionalProperties" not in Dialog.model_json_schema()
    assert "additionalProperties" not in Sidebar.model_json_schema()


def test_a_toast_says_it_is_a_toast_and_a_dialog_says_it_is_a_dialog() -> None:
    assert Toast(text="t", kind="info").type == "toast"
    assert Dialog(id="d-1").type == "dialog"
    assert Toast(text="t", kind="info").model_dump()["type"] == "toast"
    assert Dialog(id="d-1").model_dump()["type"] == "dialog"
    with pytest.raises(ValidationError):
        Toast.model_validate({"type": "dialog", "text": "t", "kind": "info"})
    with pytest.raises(ValidationError):
        Dialog.model_validate({"type": "toast", "id": "d-1"})


def test_a_toast_needs_its_text_and_its_style() -> None:
    with pytest.raises(ValidationError):
        Toast.model_validate({})
    with pytest.raises(ValidationError):
        Toast.model_validate({"text": "only text"})


def test_a_window_with_nothing_set_is_closed_empty_and_unfocused() -> None:
    state = WindowModelState()

    assert state.model_dump() == {
        "session": "",
        "project_dir": "",
        "route": [],
        "panel": None,
        "sidebar": {"open": False, "width": None, "section": None},
        "preview": None,
        "selection": {"message_id": None, "text": None, "file": None},
        "composer": {"text": "", "attachments": [], "mode": "chat"},
        "toasts": [],
        "dialog": None,
        "scroll": {"message_id": None, "at_bottom": True},
        "focused": False,
    }
    assert WindowInfo(id="w").model_dump() == {
        "id": "w",
        "kind": "web",
        "connected": True,
        "focused": False,
        "session_id": None,
        "project_dir": None,
    }
    assert MAX_TOASTS == 6


# ── Where the model is kept ───────────────────────────────────────


class Store:
    """A client-state store that records what it is asked, and can fail on a verb."""

    def __init__(self, *, fail: frozenset[str] = frozenset(), ok: bool = True) -> None:
        self.values: dict[str, str] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.fail = fail
        self.ok = ok

    def get_for_client(self, client_id: str) -> ClientStateRead:
        return ClientStateRead(ok=self.ok, values=dict(self.values))

    def set_value(self, client_id: str, key: str, value: str) -> ClientStateResult:
        self.calls.append(("set", key, value))
        if "set" in self.fail:
            return ClientStateResult(ok=False, error=WRITE_FAILED)
        self.values[key] = value
        return ClientStateResult(ok=True)

    def delete_value(self, client_id: str, key: str) -> ClientStateResult:
        self.calls.append(("delete", key, ""))
        if "delete" in self.fail:
            return ClientStateResult(ok=False, error=DELETE_FAILED)
        self.values.pop(key, None)
        return ClientStateResult(ok=True)


def test_each_session_keeps_its_own_draft_and_mode_under_its_own_keys() -> None:
    assert UiKeys.draft("s-1") == "draft:s-1"
    assert UiKeys.mode("s-1") == "draft-mode:s-1"
    assert UiKeys.draft("") == "draft:"
    assert (UiKeys.SESSION, UiKeys.PROJECT_DIR, UiKeys.PAGE) == (
        "session-id",
        "project-dir",
        "page",
    )
    assert (UiKeys.SIDEBAR, UiKeys.SIDEBAR_WIDTH, UiKeys.SIDEBAR_SECTION) == (
        "sidebar-open",
        "sidebar-width",
        "sidebar-section",
    )


def test_a_lasting_value_is_stored_and_an_empty_one_is_deleted() -> None:
    store = Store()
    lasting = LastingState(store)

    lasting.write("w", "page", "x")
    assert lasting.read("w") == {"page": "x"}
    lasting.write("w", "page", "")

    assert store.calls == [("set", "page", "x"), ("delete", "page", "")]
    assert lasting.read("w") == {}


def test_a_state_that_cannot_be_read_is_an_error_and_never_an_empty_state() -> None:
    lasting = LastingState(Store(ok=False))

    with pytest.raises(WindowStateError, match="window 'w' state could not be read"):
        lasting.read("w")


def test_a_state_that_cannot_be_written_or_deleted_is_an_error() -> None:
    lasting = LastingState(Store(fail=frozenset({"set", "delete"})))

    with pytest.raises(WindowStateError, match="window 'w' state could not be written"):
        lasting.write("w", "page", "x")
    with pytest.raises(WindowStateError, match="window 'w' state could not be written"):
        lasting.write("w", "page", "")


def test_each_window_has_its_own_transient_fields_and_keeps_them() -> None:
    transient = TransientState()
    first = transient.of("w-1")

    first.focused = True

    assert transient.of("w-1") is first
    assert transient.of("w-2") is not first
    assert transient.of("w-2").focused is False
    assert transient.of("w-2").scroll == Scroll()


def test_a_window_state_is_what_lasts_beside_what_lives_here() -> None:
    state = WindowState(Store())

    context = state.context("w-1")

    assert context.window_id == "w-1"
    assert context.transient is state.transient.of("w-1")
    assert isinstance(state.lasting, LastingState)
    assert state.context("w-1").transient is context.transient
