"""The Win32 calls igni makes for an AppContainer (``sandbox/winapi.py``), through a faked ctypes.

``test_an_extension_process_is_confined_on_windows.py`` and its edges file
replace this module by a fake, so they prove how :mod:`appcontainer` uses
it, not what it does. Here the module is real and only the DLL boundary is
faked: what each call is handed, what it frees, which results it tolerates
and which it turns into an :class:`OSError`. Nothing here needs Windows.
"""

from __future__ import annotations

import ctypes
import struct
from pathlib import Path

import pytest

from igni_core.extensions.sandbox import winapi

pytestmark = pytest.mark.extension_sandbox

ALREADY_EXISTS = 0x800700B7
NOT_FOUND = 0x80070490
FILE_NOT_FOUND = 0x80070002


def signed(hresult: int) -> int:
    """An HRESULT as a signed 32-bit return, which is how a C ``long`` comes back."""
    return hresult - (1 << 32)


class Win32:
    """The DLLs' functions by name, recorded; each handler gets the raw arguments."""

    def __init__(self) -> None:
        self.handlers: dict = {}
        self.calls: list[tuple] = []
        self.freed: list[tuple[str, int]] = []
        self.loaded: list[str] = []
        self.last_error = 0
        self.handlers["LocalFree"] = lambda p: self.freed.append(("LocalFree", p.value)) or 0
        self.handlers["FreeSid"] = lambda p: self.freed.append(("FreeSid", p.value)) or 0
        self.handlers["CloseHandle"] = lambda h: self.freed.append(("CloseHandle", _int(h))) or 1

    def called(self, name: str) -> list[tuple]:
        return [args for fn, args in self.calls if fn == name]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Win32:
        win = self

        class Lib:
            def __init__(self, dll: str) -> None:
                win.loaded.append(dll)

            def __getattr__(self, name: str):
                if name not in win.handlers:
                    raise AttributeError(name)

                def call(*args):
                    win.calls.append((name, args))
                    return win.handlers[name](*args)

                return call

        def win_error(code: int, what: str) -> OSError:
            return OSError(f"{what}: error {code}")

        monkeypatch.setattr(winapi, "WINDOWS", True)
        monkeypatch.setattr(winapi, "_dlls", {})
        monkeypatch.setattr(ctypes, "WinDLL", lambda dll, use_last_error=False: Lib(dll), False)
        monkeypatch.setattr(ctypes, "WinError", win_error, raising=False)
        monkeypatch.setattr(ctypes, "get_last_error", lambda: self.last_error, raising=False)
        return self


def _int(handle) -> int:
    return handle.value if hasattr(handle, "value") else handle


def out(arg):
    """The variable a ``byref`` argument points at."""
    return arg._obj


@pytest.fixture
def win(monkeypatch: pytest.MonkeyPatch) -> Win32:
    return Win32().install(monkeypatch)


# ── off Windows ───────────────────────────────────────────────────────


@pytest.mark.skipif(winapi.WINDOWS, reason="this is the guard for every other platform")
class TestOffWindows:
    def test_the_module_says_it_is_not_windows(self):
        assert winapi.WINDOWS is False

    @pytest.mark.parametrize(
        "call",
        [
            lambda: winapi.create_profile("n", "d", "x"),
            lambda: winapi.delete_profile("n"),
            winapi.appcontainer_api_present,
            winapi.current_user_sid,
            winapi.in_appcontainer,
            lambda: winapi.grant(Path("."), "S-1-1-0", write=True),
            lambda: winapi.revoke(Path("."), "S-1-1-0"),
            lambda: winapi.label(Path(".")),
            lambda: winapi.set_label(Path("."), "S:"),
            lambda: winapi.process_alive(1),
            winapi.OwnedSid("S-1-1-0").__enter__,
        ],
        ids=[
            "create_profile",
            "delete_profile",
            "appcontainer_api_present",
            "current_user_sid",
            "in_appcontainer",
            "grant",
            "revoke",
            "label",
            "set_label",
            "process_alive",
            "OwnedSid",
        ],
    )
    def test_every_call_refuses_with_an_oserror(self, call):
        with pytest.raises(OSError, match="not Windows"):
            call()


