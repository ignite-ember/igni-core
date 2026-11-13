"""Each field of a window's model has a handler that reads, checks and keeps its value.

What lasts is kept in the client-state store (a real one, on disk) and
read back from it; what does not is held in the window's transient
fields. A handler refuses what the schema or its own rules refuse, with
a ``WindowModelError`` naming why.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.core.session.client_state import ClientStateStore
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.types import SchemaTypes
from igni_core.extensions.host.window_errors import WindowModelError
from igni_core.extensions.host.window_fields import WindowFields
from igni_core.extensions.host.window_state import UiKeys, WindowState
from igni_core.extensions.host.window_values import Composer, Page, Panel, Scroll, Sidebar

WINDOW = "w-1"


class Rig:
    def __init__(self, path: Path) -> None:
        self.store = ClientStateStore(path / "client_state.db")
        self.state = WindowState(self.store)
        self.fields = WindowFields(SchemaTypes(HOST_SCHEMA), self.state.lasting)
        self.ctx = self.state.context(WINDOW)

    def write(self, name: str, value: object) -> object:
        handler = self.fields.handler(name)
        checked = handler.check(value)
        handler.keep(self.ctx, checked)
        return handler.read(self.ctx)

    def stored(self) -> dict[str, str]:
        return self.state.lasting.read(WINDOW)

    def put(self, key: str, value: str) -> None:
        self.state.lasting.write(WINDOW, key, value)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


class TestAStoredText:
    def test_it_is_empty_until_written_and_then_the_text_that_was_kept(self, rig: Rig) -> None:
        handler = rig.fields.handler("session")
        assert handler.read(rig.ctx) == ""

        assert rig.write("session", "s-é") == "s-é"
        assert rig.stored()[UiKeys.SESSION] == "s-é"
        assert rig.write("project_dir", "/work") == "/work"
        assert rig.stored()[UiKeys.PROJECT_DIR] == "/work"

    def test_an_empty_text_keeps_nothing(self, rig: Rig) -> None:
        rig.write("session", "s1")

        assert rig.write("session", "") == ""
        assert UiKeys.SESSION not in rig.stored()

    def test_a_text_is_checked_to_be_one(self, rig: Rig) -> None:
        with pytest.raises(WindowModelError, match="session: expected str"):
            rig.fields.handler("session").check(["s1"])


class TestARoute:
    def test_it_is_kept_as_json_and_read_back_as_pages(self, rig: Rig) -> None:
        pages = rig.write(
            "route",
            [
                {"kind": "plugins", "label": "Plugins"},
                {"kind": "x", "label": "X", "params": {"a": "1"}},
            ],
        )

        assert pages == [
            Page(kind="plugins", label="Plugins"),
            Page(kind="x", label="X", params={"a": "1"}),
        ]
        assert [p["kind"] for p in json.loads(rig.stored()[UiKeys.PAGE])] == ["plugins", "x"]

    def test_a_stored_page_with_no_label_is_labelled_with_its_kind(self, rig: Rig) -> None:
        rig.put(UiKeys.PAGE, json.dumps([{"kind": "plugins", "params": {"n": 1}}]))

        assert rig.fields.handler("route").read(rig.ctx) == [
            Page(kind="plugins", label="plugins", params={"n": "1"})
        ]

    def test_an_empty_route_is_kept_as_nothing(self, rig: Rig) -> None:
        rig.write("route", [{"kind": "plugins", "label": "Plugins"}])

        assert rig.write("route", []) == []
        assert UiKeys.PAGE not in rig.stored()

    @pytest.mark.parametrize(
        "raw",
        ["{not json", '{"kind": "plugins"}', '"plugins"', "7", "null"],
    )
    def test_a_stored_route_that_is_not_a_list_of_pages_reads_as_empty(
        self, rig: Rig, raw: str
    ) -> None:
        rig.put(UiKeys.PAGE, raw)

        assert rig.fields.handler("route").read(rig.ctx) == []

    def test_reading_stops_at_the_first_entry_that_is_not_a_page(self, rig: Rig) -> None:
        for bad in ("oops", {"label": "no kind"}, {"kind": 3}):
            rig.put(UiKeys.PAGE, json.dumps([{"kind": "a"}, bad, {"kind": "c"}]))

            assert [p.kind for p in rig.fields.handler("route").read(rig.ctx)] == ["a"]

    def test_a_page_with_no_kind_is_refused(self, rig: Rig) -> None:
        with pytest.raises(WindowModelError, match="route: expected list"):
            rig.fields.handler("route").check([{"label": "no kind"}])


class TestASidebar:
    def test_it_is_closed_and_unsized_until_written(self, rig: Rig) -> None:
        assert rig.fields.handler("sidebar").read(rig.ctx) == Sidebar()

    def test_open_width_and_section_are_kept_and_read_back(self, rig: Rig) -> None:
        shown = rig.write("sidebar", {"open": True, "width": 240, "section": "sessions"})

        assert shown == Sidebar(open=True, width=240, section="sessions")
        assert rig.stored()[UiKeys.SIDEBAR] == "true"
        assert rig.write("sidebar", {"open": False}) == Sidebar()
        assert rig.stored()[UiKeys.SIDEBAR] == "false"
        assert UiKeys.SIDEBAR_WIDTH not in rig.stored()

    @pytest.mark.parametrize("width", [180, 600])
    def test_a_width_at_the_edge_is_accepted(self, rig: Rig, width: int) -> None:
        assert rig.write("sidebar", {"open": True, "width": width}).width == width

    @pytest.mark.parametrize("width", [179, 601, 0, -300])
    def test_a_width_outside_180_to_600_is_refused(self, rig: Rig, width: int) -> None:
        with pytest.raises(WindowModelError, match=f"sidebar width {width} is outside 180 to 600"):
            rig.fields.handler("sidebar").check({"open": True, "width": width})

    def test_a_width_that_is_not_a_whole_number_is_refused(self, rig: Rig) -> None:
        with pytest.raises(WindowModelError, match="sidebar: expected Sidebar"):
            rig.fields.handler("sidebar").check({"open": True, "width": "wide"})

    @pytest.mark.parametrize("section", ["", "Links", "other"])
    def test_a_section_that_is_not_links_or_sessions_is_refused(
        self, rig: Rig, section: str
    ) -> None:
        with pytest.raises(WindowModelError, match="is not `links` or `sessions`"):
            rig.fields.handler("sidebar").check({"open": True, "section": section})

    def test_a_stored_width_that_is_not_digits_reads_as_no_width(self, rig: Rig) -> None:
        rig.put(UiKeys.SIDEBAR_WIDTH, "-5")
        assert rig.fields.handler("sidebar").read(rig.ctx).width is None
        rig.put(UiKeys.SIDEBAR_WIDTH, "wide")
        assert rig.fields.handler("sidebar").read(rig.ctx).width is None
        rig.put(UiKeys.SIDEBAR_WIDTH, "300")
        assert rig.fields.handler("sidebar").read(rig.ctx).width == 300


class TestAComposer:
    def test_with_no_session_it_is_empty_and_refuses_a_draft(self, rig: Rig) -> None:
        assert rig.fields.handler("composer").read(rig.ctx) == Composer()

        with pytest.raises(WindowModelError, match="shows no session yet: no draft to write"):
            rig.write("composer", {"text": "hello"})

    def test_the_draft_its_files_and_its_mode_are_the_sessions(self, rig: Rig) -> None:
        rig.write("session", "s1")

        shown = rig.write("composer", {"text": "look at @a.py and @b.py", "mode": "shell"})

        assert shown == Composer(
            text="look at @a.py and @b.py", attachments=["a.py", "b.py"], mode="shell"
        )
        rig.write("session", "s2")
        assert rig.fields.handler("composer").read(rig.ctx) == Composer()
        rig.write("session", "s1")
        assert rig.fields.handler("composer").read(rig.ctx).mode == "shell"

    def test_the_default_mode_is_chat_and_keeps_nothing(self, rig: Rig) -> None:
        rig.write("session", "s1")
        rig.write("composer", {"text": "hi", "mode": "command"})

        assert rig.write("composer", {"text": "hi"}).mode == "chat"
        assert UiKeys.mode("s1") not in rig.stored()

    @pytest.mark.parametrize("mode", ["Chat", "voice", "chat "])
    def test_a_mode_that_is_not_chat_command_or_shell_is_refused(self, rig: Rig, mode: str) -> None:
        with pytest.raises(
            WindowModelError, match="composer mode .* is not one of chat, command, shell"
        ):
            rig.fields.handler("composer").check({"text": "", "mode": mode})

    def test_a_draft_keeps_what_was_typed(self, rig: Rig) -> None:
        assert rig.fields.handler("composer").check({"text": "é ✓"}).text == "é ✓"


class TestAModelHeldHere:
    def test_a_panel_or_preview_can_be_empty_and_is_kept_in_this_process_only(
        self, rig: Rig
    ) -> None:
        assert rig.fields.handler("panel").read(rig.ctx) is None
        assert rig.write("panel", {"kind": "search"}) == Panel(kind="search", params={})
        assert rig.write("panel", {"kind": "login", "params": None}).params == {}
        assert rig.write("panel", None) is None
        assert rig.write("preview", None) is None
        assert rig.stored() == {}

    def test_a_panel_keeps_the_parameters_it_was_given(self, rig: Rig) -> None:
        shown = rig.write("panel", {"kind": "search", "params": {"q": "x", "n": 2}})

        assert shown.params == {"q": "x", "n": 2}

    def test_a_scroll_is_told_nothing_of_its_window(self, rig: Rig) -> None:
        shown = rig.write("scroll", {"message_id": "m1", "at_bottom": False, "window": "w-9"})

        assert shown == Scroll(message_id="m1", at_bottom=False)
        assert "window" not in shown.model_dump()

    def test_a_selection_and_the_focus_are_held_as_they_are_checked(self, rig: Rig) -> None:
        assert rig.write("focused", True) is True
        with pytest.raises(WindowModelError, match="focused: expected bool"):
            rig.fields.handler("focused").check("maybe")
        with pytest.raises(WindowModelError, match="selection: expected SelectionState"):
            rig.fields.handler("selection").check({"text": 3})

    def test_each_window_holds_its_own(self, rig: Rig) -> None:
        rig.write("focused", True)

        assert rig.state.context("w-2").transient.focused is False
