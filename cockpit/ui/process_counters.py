"""This process's memory and handle counts, read from Windows (Patch 14 §10.1).

Each count is read by its own call. A call that fails, or any call off Windows, leaves its field absent;
nothing here raises.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_MB = 1024.0 * 1024.0
_GR_GDIOBJECTS = 0
_GR_USEROBJECTS = 1


@dataclass(frozen=True)
class ProcessCounters:
    working_set_mb: Optional[float] = None
    private_mb: Optional[float] = None
    handles: Optional[int] = None
    gdi_objects: Optional[int] = None
    user_objects: Optional[int] = None


if sys.platform == "win32":
    from ctypes import wintypes

    class _ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    def _bind():
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCountersEx), wintypes.DWORD,
        ]
        user32.GetGuiResources.restype = wintypes.DWORD
        user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        return kernel32, psapi, user32

    try:
        _KERNEL32, _PSAPI, _USER32 = _bind()
    except (OSError, AttributeError):
        _KERNEL32 = _PSAPI = _USER32 = None


def read_process_counters() -> ProcessCounters:
    """Reads this process's memory and handle counts from Windows.

    post: each field is the current value, or None when its call fails or the platform isn't Windows
    raises: nothing
    """
    if sys.platform != "win32" or _KERNEL32 is None:
        return ProcessCounters()
    try:
        process = _KERNEL32.GetCurrentProcess()
    except OSError:
        return ProcessCounters()

    working_set_mb = private_mb = None
    try:
        memory = _ProcessMemoryCountersEx()
        memory.cb = ctypes.sizeof(memory)
        if _PSAPI.GetProcessMemoryInfo(process, ctypes.byref(memory), memory.cb):
            working_set_mb = round(memory.WorkingSetSize / _MB, 1)
            private_mb = round(memory.PrivateUsage / _MB, 1)
    except OSError:
        pass

    handles = None
    try:
        count = wintypes.DWORD(0)
        if _KERNEL32.GetProcessHandleCount(process, ctypes.byref(count)):
            handles = int(count.value)
    except OSError:
        pass

    gdi_objects = user_objects = None
    try:
        gdi_objects = int(_USER32.GetGuiResources(process, _GR_GDIOBJECTS))
        user_objects = int(_USER32.GetGuiResources(process, _GR_USEROBJECTS))
    except OSError:
        gdi_objects = user_objects = None

    return ProcessCounters(
        working_set_mb=working_set_mb,
        private_mb=private_mb,
        handles=handles,
        gdi_objects=gdi_objects,
        user_objects=user_objects,
    )