def test_the_values_igni_hands_windows():
    assert winapi.FILE_READ_EXECUTE == 0x1200A9
    assert winapi.FILE_MODIFY == 0x1301BF
    assert (winapi.GRANT_ACCESS, winapi.REVOKE_ACCESS) == (1, 4)
    assert (winapi.NO_INHERITANCE, winapi.SUB_CONTAINERS_AND_OBJECTS_INHERIT) == (0, 3)
    assert winapi.DACL_SECURITY_INFORMATION == 0x4
    assert winapi.LABEL_SECURITY_INFORMATION == 0x10
    assert winapi.STILL_ACTIVE == 259
    assert winapi.PROCESS_QUERY_LIMITED_INFORMATION == 0x1000
    assert winapi.TOKEN_IS_APP_CONTAINER == 29


def test_a_dll_is_loaded_once_and_its_functions_are_found_on_each_call(win):
    win.handlers["DeleteAppContainerProfile"] = lambda name: 0
    winapi.delete_profile("a")
    winapi.delete_profile("b")
    assert win.loaded == ["userenv"]


# ── profiles ──────────────────────────────────────────────────────────


def install_profile(win: Win32, *, create: int, derive: int = 0, sid: str = "S-1-15-2-9"):
    def create_profile(name, display, description, caps, count, psid):
        out(psid).value = 1234
        return create

    def derive_sid(name, psid):
        out(psid).value = 1234
        return derive

    def to_string(psid, text):
        assert psid.value == 1234
        out(text).value = sid
        return 1

    win.handlers["CreateAppContainerProfile"] = create_profile
    win.handlers["DeriveAppContainerSidFromAppContainerName"] = derive_sid
    win.handlers["ConvertSidToStringSidW"] = to_string


def test_a_new_profile_answers_its_sid_and_frees_what_windows_allocated(win):
    install_profile(win, create=0)
    assert winapi.create_profile("igni.x", "Display", "Desc") == "S-1-15-2-9"
    name, display, description, caps, count, _ = win.called("CreateAppContainerProfile")[0]
    assert (name, display, description, caps, count) == ("igni.x", "Display", "Desc", None, 0)
    assert win.called("DeriveAppContainerSidFromAppContainerName") == []
    assert ("FreeSid", 1234) in win.freed


@pytest.mark.parametrize("already", [ALREADY_EXISTS, signed(ALREADY_EXISTS)])
def test_a_profile_that_exists_is_found_again_by_its_name(win, already):
    install_profile(win, create=already)
    assert winapi.create_profile("igni.x", "D", "D") == "S-1-15-2-9"
    assert win.called("DeriveAppContainerSidFromAppContainerName")[0][0] == "igni.x"
    assert ("FreeSid", 1234) in win.freed


def test_a_profile_windows_refuses_raises_with_the_name_and_the_code(win):
    install_profile(win, create=signed(0x80070057))
    with pytest.raises(OSError, match=r"igni\.x failed: HRESULT 0x80070057"):
        winapi.create_profile("igni.x", "D", "D")
    assert win.called("ConvertSidToStringSidW") == []


def test_a_profile_that_exists_but_cannot_be_derived_raises(win):
    install_profile(win, create=ALREADY_EXISTS, derive=signed(0x80070005))
    with pytest.raises(OSError, match="HRESULT 0x80070005"):
        winapi.create_profile("igni.x", "D", "D")


def test_the_sid_is_freed_even_when_it_cannot_be_written_out(win):
    install_profile(win, create=0)
    win.handlers["ConvertSidToStringSidW"] = lambda psid, text: 0
    with pytest.raises(OSError, match="ConvertSidToStringSidW"):
        winapi.create_profile("igni.x", "D", "D")
    assert ("FreeSid", 1234) in win.freed


