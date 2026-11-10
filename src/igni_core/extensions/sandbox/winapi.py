"""Windows: the Win32 calls igni itself makes for an AppContainer, through ctypes.

What igni does before and after a run (:mod:`.appcontainer`): create and
delete the container's profile, name its SID, add and remove that SID's
entries in a path's access list, set and restore a path's integrity
label, and ask whether a process is alive. Starting the process, its
job object and its pipes are the runner's (:mod:`.winbridge`), which is
a separate script.

No dependency beyond the standard library. Importable everywhere; every
function raises :class:`OSError` off Windows, so nothing here is called
on macOS or Linux by accident.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

WINDOWS = sys.platform == "win32"

#: ``HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)``: the profile is there already.
_HR_ALREADY_EXISTS = 0x800700B7
#: ``HRESULT_FROM_WIN32(ERROR_NOT_FOUND)`` and ``(ERROR_FILE_NOT_FOUND)``.
_HR_NOT_FOUND = (0x80070490, 0x80070002)

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x4
LABEL_SECURITY_INFORMATION = 0x10
SDDL_REVISION_1 = 1
ACL_REVISION = 2

GRANT_ACCESS = 1
REVOKE_ACCESS = 4
NO_INHERITANCE = 0
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 3
TRUSTEE_IS_SID = 0
TRUSTEE_IS_UNKNOWN = 0

#: What ``icacls`` calls ``RX``: read, list, traverse and execute.
FILE_READ_EXECUTE = 0x1200A9
#: What ``icacls`` calls ``M``: read, write, delete — not change permissions.
FILE_MODIFY = 0x1301BF

ERROR_ACCESS_DENIED = 5
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x8
TOKEN_USER = 1
TOKEN_IS_APP_CONTAINER = 29


class TRUSTEE_W(ctypes.Structure):  # noqa: N801 — the Win32 name
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", ctypes.c_void_p),
    ]


class EXPLICIT_ACCESS_W(ctypes.Structure):  # noqa: N801 — the Win32 name
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", TRUSTEE_W),
    ]


def _require_windows() -> None:
    if not WINDOWS:
        raise OSError("this is a Windows call and this is not Windows")


_dlls: dict[str, Any] = {}


def _fn(dll: str, name: str, restype: Any, *argtypes: Any) -> Any:
    """``dll!name`` with its prototype set, so handles and pointers keep 64 bits."""
    _require_windows()
    lib = _dlls.get(dll)
    if lib is None:
        lib = _dlls[dll] = ctypes.WinDLL(dll, use_last_error=True)
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


def _check(ok: Any, what: str) -> None:
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error(), what)


def _check_status(status: int, what: str) -> None:
    """For calls that return a Win32 error code rather than set the last error."""
    if status != 0:
        raise ctypes.WinError(status, what)


def _local_free(pointer: Any) -> None:
    if pointer:
        _fn("kernel32", "LocalFree", ctypes.c_void_p, ctypes.c_void_p)(pointer)


# ── SIDs and profiles ─────────────────────────────────────────────────


def _sid_to_string(psid: Any) -> str:
    text = ctypes.c_wchar_p()
    convert = _fn(
        "advapi32", "ConvertSidToStringSidW", wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p
    )
    _check(convert(psid, ctypes.byref(text)), "ConvertSidToStringSidW")
    try:
        return str(text.value)
    finally:
        _local_free(ctypes.cast(text, ctypes.c_void_p))


class OwnedSid:
    """A SID from its string form, freed when the ``with`` block ends."""

    def __init__(self, sid: str) -> None:
        self.text = sid
        self.pointer = ctypes.c_void_p()

    def __enter__(self) -> OwnedSid:
        convert = _fn(
            "advapi32", "ConvertStringSidToSidW", wintypes.BOOL, wintypes.LPCWSTR, ctypes.c_void_p
        )
        _check(
            convert(self.text, ctypes.byref(self.pointer)), f"ConvertStringSidToSidW {self.text}"
        )
        return self

    def __exit__(self, *_: object) -> None:
        _local_free(self.pointer)


def create_profile(name: str, display: str, description: str) -> str:
    """Create the AppContainer profile ``name`` (or find it) and return its SID."""
    psid = ctypes.c_void_p()
    create = _fn(
        "userenv",
        "CreateAppContainerProfile",
        ctypes.c_long,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    hr = create(name, display, description, None, 0, ctypes.byref(psid)) & 0xFFFFFFFF
    if hr == _HR_ALREADY_EXISTS:
        derive = _fn(
            "userenv",
            "DeriveAppContainerSidFromAppContainerName",
            ctypes.c_long,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
        )
        hr = derive(name, ctypes.byref(psid)) & 0xFFFFFFFF
    if hr != 0:
        raise OSError(f"CreateAppContainerProfile {name} failed: HRESULT 0x{hr:08X}")
    try:
        return _sid_to_string(psid)
    finally:
        _fn("advapi32", "FreeSid", ctypes.c_void_p, ctypes.c_void_p)(psid)


def delete_profile(name: str) -> None:
    """Delete the AppContainer profile ``name``; one already gone is no error."""
    delete = _fn("userenv", "DeleteAppContainerProfile", ctypes.c_long, wintypes.LPCWSTR)
    hr = delete(name) & 0xFFFFFFFF
    if hr != 0 and hr not in _HR_NOT_FOUND:
        raise OSError(f"DeleteAppContainerProfile {name} failed: HRESULT 0x{hr:08X}")


def appcontainer_api_present() -> None:
    """Raise unless this Windows has the AppContainer profile calls."""
    _require_windows()
    lib = _dlls.get("userenv")
    if lib is None:
        lib = _dlls["userenv"] = ctypes.WinDLL("userenv", use_last_error=True)
    for name in (
        "CreateAppContainerProfile",
        "DeleteAppContainerProfile",
        "DeriveAppContainerSidFromAppContainerName",
    ):
        if not hasattr(lib, name):
            raise OSError(f"userenv.dll has no {name}")


def _token_information(cls: int) -> ctypes.Array[ctypes.c_char]:
    process = _fn("kernel32", "GetCurrentProcess", wintypes.HANDLE)()
    token = wintypes.HANDLE()
    open_token = _fn(
        "advapi32",
        "OpenProcessToken",
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    _check(open_token(process, TOKEN_QUERY, ctypes.byref(token)), "OpenProcessToken")
    get = _fn(
        "advapi32",
        "GetTokenInformation",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    try:
        size = wintypes.DWORD()
        get(token, cls, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(max(size.value, 4))
        _check(get(token, cls, buffer, size, ctypes.byref(size)), "GetTokenInformation")
        return buffer
    finally:
        _fn("kernel32", "CloseHandle", wintypes.BOOL, wintypes.HANDLE)(token)


def current_user_sid() -> str:
    """The SID of the account igni runs as."""
    buffer = _token_information(TOKEN_USER)
    # TOKEN_USER is one SID_AND_ATTRIBUTES; its first member is the PSID.
    psid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
    return _sid_to_string(ctypes.c_void_p(psid))


def in_appcontainer() -> bool:
    """Whether igni itself runs in an AppContainer (which cannot start another)."""
    buffer = _token_information(TOKEN_IS_APP_CONTAINER)
    return bool(ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0])


# ── Access lists and labels ───────────────────────────────────────────


def _edit_dacl(path: Path, sid: str, mode: int, access: int) -> None:
    get = _fn(
        "advapi32",
        "GetNamedSecurityInfoW",
        wintypes.DWORD,
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    set_entries = _fn(
        "advapi32",
        "SetEntriesInAclW",
        wintypes.DWORD,
        wintypes.ULONG,
        ctypes.POINTER(EXPLICIT_ACCESS_W),
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    set_info = _fn(
        "advapi32",
        "SetNamedSecurityInfoW",
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    old_dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    new_dacl = ctypes.c_void_p()
    _check_status(
        get(
            str(path),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(old_dacl),
            None,
            ctypes.byref(descriptor),
        ),
        f"GetNamedSecurityInfoW {path}",
    )
    try:
        with OwnedSid(sid) as owned:
            entry = EXPLICIT_ACCESS_W()
            entry.grfAccessPermissions = access
            entry.grfAccessMode = mode
            entry.grfInheritance = (
                SUB_CONTAINERS_AND_OBJECTS_INHERIT if path.is_dir() else NO_INHERITANCE
            )
            entry.Trustee.TrusteeForm = TRUSTEE_IS_SID
            entry.Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN
            entry.Trustee.ptstrName = owned.pointer
            _check_status(
                set_entries(1, ctypes.byref(entry), old_dacl, ctypes.byref(new_dacl)),
                "SetEntriesInAclW",
            )
            try:
                # Writes the access list, and propagates its inheritable
                # entries to everything below a directory.
                _check_status(
                    set_info(
                        str(path),
                        SE_FILE_OBJECT,
                        DACL_SECURITY_INFORMATION,
                        None,
                        None,
                        new_dacl,
                        None,
                    ),
                    f"SetNamedSecurityInfoW {path}",
                )
            finally:
                _local_free(new_dacl)
    finally:
        _local_free(descriptor)


def grant(path: Path, sid: str, *, write: bool) -> None:
    """Give ``sid`` read (and execute), or modify, on ``path`` and everything under it."""
    _edit_dacl(path, sid, GRANT_ACCESS, FILE_MODIFY if write else FILE_READ_EXECUTE)


def revoke(path: Path, sid: str) -> None:
    """Remove every explicit entry for ``sid`` on ``path``, and what it passed down."""
    _edit_dacl(path, sid, REVOKE_ACCESS, 0)


def label(path: Path) -> str:
    """``path``'s integrity label as SDDL (``S:…``), empty when it has none."""
    get = _fn(
        "advapi32",
        "GetNamedSecurityInfoW",
        wintypes.DWORD,
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    to_string = _fn(
        "advapi32",
        "ConvertSecurityDescriptorToStringSecurityDescriptorW",
        wintypes.BOOL,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    descriptor = ctypes.c_void_p()
    _check_status(
        get(
            str(path),
            SE_FILE_OBJECT,
            LABEL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            ctypes.byref(descriptor),
        ),
        f"GetNamedSecurityInfoW {path}",
    )
    try:
        text = ctypes.c_wchar_p()
        _check(
            to_string(
                descriptor,
                SDDL_REVISION_1,
                LABEL_SECURITY_INFORMATION,
                ctypes.byref(text),
                None,
            ),
            "ConvertSecurityDescriptorToStringSecurityDescriptorW",
        )
        try:
            return str(text.value or "")
        finally:
            _local_free(ctypes.cast(text, ctypes.c_void_p))
    finally:
        _local_free(descriptor)


def set_label(path: Path, sddl: str) -> None:
    """Set ``path``'s integrity label from SDDL; ``"S:"`` removes an explicit one."""
    from_string = _fn(
        "advapi32",
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        wintypes.BOOL,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    get_sacl = _fn(
        "advapi32",
        "GetSecurityDescriptorSacl",
        wintypes.BOOL,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
    )
    set_info = _fn(
        "advapi32",
        "SetNamedSecurityInfoW",
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    descriptor = ctypes.c_void_p()
    _check(
        from_string(sddl or "S:", SDDL_REVISION_1, ctypes.byref(descriptor), None),
        f"ConvertStringSecurityDescriptorToSecurityDescriptorW {sddl}",
    )
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        sacl = ctypes.c_void_p()
        _check(
            get_sacl(
                descriptor, ctypes.byref(present), ctypes.byref(sacl), ctypes.byref(defaulted)
            ),
            "GetSecurityDescriptorSacl",
        )
        empty = ctypes.create_string_buffer(64)
        if not sacl:
            init = _fn(
                "advapi32",
                "InitializeAcl",
                wintypes.BOOL,
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.DWORD,
            )
            _check(init(empty, len(empty), ACL_REVISION), "InitializeAcl")
            sacl = ctypes.cast(empty, ctypes.c_void_p)
        _check_status(
            set_info(str(path), SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION, None, None, None, sacl),
            f"SetNamedSecurityInfoW (label) {path}",
        )
    finally:
        _local_free(descriptor)


# ── Processes ─────────────────────────────────────────────────────────


def process_alive(pid: int) -> bool:
    """Whether a process with this id is running (one we may not open counts)."""
    open_process = _fn(
        "kernel32", "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
    )
    handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        exit_code = _fn(
            "kernel32",
            "GetExitCodeProcess",
            wintypes.BOOL,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        if not exit_code(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        _fn("kernel32", "CloseHandle", wintypes.BOOL, wintypes.HANDLE)(handle)


__all__ = [
    "WINDOWS",
    "appcontainer_api_present",
    "create_profile",
    "current_user_sid",
    "delete_profile",
    "grant",
    "in_appcontainer",
    "label",
    "process_alive",
    "revoke",
    "set_label",
]
