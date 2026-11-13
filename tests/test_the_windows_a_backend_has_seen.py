"""Which windows are open, which is active, and which has the focus."""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.session.client_state import ClientStateStore
from igni_core.extensions.host.window_changes import ChangeNotifier, WindowChange
from igni_core.extensions.host.window_registry import WindowCursor, WindowRegistry
from igni_core.extensions.host.window_state import UiKeys, WindowState
from igni_core.extensions.host.window_values import WindowInfo


class Rig:
    def __init__(self, path: Path) -> None:
        self.moves: list[tuple[str, str]] = []
        self.changes: list[tuple[str, str, object]] = []
        self.pushed: list[WindowChange] = []
        self.notifier = ChangeNotifier(self.pushed.append)
        self.notifier.on_window(lambda what, window: self.moves.append((what, window.id)))
        self.notifier.on_change(lambda c: self.changes.append((c.window, c.field, c.value)))
        self.state = WindowState(ClientStateStore(path / "client_state.db"))
        self.registry = WindowRegistry(self.notifier, self.state.lasting, self.state.transient)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_a_window_that_attaches_is_open_and_active_and_opened_is_told_once(rig: Rig) -> None:
    rig.registry.attached("w-1", connection_id="c-1", kind="tauri")
    rig.registry.attached("w-1", connection_id="c-1")

    assert rig.moves == [("opened", "w-1")]
    assert rig.registry.known("w-1")
    assert rig.registry.active() == "w-1"
    assert rig.registry.by_connection("c-1") == "w-1"
    record = rig.registry.record("w-1")
    assert (record.kind, record.connection_id, record.connected) == ("tauri", "c-1", True)


def test_a_window_with_no_id_is_ignored(rig: Rig) -> None:
    rig.registry.attached("", connection_id="c-1")

    assert rig.moves == []
    assert rig.registry.open() == []
    assert rig.registry.by_connection("c-1") is None
    assert rig.registry.active() is None


def test_a_window_that_attaches_again_keeps_its_kind_and_connection_unless_told_new(
    rig: Rig,
) -> None:
    rig.registry.attached("w-1", connection_id="c-1", kind="tauri")
    rig.registry.attached("w-1")

    record = rig.registry.record("w-1")
    assert (record.kind, record.connection_id) == ("tauri", "c-1")

    rig.registry.attached("w-1", connection_id="c-2", kind="web")
    record = rig.registry.record("w-1")
    assert (record.kind, record.connection_id) == ("web", "c-2")
    assert rig.registry.by_connection("c-1") is None


def test_a_window_with_no_kind_is_a_web_window(rig: Rig) -> None:
    rig.registry.attached("w-1")

    assert rig.registry.describe("w-1").kind == "web"


def test_a_closed_connection_closes_its_window_and_a_reconnect_opens_it_again(rig: Rig) -> None:
    rig.registry.attached("w-1", connection_id="c-1")
    rig.registry.detached("c-1")

    assert rig.moves == [("opened", "w-1"), ("closed", "w-1")]
    assert not rig.registry.known("w-1")
    assert rig.registry.open() == []
    assert rig.registry.by_connection("c-1") is None
    assert rig.registry.describe("w-1").connected is False

    rig.registry.detached("c-1")
    rig.registry.detached("c-unknown")
    assert rig.moves == [("opened", "w-1"), ("closed", "w-1")]

    rig.registry.attached("w-1", connection_id="c-3")
    assert rig.moves[-1] == ("opened", "w-1")
    assert rig.registry.known("w-1")


def test_the_last_used_window_is_active_and_a_gone_one_gives_way_to_the_most_recent(
    rig: Rig,
) -> None:
    for number in (1, 2, 3):
        rig.registry.attached(f"w-{number}", connection_id=f"c-{number}")
    assert rig.registry.active() == "w-3"

    rig.registry.touch("w-1")
    assert rig.registry.active() == "w-1"
    rig.registry.touch("w-2")
    rig.registry.touch("w-3")
    rig.registry.touch("w-1")
    rig.registry.detached("c-1")

    assert rig.registry.active() == "w-3"
    rig.registry.detached("c-3")
    assert rig.registry.active() == "w-2"
    rig.registry.detached("c-2")
    assert rig.registry.active() is None