@pytest.mark.parametrize("gone", [NOT_FOUND, FILE_NOT_FOUND, signed(NOT_FOUND)])
def test_deleting_a_profile_that_is_gone_is_no_error(win, gone):
    win.handlers["DeleteAppContainerProfile"] = lambda name: gone
    winapi.delete_profile("igni.x")


def test_deleting_a_profile_is_asked_by_name(win):
    win.handlers["DeleteAppContainerProfile"] = lambda name: 0
    winapi.delete_profile("igni.x")
    assert win.called("DeleteAppContainerProfile") == [("igni.x",)]


def test_a_profile_that_cannot_be_deleted_raises_with_the_code(win):
    win.handlers["DeleteAppContainerProfile"] = lambda name: signed(0x80070005)
    with pytest.raises(OSError, match=r"igni\.x failed: HRESULT 0x80070005"):
        winapi.delete_profile("igni.x")


FUNCTIONS = (
    "CreateAppContainerProfile",
    "DeleteAppContainerProfile",
    "DeriveAppContainerSidFromAppContainerName",
)


def test_a_windows_with_the_three_profile_calls_passes(win):
    for name in FUNCTIONS:
        win.handlers[name] = lambda *a: 0
    winapi.appcontainer_api_present()


@pytest.mark.parametrize("missing", FUNCTIONS)
def test_a_windows_without_one_profile_call_names_it(win, missing):
    for name in FUNCTIONS:
        win.handlers[name] = lambda *a: 0
    del win.handlers[missing]
    with pytest.raises(OSError, match=f"userenv.dll has no {missing}"):
        winapi.appcontainer_api_present()


# ── SIDs ──────────────────────────────────────────────────────────────


def test_an_owned_sid_is_converted_on_entry_and_freed_on_exit(win):
    def convert(text, pointer):
        out(pointer).value = 33
        return 1

    win.handlers["ConvertStringSidToSidW"] = convert
    with winapi.OwnedSid("S-1-15-2-9") as sid:
        assert win.called("ConvertStringSidToSidW")[0][0] == "S-1-15-2-9"
        assert sid.pointer.value == 33
        assert win.freed == []
    assert win.freed == [("LocalFree", 33)]


def test_a_sid_string_windows_cannot_read_raises_naming_it(win):
    win.handlers["ConvertStringSidToSidW"] = lambda text, pointer: 0
    win.last_error = 1337
    with (
        pytest.raises(OSError, match="ConvertStringSidToSidW not-a-sid: error 1337"),
        winapi.OwnedSid("not-a-sid"),
    ):
        pytest.fail("the block must not run")


def install_token(win: Win32, fill, *, size: int = 8):
    """The token calls: the first ask for the size, the second for the bytes."""

    def open_token(process, access, token):
        assert access == 0x8
        out(token).value = 99
        return 1

    def get_information(token, cls, buffer, length, size_out):
        win.token_class = cls
        if buffer is None:
            out(size_out).value = size
            return 0
        fill(buffer)
        return 1

    win.handlers["GetCurrentProcess"] = lambda: -1
    win.handlers["OpenProcessToken"] = open_token
    win.handlers["GetTokenInformation"] = get_information


@pytest.mark.parametrize("flag, expected", [(1, True), (0, False)])
def test_igni_asks_whether_its_own_token_is_an_appcontainer_token(win, flag, expected):
    # The size Windows reports back may be smaller than a DWORD: 4 bytes are still read.
    install_token(
        win, lambda buffer: buffer.__setitem__(slice(0, 4), struct.pack("<I", flag)), size=0
    )
    assert winapi.in_appcontainer() is expected
    assert win.token_class == 29
    assert win.freed == [("CloseHandle", 99)]


def test_the_account_igni_runs_as_is_its_token_users_sid(win):
    def fill(buffer):
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0] = 555

    def to_string(psid, text):
        assert psid.value == 555
        out(text).value = "S-1-5-21-1"
        return 1

    install_token(win, fill)
    win.handlers["ConvertSidToStringSidW"] = to_string
    assert winapi.current_user_sid() == "S-1-5-21-1"
    assert win.token_class == 1
    assert ("CloseHandle", 99) in win.freed


