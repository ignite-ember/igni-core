"""The window's "choose a folder" button asks the operating system's own dialog.

Each platform's dialog is a real program here, a script at the front of
``PATH`` that records its arguments and answers as told, so what is
pinned is what the picker runs and what it makes of the answer: a path,
a cancel, or an error that says nothing could be shown.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from igni_core.backend.folder_picker import NativeFolderPicker

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the dialogs are stood in for by shell scripts"
)


@pytest.fixture
def desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The whole of ``PATH``; a dialog program goes in here."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return bin_dir


def _dialog(desk: Path, name: str, *, answer: str = "", rc: int = 0) -> Path:
    """Record each argument on its own line to ``<name>.argv``; print ``answer``; exit ``rc``."""
    argv = desk / f"{name}.argv"
    script = desk / name
    script.write_text(
        f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a" >> "{argv}"; done\n'
        f"printf '%b' '{answer}'\nexit {rc}\n"
    )
    script.chmod(0o755)
    return argv


def _args(argv: Path) -> list[str]:
    return argv.read_text().splitlines()


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", "_DarwinPicker"),
        ("linux", "_LinuxPicker"),
        ("linux2", "_LinuxPicker"),
        ("win32", "_WindowsPicker"),
        ("freebsd14", "_UnsupportedPicker"),
    ],
)
def test_the_picker_is_chosen_by_platform(monkeypatch, platform, expected):
    monkeypatch.setattr(sys, "platform", platform)

    assert type(NativeFolderPicker.for_platform("")).__name__ == expected


@pytest.mark.asyncio
async def test_an_unsupported_platform_is_an_error_and_not_a_cancel(monkeypatch):
    monkeypatch.setattr(sys, "platform", "freebsd14")

    result = await NativeFolderPicker.for_platform("").pick()

    assert result.error == "unsupported platform: freebsd14"
    assert result.cancelled is False
    assert result.path == ""


class TestOnMacOs:
    @pytest.fixture(autouse=True)
    def _platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")

    @pytest.mark.asyncio
    async def test_the_chosen_folder_comes_back_without_its_trailing_slash(self, desk):
        _dialog(desk, "osascript", answer="/Users/me/work/\\n")

        result = await NativeFolderPicker.for_platform("").pick()

        assert (result.path, result.cancelled, result.error) == ("/Users/me/work", False, "")

    @pytest.mark.asyncio
    async def test_choosing_the_root_is_the_root(self, desk):
        _dialog(desk, "osascript", answer="/\\n")

        assert (await NativeFolderPicker.for_platform("").pick()).path == "/"

    @pytest.mark.asyncio
    async def test_the_start_folder_is_the_default_location_with_quotes_escaped(self, desk):
        argv = _dialog(desk, "osascript", answer="/x\\n")

        await NativeFolderPicker.for_platform('/Users/me/a"b\\c').pick()

        script = _args(argv)[1]
        assert 'default location POSIX file "/Users/me/a\\"b\\\\c"' in script
        assert script.startswith("POSIX path of (choose folder")

    @pytest.mark.asyncio
    async def test_no_start_folder_means_no_default_location(self, desk):
        argv = _dialog(desk, "osascript", answer="/x\\n")

        await NativeFolderPicker.for_platform("").pick()

        assert "default location" not in _args(argv)[1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("answer", "rc"), [("", 1), ("", 0), ("/x\\n", 1)])
    async def test_a_cancel_is_not_an_error(self, desk, answer, rc):
        """osascript exits non-zero when the person cancels, and prints nothing."""
        _dialog(desk, "osascript", answer=answer, rc=rc)

        result = await NativeFolderPicker.for_platform("").pick()

        assert (result.path, result.cancelled, result.error) == ("", True, "")


class TestOnLinux:
    @pytest.fixture(autouse=True)
    def _platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")

    @pytest.mark.asyncio
    async def test_zenity_is_asked_first_with_the_start_folder(self, desk):
        argv = _dialog(desk, "zenity", answer="/home/me/p\\n")
        kdialog = _dialog(desk, "kdialog", answer="/other\\n")

        result = await NativeFolderPicker.for_platform("/home/me").pick()

        assert result.path == "/home/me/p"
        assert _args(argv) == ["--file-selection", "--directory", "--filename=/home/me/"]
        assert not kdialog.exists()

    @pytest.mark.asyncio
    async def test_without_zenity_kdialog_is_asked_and_the_start_folder_is_one_argument(self, desk):
        argv = _dialog(desk, "kdialog", answer="/home/me/p\\n")

        result = await NativeFolderPicker.for_platform('/home/o\'brien "q"').pick()

        assert (result.path, result.cancelled, result.error) == ("/home/me/p", False, "")
        assert _args(argv) == ["--getexistingdirectory", '/home/o\'brien "q"']

    @pytest.mark.asyncio
    async def test_a_cancelled_zenity_is_a_cancel_and_kdialog_is_not_tried(self, desk):
        _dialog(desk, "zenity", rc=1)
        kdialog = _dialog(desk, "kdialog", answer="/other\\n")

        result = await NativeFolderPicker.for_platform("").pick()

        assert (result.path, result.cancelled, result.error) == ("", True, "")
        assert not kdialog.exists()

    @pytest.mark.asyncio
    async def test_neither_dialog_installed_says_so(self, desk):
        result = await NativeFolderPicker.for_platform("").pick()

        assert result.error == "no native dialog available"
        assert result.cancelled is False

    @pytest.mark.asyncio
    async def test_an_answer_that_is_not_utf8_is_still_a_path(self, desk):
        _dialog(desk, "zenity", answer="/home/me/caf\\351\\n")

        result = await NativeFolderPicker.for_platform("").pick()

        assert result.path == "/home/me/caf\ufffd"


class TestOnWindows:
    @pytest.fixture(autouse=True)
    def _platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")

    @pytest.mark.asyncio
    async def test_the_start_folder_is_where_the_dialog_opens(self, desk):
        argv = _dialog(desk, "powershell", answer="C:\\\\Users\\\\me\\\\p\\n")

        result = await NativeFolderPicker.for_platform("C:\\Users\\me").pick()

        args = _args(argv)
        assert args[:2] == ["-NoProfile", "-Command"]
        assert "$d.SelectedPath = 'C:\\Users\\me'; " in args[2]
        assert (result.path, result.cancelled) == ("C:\\Users\\me\\p", False)

    @pytest.mark.asyncio
    async def test_a_start_folder_with_a_single_quote_is_left_out_not_put_in_the_script(self, desk):
        argv = _dialog(desk, "powershell", answer="C:\\\\p\\n")

        await NativeFolderPicker.for_platform("C:\\x'; Remove-Item -Recurse C:\\; '").pick()

        script = _args(argv)[2]
        assert "$d.SelectedPath =" not in script
        assert "Remove-Item" not in script

    @pytest.mark.asyncio
    async def test_an_empty_answer_is_a_cancel(self, desk):
        _dialog(desk, "powershell", answer="", rc=0)

        result = await NativeFolderPicker.for_platform("").pick()

        assert (result.path, result.cancelled, result.error) == ("", True, "")