def test_touching_an_unknown_window_or_connection_changes_nothing(rig: Rig) -> None:
    rig.registry.attached("w-1", connection_id="c-1")
    rig.registry.attached("w-2", connection_id="c-2")

    rig.registry.touch("w-nobody")
    rig.registry.touch_connection("c-nobody")
    assert rig.registry.active() == "w-2"

    rig.registry.touch_connection("c-1")
    assert rig.registry.active() == "w-1"


def test_a_window_that_was_closed_is_not_made_active_by_its_connection(rig: Rig) -> None:
    rig.registry.attached("w-1", connection_id="c-1")
    rig.registry.attached("w-2", connection_id="c-2")
    rig.registry.detached("c-1")

    rig.registry.touch_connection("c-1")

    assert rig.registry.active() == "w-2"


def test_only_one_window_has_the_focus_and_the_one_before_is_told_it_lost_it(rig: Rig) -> None:
    rig.registry.attached("w-1", connection_id="c-1")
    rig.registry.attached("w-2", connection_id="c-2")

    rig.registry.focus_window("w-1", True)
    rig.state.transient.of("w-1").focused = True
    rig.registry.focus_window("w-2", True)

    assert rig.registry.focused() == "w-2"
    assert rig.registry.active() == "w-2"
    assert rig.state.transient.of("w-1").focused is False
    assert ("w-1", "focused", False) in rig.changes
    assert rig.moves[-1] == ("focus_changed", "w-2")
    assert [w.id for w in rig.registry.describe_all() if w.focused] == ["w-2"]


def test_the_focus_is_told_only_when_it_moved(rig: Rig) -> None:
    rig.registry.attached("w-1", connection_id="c-1")
    rig.registry.attached("w-2", connection_id="c-2")
    rig.registry.focus_window("w-1", True)
    before = list(rig.moves)

    rig.registry.focus_window("w-1", True)
    rig.registry.focus_window("w-2", False)

    assert rig.moves == before
    assert rig.registry.focused() == "w-1"
    assert rig.changes == []

    rig.registry.focus_window("w-1", False)
    assert rig.registry.focused() is None
    assert rig.moves == [*before, ("focus_changed", "w-1")]


def test_a_window_that_goes_away_with_the_focus_takes_it_and_the_active_place_with_it(
    rig: Rig,
) -> None:
    rig.registry.attached("w-1", connection_id="c-1")
    rig.registry.focus_window("w-1", True)
    rig.state.transient.of("w-1").focused = True

    rig.registry.detached("c-1")

    assert rig.registry.focused() is None
    assert rig.state.transient.of("w-1").focused is False
    assert rig.registry.describe("w-1").focused is False


def test_a_window_is_described_with_the_session_and_folder_it_keeps(rig: Rig) -> None:
    rig.registry.attached("w-1", connection_id="c-1", kind="tauri")
    assert rig.registry.describe("w-1") == WindowInfo(
        id="w-1", kind="tauri", connected=True, focused=False, session_id=None, project_dir=None
    )

    rig.state.lasting.write("w-1", UiKeys.SESSION, "s1")
    rig.state.lasting.write("w-1", UiKeys.PROJECT_DIR, "/work")

    info = rig.registry.describe("w-1")
    assert (info.session_id, info.project_dir) == ("s1", "/work")
    assert rig.registry.describe_all() == [info]


def test_a_window_that_was_never_seen_is_not_described(rig: Rig) -> None:
    with pytest.raises(KeyError):
        rig.registry.describe("w-nobody")
    with pytest.raises(KeyError):
        rig.registry.record("w-nobody")
    assert not rig.registry.known("w-nobody")


def test_using_a_window_counts_the_uses_and_makes_it_the_active_one() -> None:
    cursor = WindowCursor()

    cursor.use("w-1")
    cursor.use("w-2")
    cursor.use("w-1")

    assert cursor.active == "w-1"
    assert cursor.last_used == {"w-1": 3, "w-2": 2}
    assert cursor.tick == 3
