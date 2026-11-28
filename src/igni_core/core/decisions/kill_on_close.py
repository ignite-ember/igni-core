"""Windows: a job object that ends the decisions service when its backend ends, however it ends.

Importable everywhere; off Windows :meth:`KillOnClose.attach` does nothing.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
from ctypes import wintypes
from typing import Any

logger = logging.getLogger(__name__)

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 — the Win32 name
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):  # noqa: N801 — the Win32 name
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 — the Win32 name
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class KillOnClose:
    """A job whose one handle is this process's: the kernel ends its members when we end."""

    def __init__(self) -> None:
        self._handle: Any = None

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        """Put ``process`` in the job; on failure it is said and the other lifelines remain."""
        if sys.platform != "win32":
            return
        try:
            kernel32 = self._kernel32()
            if not self._handle:
                self._handle = self._job(kernel32)
            handle = int(process._handle)  # type: ignore[attr-defined]  # noqa: SLF001
            if not kernel32.AssignProcessToJobObject(self._handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        except OSError as exc:
            logger.warning("decisions: no kill-on-close job for the service (%s)", exc)

    @staticmethod
    def _kernel32() -> Any:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        return kernel32

    @staticmethod
    def _job(kernel32: Any) -> Any:
        """A new job that kills its members when its last handle closes."""
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise ctypes.WinError(error)  # type: ignore[attr-defined]
        return job


__all__ = ["KillOnClose"]