def test_the_token_is_closed_when_its_information_cannot_be_read(win):
    install_token(win, lambda buffer: None)
    win.handlers["GetTokenInformation"] = lambda *a: 0
    with pytest.raises(OSError, match="GetTokenInformation"):
        winapi.in_appcontainer()
    assert win.freed == [("CloseHandle", 99)]


def test_a_token_that_cannot_be_opened_raises(win):
    win.handlers["GetCurrentProcess"] = lambda: -1
    win.handlers["OpenProcessToken"] = lambda *a: 0
    with pytest.raises(OSError, match="OpenProcessToken"):
        winapi.current_user_sid()


# ── access lists ──────────────────────────────────────────────────────


def install_acl(win: Win32, *, set_entries: int = 0, set_info: int = 0, get: int = 0):
    seen: dict = {}

    def get_info(path, kind, info, *rest):
        seen["get"] = (path, kind, info)
        if get == 0:
            out(rest[2]).value = 11
            out(rest[4]).value = 22
        return get

    def convert(text, pointer):
        out(pointer).value = 33
        return 1

    def entries(count, entry, old, new):
        e = out(entry)
        seen["entry"] = (
            count,
            e.grfAccessPermissions,
            e.grfAccessMode,
            e.grfInheritance,
            e.Trustee.TrusteeForm,
            e.Trustee.TrusteeType,
            e.Trustee.ptstrName,
            old.value,
        )
        out(new).value = 44
        return set_entries

    def set_named(path, kind, info, owner, group, dacl, sacl):
        seen["set"] = (path, kind, info, owner, group, dacl.value, sacl)
        return set_info

    win.handlers["GetNamedSecurityInfoW"] = get_info
    win.handlers["ConvertStringSidToSidW"] = convert
    win.handlers["SetEntriesInAclW"] = entries
    win.handlers["SetNamedSecurityInfoW"] = set_named
    return seen


def test_a_grant_to_a_directory_is_modify_inherited_by_what_is_under_it(win, tmp_path):
    seen = install_acl(win)
    winapi.grant(tmp_path, "S-1-15-2-9", write=True)
    assert seen["get"] == (str(tmp_path), 1, 0x4)
    # count, mask, GRANT_ACCESS, sub-containers and objects inherit, SID form, unknown type,
    # the SID, the existing list.
    assert seen["entry"] == (1, 0x1301BF, 1, 3, 0, 0, 33, 11)
    # Written back to the path as its new access list, to be passed down.
    assert seen["set"] == (str(tmp_path), 1, 0x4, None, None, 44, None)


def test_a_read_grant_to_a_file_is_read_and_execute_and_inherited_by_nothing(win, tmp_path):
    seen = install_acl(win)
    file = tmp_path / "a.txt"
    file.write_text("x")
    winapi.grant(file, "S-1-15-2-9", write=False)
    assert seen["entry"][:4] == (1, 0x1200A9, 1, 0)


def test_a_revoke_removes_every_entry_of_the_sid(win, tmp_path):
    seen = install_acl(win)
    winapi.revoke(tmp_path, "S-1-15-2-9")
    assert seen["entry"][:4] == (1, 0, 4, 3)
    assert seen["set"][5] == 44


def test_a_grant_frees_the_descriptor_the_sid_and_the_new_list(win, tmp_path):
    install_acl(win)
    winapi.grant(tmp_path, "S-1-15-2-9", write=False)
    assert sorted(win.freed) == [("LocalFree", 22), ("LocalFree", 33), ("LocalFree", 44)]


def test_a_path_whose_access_list_cannot_be_read_raises_and_frees_nothing(win, tmp_path):
    install_acl(win, get=5)
    with pytest.raises(OSError, match=r"GetNamedSecurityInfoW .*: error 5"):
        winapi.grant(tmp_path, "S-1-15-2-9", write=True)
    assert win.freed == []
    assert win.called("SetEntriesInAclW") == []


