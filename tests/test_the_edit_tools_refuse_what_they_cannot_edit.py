"""The edit tools answer in a string, always — they never raise at the model.

``edit_file`` and ``edit_file_replace_all`` read the whole file as UTF-8 and
write it back. Three inputs made them raise out of the tool instead of
answering:

* an empty ``old_string``: ``str.count("")`` is one more than the length, so
  ``edit_file`` told the model the empty string "appears 12 times" and
  ``edit_file_replace_all`` wrote ``new_string`` between every character of
  the file;
* a file that is not UTF-8 text (an image, a compiled file), which raised
  ``UnicodeDecodeError``;
* a path that is a directory, or a file that cannot be written, which raised
  ``IsADirectoryError`` / ``PermissionError``.

Each is now a plain ``Error: ...`` answer and the file is left as it was.
"""

from __future__ import annotations

import os
import stat

import pytest

from igni_core.core.tools.edit import FileEditNotifier, IgniEditTools


@pytest.fixture
def notified() -> list[str]:
    return []


@pytest.fixture
def editor(tmp_path, notified):
    return IgniEditTools(base_dir=str(tmp_path), notifier=FileEditNotifier(notified.append))


@pytest.mark.parametrize("tool", ["edit_file", "edit_file_replace_all"])
def test_an_empty_old_string_is_refused_and_the_file_is_untouched(editor, tmp_path, notified, tool):
    target = tmp_path / "a.txt"
    target.write_text("hello")

    answer = getattr(editor, tool)("a.txt", "", "X")

    assert answer == "Error: old_string is empty. Provide the text to replace."
    assert target.read_text() == "hello"
    assert notified == []


@pytest.mark.parametrize("tool", ["edit_file", "edit_file_replace_all"])
def test_a_file_that_is_not_utf8_text_is_refused(editor, tmp_path, notified, tool):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\xff\xfe\x00binary")

    answer = getattr(editor, tool)("blob.bin", "a", "b")

    assert answer == f"Error: {target} is not a UTF-8 text file."
    assert target.read_bytes() == b"\xff\xfe\x00binary"
    assert notified == []


@pytest.mark.parametrize("tool", ["edit_file", "edit_file_replace_all"])
def test_a_directory_is_not_a_file_to_edit(editor, tmp_path, notified, tool):
    (tmp_path / "pkg").mkdir()

    answer = getattr(editor, tool)("pkg", "a", "b")

    assert answer.startswith(f"Error: could not read {tmp_path / 'pkg'}: ")
    assert notified == []


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a read-only file")
@pytest.mark.parametrize("tool", ["edit_file", "edit_file_replace_all"])
def test_a_file_that_cannot_be_written_says_so(editor, tmp_path, notified, tool):
    target = tmp_path / "ro.txt"
    target.write_text("one")
    target.chmod(stat.S_IRUSR)

    answer = getattr(editor, tool)("ro.txt", "one", "two")

    assert answer.startswith(f"Error: could not write {target}: ")
    assert target.read_text() == "one"
    assert notified == []


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a read-only directory")
def test_a_file_that_cannot_be_created_says_so(editor, tmp_path, notified):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        answer = editor.create_file("locked/new.txt", "x")
    finally:
        locked.chmod(stat.S_IRWXU)

    assert answer.startswith(f"Error: could not write {locked / 'new.txt'}: ")
    assert not (locked / "new.txt").exists()
    assert notified == []


def test_a_parent_that_is_a_file_cannot_be_made_into_a_directory(editor, tmp_path, notified):
    (tmp_path / "plain.txt").write_text("x")

    answer = editor.create_file("plain.txt/sub/new.txt", "x")

    assert answer.startswith(
        f"Error: could not create {tmp_path / 'plain.txt' / 'sub' / 'new.txt'}: "
    )
    assert notified == []
