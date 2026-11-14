"""``host.ui``'s members and the vocabulary they name (``ui_values``): what a
call writes into a window's model, what it answers, and each refusal's code
and words.

The members run over the backend as the app assembles it
(:class:`tests.host_model_helpers.HostRig`) with a real window connected,
so a refusal is what an extension is told and a value is what the window
holds.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.ui_values import (
    DialogButton,
    DialogInput,
    DialogSpec,
    ExtensionDialog,
    PanelKind,
    ToastKind,
)
from igni_core.extensions.host.spec import ErrorCode
from tests.host_model_helpers import HostRig


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def ok(reply: Any) -> Any:
    assert reply.ok, f"refused: {reply.error}"
    return reply.result


def refused(reply: Any, code: str) -> str:
    assert not reply.ok, f"answered {reply.result!r}"
    assert reply.error.code == code, reply.error
    return reply.error.message


class TestWithNoWindow:
    @pytest.mark.parametrize(
        ("op", "member", "params"),
        [
            ("get", "ui.route", {}),
            ("call", "ui.navigate", {"page": "plugins"}),
            ("call", "ui.back", {}),
            ("call", "ui.close_panel", {}),
            ("call", "ui.toast", {"text": "hi"}),
            ("call", "ui.dismiss_toast", {"toast_id": "t"}),
            ("call", "ui.show_dialog", {"spec": {"title": "x"}}),
        ],
    )
    async def test_a_member_that_needs_a_window_says_none_is_open(
        self, rig: HostRig, op: str, member: str, params: dict[str, Any]
    ) -> None:
        ext = rig.install(host=["ui"])
        assert refused(await rig.ask(ext, op, member, **params), "not_found") == (
            "no igni window is open"
        )


class TestNavigation:
    async def test_navigate_replaces_the_route_with_the_page_named_and_its_text_params(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        got = ok(await rig.ask(ext, "call", "ui.navigate", page="plugins", params={"n": "3"}))
        assert got == [{"kind": "plugins", "label": "plugins", "params": {"n": "3"}}]
        again = ok(await rig.ask(ext, "call", "ui.navigate", page="extensions"))
        assert again == [{"kind": "extensions", "label": "extensions", "params": {}}]
        assert ok(await rig.ask(ext, "get", "ui.route")) == again

    async def test_back_drops_the_last_entry_and_answers_the_route_left(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        ok(
            await rig.ask(
                ext,
                "set",
                "ui.route",
                to=[
                    {"kind": "plugins", "label": "Plugins"},
                    {"kind": "extensions", "label": "Extensions"},
                ],
            )
        )
        left = ok(await rig.ask(ext, "call", "ui.back"))
        assert [p["kind"] for p in left] == ["plugins"]
        assert ok(await rig.ask(ext, "call", "ui.back")) == []

    async def test_back_on_an_empty_route_is_an_empty_route(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        assert ok(await rig.ask(ext, "call", "ui.back")) == []

    async def test_setting_the_route_to_nothing_empties_it(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        ok(await rig.ask(ext, "call", "ui.navigate", page="plugins"))
        ok(await rig.ask(ext, "set", "ui.route", to=[]))
        assert ok(await rig.ask(ext, "get", "ui.route")) == []

    async def test_a_panel_opens_with_its_params_and_closes(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        opened = ok(await rig.ask(ext, "call", "ui.open_panel", kind="search", params={"q": "x"}))
        assert opened == {"kind": "search", "params": {"q": "x"}}
        assert ok(await rig.ask(ext, "call", "ui.close_panel")) is None
        assert ok(await rig.ask(ext, "get", "ui.panel")) is None

    async def test_a_panel_that_is_not_one_is_refused_listing_the_panels_sorted(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        message = refused(await rig.ask(ext, "call", "ui.open_panel", kind="cupboard"), "invalid")
        assert message == "no panel 'cupboard': one of dir-picker, login, notifications, search"

    async def test_a_sidebar_keeps_what_the_call_does_not_name(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        first = ok(
            await rig.ask(ext, "call", "ui.set_sidebar", open=True, width=320, section="links")
        )
        assert first == {"open": True, "width": 320, "section": "links"}
        closed = ok(await rig.ask(ext, "call", "ui.set_sidebar", open=False))
        assert closed == {"open": False, "width": 320, "section": "links"}
        assert ok(await rig.ask(ext, "get", "ui.sidebar")) == closed


class TestContent:
    async def test_the_preview_opens_and_closes(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"], workspace="read")
        await rig.window("w-1")
        assert ok(await rig.ask(ext, "call", "ui.open_preview", path="a.md")) == {"path": "a.md"}
        assert ok(await rig.ask(ext, "call", "ui.close_preview")) is None
        assert ok(await rig.ask(ext, "get", "ui.preview")) is None

    async def test_a_preview_set_outside_the_grant_is_refused_and_leaves_the_window_alone(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["ui"], workspace="read", workspacePaths=["docs/*"])
        await rig.window("w-1")
        ok(await rig.ask(ext, "set", "ui.preview", to={"path": "docs/a.md"}))
        message = refused(
            await rig.ask(ext, "set", "ui.preview", to={"path": "src/x.py"}), "refused"
        )
        assert "src/x.py" in message
        assert ok(await rig.ask(ext, "get", "ui.preview")) == {"path": "docs/a.md"}

    async def test_a_preview_set_to_nothing_clears_it_with_no_workspace_grant_at_all(
        self, rig: HostRig
    ) -> None:
        granted = rig.install("acme.granted", host=["ui"], workspace="read")
        blind = rig.install("acme.blind", host=["ui"])
        await rig.window("w-1")
        ok(await rig.ask(granted, "call", "ui.open_preview", path="a.md"))
        ok(await rig.ask(blind, "set", "ui.preview", to=None))
        assert ok(await rig.ask(blind, "get", "ui.preview")) is None

    async def test_the_selection_is_set_and_read_back(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        picked = ok(await rig.ask(ext, "call", "ui.select", selection={"text": "this"}))
        assert picked["text"] == "this"
        assert ok(await rig.ask(ext, "get", "ui.selection")) == picked

    async def test_scrolling_to_a_message_is_the_scroll_position(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        got = ok(await rig.ask(ext, "call", "ui.scroll_to", message_id="run-9"))
        assert got["message_id"] == "run-9"
        assert ok(await rig.ask(ext, "get", "ui.scroll")) == got

    async def test_a_draft_names_each_attachment_once_and_keeps_the_mode_there(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1", session="boot")
        ok(await rig.ask(ext, "set", "ui.composer", to={"text": "", "mode": "shell"}))
        kept = ok(
            await rig.ask(ext, "call", "ui.set_composer", text="look", attachments=["a.py", "a.py"])
        )
        assert kept == {"text": "look @a.py ", "attachments": ["a.py"], "mode": "shell"}

    @pytest.mark.parametrize("mode", [None, ""])
    async def test_no_or_empty_mode_keeps_the_current_one(self, rig: HostRig, mode: Any) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1", session="boot")
        ok(await rig.ask(ext, "set", "ui.composer", to={"text": "", "mode": "command"}))
        got = ok(await rig.ask(ext, "call", "ui.set_composer", text="/he", mode=mode))
        assert got["mode"] == "command"

    async def test_a_mode_named_replaces_the_current_one(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1", session="boot")
        ok(await rig.ask(ext, "set", "ui.composer", to={"text": "", "mode": "shell"}))
        got = ok(await rig.ask(ext, "call", "ui.set_composer", text="x", mode="command"))
        assert got["mode"] == "command"

    async def test_a_draft_with_unicode_text_is_kept_as_written(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1", session="boot")
        got = ok(await rig.ask(ext, "call", "ui.set_composer", text="naïve 日本語 🙂"))
        assert got["text"] == "naïve 日本語 🙂"

    async def test_a_draft_with_no_text_is_a_missing_parameter(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        reply = await rig.ask(ext, "call", "ui.set_composer")
        assert not reply.ok


class TestARefusedWrite:
    async def test_a_value_the_window_model_cannot_hold_is_invalid_in_its_own_words(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1", session="boot")
        refused(await rig.ask(ext, "call", "ui.set_composer", text="x", mode="loud"), "invalid")
        refused(await rig.ask(ext, "call", "ui.set_sidebar", open=True, width=5000), "invalid")
        assert ok(await rig.ask(ext, "get", "ui.composer"))["mode"] == "chat"

    async def test_a_draft_for_a_window_showing_no_session_is_invalid_and_says_so(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        message = refused(await rig.ask(ext, "call", "ui.set_composer", text="x"), "invalid")
        assert message == "window w-1 shows no session yet: no draft to write"


class TestNotices:
    async def test_a_toast_with_no_kind_is_info_and_carries_its_source_and_body(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        toast = ok(await rig.ask(ext, "call", "ui.toast", text="Indexed", body="3 files"))
        assert (toast["text"], toast["kind"], toast["body"], toast["source"]) == (
            "Indexed",
            "info",
            "3 files",
            ext,
        )
        assert toast["target"] is None

    @pytest.mark.parametrize("kind", ["info", "success", "warning", "error"])
    async def test_each_toast_kind_is_kept(self, rig: HostRig, kind: str) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        assert ok(await rig.ask(ext, "call", "ui.toast", text="x", kind=kind))["kind"] == kind

    async def test_a_toast_kind_that_is_not_one_lists_the_kinds_in_order(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        message = refused(await rig.ask(ext, "call", "ui.toast", text="x", kind="loud"), "invalid")
        assert message == "toast kind 'loud': one of info, success, warning, error"
        assert ok(await rig.ask(ext, "get", "ui.toasts")) == []

    async def test_a_toast_target_is_a_page_labelled_with_its_kind(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        toast = ok(
            await rig.ask(
                ext, "call", "ui.toast", text="x", target={"kind": "plugins", "label": "Plugins"}
            )
        )
        assert toast["target"] == {"kind": "plugins", "label": "Plugins", "params": {}}

    async def test_a_toast_with_unicode_text_is_kept_as_written(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        assert ok(await rig.ask(ext, "call", "ui.toast", text="héllo 🙂"))["text"] == "héllo 🙂"

    async def test_dismissing_an_unknown_toast_is_not_found_naming_it(self, rig: HostRig) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        message = refused(
            await rig.ask(ext, "call", "ui.dismiss_toast", toast_id="t-nope"), "not_found"
        )
        assert message == "no toast 't-nope' is on screen"

    async def test_another_extensions_toast_is_refused_in_words_and_stays_on_screen(
        self, rig: HostRig
    ) -> None:
        one = rig.install("acme.one", host=["ui"])
        two = rig.install("acme.two", host=["ui"])
        await rig.window("w-1")
        toast = ok(await rig.ask(one, "call", "ui.toast", text="mine"))
        message = refused(
            await rig.ask(two, "call", "ui.dismiss_toast", toast_id=toast["id"]), "refused"
        )
        assert message == "an extension dismisses only its own toasts"
        assert [t["id"] for t in ok(await rig.ask(one, "get", "ui.toasts"))] == [toast["id"]]

    @pytest.mark.parametrize(
        "spec",
        [
            {},
            {"title": ""},
            {"title": "x", "buttons": []},
            {"title": "x", "buttons": [{"id": str(n), "label": "b"} for n in range(5)]},
            {"title": "x", "extra": 1},
            {"title": "x", "buttons": [{"id": "", "label": "b"}]},
            {"title": "x", "buttons": [{"id": "a", "label": ""}]},
            {"title": "x", "buttons": [{"id": "a", "label": "b", "color": "red"}]},
            {"title": "x", "input": {"label": "l", "other": 1}},
            "just text",
        ],
    )
    async def test_a_dialog_spec_that_does_not_fit_is_invalid_and_opens_nothing(
        self, rig: HostRig, spec: Any
    ) -> None:
        ext = rig.install(host=["ui"])
        await rig.window("w-1")
        # Bounded: a spec wrongly accepted would open a dialog and wait for
        # an answer that never comes.
        reply = await asyncio.wait_for(rig.ask(ext, "call", "ui.show_dialog", spec=spec), 5)
        message = refused(reply, "invalid")
        assert message.startswith("ui.show_dialog(spec): ")
        assert ok(await rig.ask(ext, "get", "ui.dialog")) is None


class TestTheVocabulary:
    def test_the_panels_are_these_four(self) -> None:
        assert {k.value for k in PanelKind} == {"notifications", "search", "login", "dir-picker"}

    def test_the_toast_kinds_are_these_four_in_order(self) -> None:
        assert [k.value for k in ToastKind] == ["info", "success", "warning", "error"]

    @pytest.mark.parametrize(
        ("kind", "value", "sentence"),
        [
            (PanelKind, "x", "no panel 'x': one of dir-picker, login, notifications, search"),
            (ToastKind, "", "toast kind '': one of info, success, warning, error"),
        ],
    )
    def test_an_unknown_kind_is_refused_as_invalid_in_its_own_sentence(
        self, kind: Any, value: str, sentence: str
    ) -> None:
        with pytest.raises(HostModelError) as caught:
            kind.of(value)
        assert (caught.value.code, caught.value.message) == (ErrorCode.INVALID, sentence)

    def test_a_known_kind_is_the_member(self) -> None:
        assert PanelKind.of("dir-picker") is PanelKind.DIR_PICKER
        assert ToastKind.of("warning") is ToastKind.WARNING

    def test_a_spec_with_a_title_alone_has_one_primary_ok_button_and_no_input(self) -> None:
        spec = DialogSpec.model_validate({"title": "Sure?"})
        assert spec.body == "" and spec.input is None
        assert spec.buttons == [DialogButton(id="ok", label="OK", primary=True)]

    def test_the_default_button_is_not_shared_between_specs(self) -> None:
        one = DialogSpec(title="a")
        two = DialogSpec(title="b")
        one.buttons[0].label = "Changed"
        assert two.buttons[0].label == "OK"

    @pytest.mark.parametrize("count", [1, 4])
    def test_one_to_four_buttons_are_allowed(self, count: int) -> None:
        buttons = [{"id": str(n), "label": "b"} for n in range(count)]
        assert len(DialogSpec.model_validate({"title": "x", "buttons": buttons}).buttons) == count

    @pytest.mark.parametrize("count", [0, 5])
    def test_no_buttons_and_five_are_not(self, count: int) -> None:
        buttons = [{"id": str(n), "label": "b"} for n in range(count)]
        with pytest.raises(ValidationError):
            DialogSpec.model_validate({"title": "x", "buttons": buttons})

    def test_an_input_has_empty_defaults_and_a_button_is_not_primary_by_default(self) -> None:
        assert DialogInput().model_dump() == {"label": "", "placeholder": "", "value": ""}
        assert DialogButton(id="a", label="b").primary is False

    def test_a_spec_is_the_dialog_a_window_opens_with_its_kind_first(self) -> None:
        dialog = DialogSpec.of({"title": "Sure?", "input": {"label": "Name"}})
        assert isinstance(dialog, ExtensionDialog)
        dumped = dialog.model_dump(mode="json")
        assert list(dumped)[0] == "kind"
        assert dumped == {
            "kind": "extension",
            "title": "Sure?",
            "body": "",
            "buttons": [{"id": "ok", "label": "OK", "primary": True}],
            "input": {"label": "Name", "placeholder": "", "value": ""},
        }

    @pytest.mark.parametrize("value", [None, "text", 3, [], {"title": 1}, {"title": "x", "z": 1}])
    def test_a_spec_that_does_not_fit_is_invalid_with_the_members_name_in_front(
        self, value: Any
    ) -> None:
        with pytest.raises(HostModelError) as caught:
            DialogSpec.of(value)
        assert caught.value.code == ErrorCode.INVALID
        assert caught.value.message.startswith("ui.show_dialog(spec): ")