def test_a_path_whose_access_list_cannot_be_written_raises_and_frees_all(win, tmp_path):
    install_acl(win, set_info=5)
    with pytest.raises(OSError, match=r"SetNamedSecurityInfoW .*: error 5"):
        winapi.revoke(tmp_path, "S-1-15-2-9")
    assert sorted(win.freed) == [("LocalFree", 22), ("LocalFree", 33), ("LocalFree", 44)]


def test_a_list_that_cannot_be_extended_raises_and_frees_the_descriptor_and_sid(win, tmp_path):
    install_acl(win, set_entries=87)
    with pytest.raises(OSError, match="SetEntriesInAclW: error 87"):
        winapi.grant(tmp_path, "S-1-15-2-9", write=True)
    assert win.called("SetNamedSecurityInfoW") == []
    assert sorted(win.freed) == [("LocalFree", 22), ("LocalFree", 33)]


def test_a_malformed_sid_is_refused_before_the_list_is_touched(win, tmp_path):
    install_acl(win)
    win.handlers["ConvertStringSidToSidW"] = lambda text, pointer: 0
    with pytest.raises(OSError, match="ConvertStringSidToSidW nope"):
        winapi.grant(tmp_path, "nope", write=True)
    assert win.called("SetEntriesInAclW") == []
    assert win.freed == [("LocalFree", 22)]


def test_a_path_with_unicode_is_passed_as_it_is(win, tmp_path):
    seen = install_acl(win)
    folder = tmp_path / "pliki-żółć"
    folder.mkdir()
    winapi.grant(folder, "S-1-15-2-9", write=False)
    assert seen["get"][0] == str(folder)
    assert seen["set"][0] == str(folder)


# ── integrity labels ──────────────────────────────────────────────────


def install_label(win: Win32, *, sddl: str | None, get: int = 0, sacl: int = 0):
    seen: dict = {}

    def get_info(path, kind, info, *rest):
        seen["get"] = (path, kind, info)
        out(rest[4]).value = 22
        return get

    def to_string(descriptor, revision, info, text, length):
        seen["to_string"] = (descriptor.value, revision, info)
        out(text).value = sddl
        return 1

    def from_string(text, revision, descriptor, length):
        seen["from"] = (text, revision)
        out(descriptor).value = 22
        return 1

    def get_sacl(descriptor, present, pointer, defaulted):
        out(pointer).value = sacl
        return 1

    def initialize(buffer, length, revision):
        seen["init"] = (length, revision)
        return 1

    def set_info(path, kind, info, owner, group, dacl, acl):
        seen["set"] = (path, kind, info, owner, group, dacl, acl.value if acl else None)
        return 0

    win.handlers["GetNamedSecurityInfoW"] = get_info
    win.handlers["ConvertSecurityDescriptorToStringSecurityDescriptorW"] = to_string
    win.handlers["ConvertStringSecurityDescriptorToSecurityDescriptorW"] = from_string
    win.handlers["GetSecurityDescriptorSacl"] = get_sacl
    win.handlers["InitializeAcl"] = initialize
    win.handlers["SetNamedSecurityInfoW"] = set_info
    return seen


def test_a_label_is_read_as_sddl_and_the_descriptor_and_text_are_freed(win, tmp_path):
    seen = install_label(win, sddl="S:(ML;;NW;;;LW)")
    assert winapi.label(tmp_path) == "S:(ML;;NW;;;LW)"
    assert seen["get"] == (str(tmp_path), 1, 0x10)
    assert seen["to_string"] == (22, 1, 0x10)
    assert ("LocalFree", 22) in win.freed
    assert len(win.freed) == 2


def test_a_path_with_no_label_reads_as_empty_text(win, tmp_path):
    install_label(win, sddl=None)
    assert winapi.label(tmp_path) == ""
    assert win.freed == [("LocalFree", 22)]


