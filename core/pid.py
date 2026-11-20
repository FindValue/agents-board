"""PID liveness and process facts without third-party deps.

On Windows this uses kernel32 directly. os.kill() must never be used here:
for non-CTRL signals CPython calls TerminateProcess, which would kill the
agent CLI we are watching.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_QUERY_VM_READ = 0x0400 | 0x0010  # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_SNAP_PROCESS = 0x2
# x64 only: PEB->ProcessParameters, and the UNICODE_STRING inside the cwd CURDIR.
_PEB_PROCESS_PARAMS = 0x20
_CURDIR_OFFSET = 0x38

_kernel32 = None
_reads = None


def _api():
    global _kernel32
    if _kernel32 is None:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel32.OpenProcess.restype = wt.HANDLE
        _kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        _kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        _kernel32.CloseHandle.argtypes = [wt.HANDLE]
    return _kernel32


def _read_api():
    """kernel32/ntdll with the handle-sized signatures ctypes cannot guess."""
    global _reads
    if _reads is None:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenProcess.restype = wt.HANDLE
        kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.ReadProcessMemory.argtypes = [
            wt.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtQueryInformationProcess.argtypes = [
            wt.HANDLE,
            wt.ULONG,
            ctypes.c_void_p,
            wt.ULONG,
            ctypes.POINTER(wt.ULONG),
        ]
        _reads = (kernel32, ntdll)
    return _reads


def is_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    api = _api()
    handle = api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle or handle == _INVALID_HANDLE_VALUE:
        # A live protected process (e.g. pid 4) is refused rather than missing.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wt.DWORD()
        if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        api.CloseHandle(handle)


def processes() -> dict[int, tuple[str, int]]:
    """pid -> (exe base name, parent pid) for every process; {} off Windows.

    One Toolhelp32 snapshot is the only cheap way to see other processes' names, and
    both the window raiser and the adapters need it (a CLI's own pid is never the one
    owning a window, and some CLIs do not record their pid at all).
    """
    if sys.platform != "win32":
        return {}
    kernel32 = ctypes.windll.kernel32

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(_SNAP_PROCESS, 0)
    if snap == -1 or not snap:
        return {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        found: dict[int, tuple[str, int]] = {}
        walk = kernel32.Process32First(snap, ctypes.byref(entry))
        while walk:
            found[entry.th32ProcessID] = (
                entry.szExeFile.decode("mbcs", "replace").lower(),
                entry.th32ParentProcessID,
            )
            walk = kernel32.Process32Next(snap, ctypes.byref(entry))
        return found
    finally:
        kernel32.CloseHandle(snap)


def console_of(pid: int) -> tuple[int, str]:
    """(console window, tab title) of `pid`; (0, "") when it owns no console.

    Attaching needs a console-less caller, which is the usual case for the HUD; an
    editor-hosted session simply has no console to read. The handle is the hidden
    stub conhost or Windows Terminal parks behind the real window, never it.
    """
    if sys.platform != "win32" or pid <= 0:
        return 0, ""
    kernel32 = ctypes.windll.kernel32
    if not kernel32.AttachConsole(pid):
        return 0, ""
    try:
        buffer = ctypes.create_unicode_buffer(512)
        kernel32.GetConsoleTitleW(buffer, 512)
        title = buffer.value.strip()
        hwnd = kernel32.GetConsoleWindow()
    finally:
        kernel32.FreeConsole()
    # A console nobody renamed reports the executable path, which no tab will ever show.
    return hwnd, ("" if "\\" in title else title)


def cwd_of(pid: int) -> str:
    """The process's current directory, read out of its PEB; "" when unreadable.

    A CLI that persists no pid (opencode) can only be matched to its terminal by where
    it was launched from, and the tab title is a constant. Elevated or foreign processes
    refuse the read, which is a normal "" here, not an error.
    """
    if sys.platform != "win32" or pid <= 0 or ctypes.sizeof(ctypes.c_void_p) != 8:
        return ""
    kernel32, ntdll = _read_api()
    handle = kernel32.OpenProcess(_QUERY_VM_READ, False, pid)
    if not handle:
        return ""

    class Basic(ctypes.Structure):
        _fields_ = [
            ("ExitStatus", ctypes.c_long),
            ("PebBaseAddress", ctypes.c_void_p),
            ("AffinityMask", ctypes.c_size_t),
            ("BasePriority", ctypes.c_size_t),
            ("UniqueProcessId", ctypes.c_size_t),
            ("InheritedFromUniqueProcessId", ctypes.c_size_t),
        ]

    def read(addr, size: int) -> bytes | None:
        if not addr:
            return None
        buffer = ctypes.create_string_buffer(size)
        done = ctypes.c_size_t()
        ok = kernel32.ReadProcessMemory(handle, addr, buffer, size, ctypes.byref(done))
        return buffer.raw[: done.value] if ok and done.value == size else None

    try:
        info = Basic()
        if ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            return ""
        pointers = read(info.PebBaseAddress + _PEB_PROCESS_PARAMS, 8)
        if not pointers:
            return ""
        header = read(int.from_bytes(pointers, "little") + _CURDIR_OFFSET, 16)
        if not header:  # UNICODE_STRING: length, reserved, buffer pointer
            return ""
        length = int.from_bytes(header[:2], "little")
        if not length:
            return ""
        raw = read(int.from_bytes(header[8:16], "little"), length)
        if not raw:
            return ""
        return raw.decode("utf-16-le", "replace").strip().rstrip("\\")
    finally:
        kernel32.CloseHandle(handle)


class Liveness:
    """is_alive() results reused for a few seconds, keyed by PID."""

    def __init__(self, ttl: float = 5.0) -> None:
        self._ttl = ttl
        self._seen: dict[int, tuple[float, bool]] = {}

    def check(self, pid: int, now: float) -> bool:
        seen = self._seen.get(pid)
        if seen and now - seen[0] < self._ttl:
            return seen[1]
        result = is_alive(pid)
        if len(self._seen) > 200:
            self._seen.clear()
        self._seen[pid] = (now, result)
        return result
