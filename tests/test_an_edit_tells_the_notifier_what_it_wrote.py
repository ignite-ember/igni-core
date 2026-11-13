"""Every successful write by the edit tools is announced, and only those.

The backend binds a listener to the :class:`FileEditNotifier` so a client
with an editor buffer can reload a file the agent changed. The push bridge's
own tests call ``notify`` by hand; these pin that the tools are the ones who
call it — once per write, with the path written, never for a refused edit —
and that an observer cannot break an edit.
"""

from __future__ import annotations

from igni_core.core.tools.edit import (
    FileEditNotifier,
    IgniEditTools,
    default_file_edit_notifier,
)


def _toolkit(tmp_path, listener=None):
    heard: list[str] = []

    def hear(path: str) -> None:
        heard.append(path)
        if listener is not None:
            listener(path)

    return IgniEditTools(base_dir=str(tmp_path), notifier=FileEditNotifier(hear)), heard


def test_edit_file_announces_the_path_it_wrote(tmp_path):
    (tmp_path / "a.txt").write_text("one two")
    tools, heard = _toolkit(tmp_path)

    tools.edit_file("a.txt", "one", "1")

    assert heard == [str(tmp_path / "a.txt")]


def test_replace_all_announces_once_for_all_its_replacements(tmp_path):
    (tmp_path / "a.txt").write_text("x x x")
    tools, heard = _toolkit(tmp_path)

    answer = tools.edit_file_replace_all("a.txt", "x", "y")

    assert answer == f"Successfully replaced 3 occurrence(s) in {tmp_path / 'a.txt'}"
    assert (tmp_path / "a.txt").read_text() == "y y y"
    assert heard == [str(tmp_path / "a.txt")]


def test_create_file_announces_the_new_file(tmp_path):
    tools, heard = _toolkit(tmp_path)

    tools.create_file("deep/new.txt", "hi")

    assert heard == [str(tmp_path / "deep" / "new.txt")]


def test_a_refused_edit_announces_nothing(tmp_path):
    (tmp_path / "a.txt").write_text("dup dup")
    tools, heard = _toolkit(tmp_path)

    tools.edit_file("missing.txt", "a", "b")
    tools.edit_file("a.txt", "absent", "b")
    tools.edit_file("a.txt", "dup", "b")
    tools.edit_file_replace_all("missing.txt", "a", "b")
    tools.edit_file_replace_all("a.txt", "absent", "b")
    tools.create_file("a.txt", "overwrite")

    assert heard == []
    assert (tmp_path / "a.txt").read_text() == "dup dup"


def test_an_observer_that_raises_cannot_break_the_edit(tmp_path):
    (tmp_path / "a.txt").write_text("one")

    def boom(path: str) -> None:
        raise RuntimeError("flaky observer")

    tools, heard = _toolkit(tmp_path, listener=boom)

    answer = tools.edit_file("a.txt", "one", "two")

    assert answer == f"Successfully edited {tmp_path / 'a.txt'}"
    assert (tmp_path / "a.txt").read_text() == "two"
    assert heard == [str(tmp_path / "a.txt")]


def test_a_notifier_with_no_listener_is_silent_and_can_be_cleared():
    heard: list[str] = []
    notifier = FileEditNotifier()
    notifier.notify("/p/a")  # type: ignore[arg-type]  # nothing listening: no error

    notifier.set_listener(heard.append)
    notifier.notify("/p/a")  # type: ignore[arg-type]
    notifier.set_listener(None)
    notifier.notify("/p/b")  # type: ignore[arg-type]

    assert heard == ["/p/a"]


def test_a_toolkit_built_without_a_notifier_reports_to_the_shared_default(tmp_path):
    heard: list[str] = []
    default_file_edit_notifier.set_listener(heard.append)
    try:
        IgniEditTools(base_dir=str(tmp_path)).create_file("n.txt", "x")
    finally:
        default_file_edit_notifier.set_listener(None)

    assert heard == [str(tmp_path / "n.txt")]


def test_a_multiple_match_says_how_many_times_it_appears(tmp_path):
    (tmp_path / "a.txt").write_text("dup dup dup")
    tools, _ = _toolkit(tmp_path)

    answer = tools.edit_file("a.txt", "dup", "b")

    assert answer == (
        f"Error: old_string appears 3 times in {tmp_path / 'a.txt'}. Provide more "
        "surrounding context to make it unique, or use edit_file_replace_all."
    )