def test_a_label_that_cannot_be_read_raises_and_frees_nothing(win, tmp_path):
    install_label(win, sddl="S:", get=2)
    with pytest.raises(OSError, match=r"GetNamedSecurityInfoW .*: error 2"):
        winapi.label(tmp_path)
    assert win.freed == []


def test_a_label_is_set_from_its_sddl(win, tmp_path):
    seen = install_label(win, sddl="", sacl=77)
    winapi.set_label(tmp_path, "S:(ML;OICI;NW;;;LW)")
    assert seen["from"] == ("S:(ML;OICI;NW;;;LW)", 1)
    # Only the label is written, from the descriptor's own list, with no empty one made.
    assert seen["set"] == (str(tmp_path), 1, 0x10, None, None, None, 77)
    assert "init" not in seen
    assert win.freed == [("LocalFree", 22)]


@pytest.mark.parametrize("sddl", ["", "S:"])
def test_an_empty_label_installs_an_empty_list_that_removes_the_explicit_label(win, tmp_path, sddl):
    seen = install_label(win, sddl="", sacl=0)
    winapi.set_label(tmp_path, sddl)
    assert seen["from"] == ("S:", 1)
    assert seen["init"] == (64, 2)
    assert seen["set"][:6] == (str(tmp_path), 1, 0x10, None, None, None)
    assert seen["set"][6]  # a real, non-null list
    assert win.freed == [("LocalFree", 22)]


def test_a_malformed_sddl_raises_before_anything_is_written(win, tmp_path):
    install_label(win, sddl="")
    win.handlers["ConvertStringSecurityDescriptorToSecurityDescriptorW"] = lambda *a: 0
    with pytest.raises(OSError, match="ConvertStringSecurityDescriptorToSecurityDescriptorW junk"):
        winapi.set_label(tmp_path, "junk")
    assert win.called("SetNamedSecurityInfoW") == []


def test_a_label_that_cannot_be_written_raises_and_frees_the_descriptor(win, tmp_path):
    install_label(win, sddl="")
    win.handlers["SetNamedSecurityInfoW"] = lambda *a: 5
    with pytest.raises(OSError, match=r"SetNamedSecurityInfoW \(label\) .*: error 5"):
        winapi.set_label(tmp_path, "S:")
    assert win.freed == [("LocalFree", 22)]


# ── processes ─────────────────────────────────────────────────────────


def install_process(win: Win32, *, handle: int, code: int | None, error: int = 0):
    def open_process(access, inherit, pid):
        win.opened = (access, inherit, pid)
        win.last_error = error
        return handle

    def exit_code(h, pointer):
        if code is None:
            return 0
        out(pointer).value = code
        return 1

    win.handlers["OpenProcess"] = open_process
    win.handlers["GetExitCodeProcess"] = exit_code


def test_a_running_process_is_alive_and_its_handle_is_closed(win):
    install_process(win, handle=7, code=259)
    assert winapi.process_alive(4242) is True
    assert win.opened == (0x1000, False, 4242)
    assert win.freed == [("CloseHandle", 7)]


def test_an_exited_process_is_not_alive_and_its_handle_is_closed(win):
    install_process(win, handle=7, code=0)
    assert winapi.process_alive(4242) is False
    assert win.freed == [("CloseHandle", 7)]


def test_a_process_that_exited_with_any_other_code_is_not_alive(win):
    install_process(win, handle=7, code=1)
    assert winapi.process_alive(4242) is False


def test_a_process_whose_exit_code_cannot_be_read_counts_alive(win):
    install_process(win, handle=7, code=None)
    assert winapi.process_alive(4242) is True
    assert win.freed == [("CloseHandle", 7)]


def test_a_process_igni_may_not_open_counts_alive(win):
    install_process(win, handle=0, code=0, error=5)
    assert winapi.process_alive(4) is True
    assert win.freed == []


@pytest.mark.parametrize("pid", [999999, 0, -1])
def test_a_process_that_is_gone_or_not_a_process_is_not_alive(win, pid):
    install_process(win, handle=0, code=0, error=87)
    assert winapi.process_alive(pid) is False
    assert win.freed == []
