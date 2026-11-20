"""Windows presentation details: native-DPI rendering, DWM rounded corners,
raising the window that hosts a watched process.

A DPI-unaware process is stretched by the desktop compositor, which is what makes
the HUD blurry, so the process claims DPI awareness before Tk starts and every
design pixel goes through px(). Corner rounding is delegated to DWM because a
colour-key window can only cut pixels on or off: an anti-aliased arc is impossible
without per-pixel alpha, which tkinter cannot give a window.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import tkinter as tk

from core.pid import console_of, processes

_CORNER_ATTRIBUTE = 33
_CORNER_ROUND = 2
_SW_RESTORE = 9
_SW_MINIMIZE = 6
_CLOAKED = 14
# The desktop and the taskbar own visible titled windows too; raising them is never
# what a click on a session card meant.
_NON_APP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Button", "NotifyIconOverflowWindow"}

_factor = 1.0


def become_aware() -> None:
    """Call before the first Tk window exists; harmless on other platforms."""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    attempts = (
        lambda: user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
        user32.SetProcessDPIAware,
    )
    for attempt in attempts:
        try:
            if attempt():
                return
        except (OSError, AttributeError):
            continue


def scale(root: tk.Misc) -> float:
    """Record the device factor (1.0 at 96 dpi) and return it."""
    global _factor
    try:
        _factor = float(root.winfo_fpixels("1i")) / 96.0
    except (tk.TclError, ValueError):
        _factor = 1.0
    return _factor


def px(design: float) -> int:
    """Design pixels -> device pixels."""
    return int(round(design * _factor))


def device(design: float) -> int:
    """Device pixels -> design pixels, for persisting what Tk reports back."""
    return int(round(design / _factor))


def round_corners(win: tk.Misc) -> bool:
    """Ask DWM for anti-aliased corners. False on Windows 10, where it stays square."""
    if sys.platform != "win32":
        return False
    try:
        hwnd = _toplevel_hwnd(win)
    except tk.TclError:
        return False
    value = ctypes.c_int(_CORNER_ROUND)
    try:
        error = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, _CORNER_ATTRIBUTE, ctypes.byref(value), ctypes.sizeof(value)
        )
    except OSError:
        return False
    return error == 0


def _toplevel_hwnd(win: tk.Misc) -> int:
    user32 = ctypes.windll.user32
    hwnd = win.winfo_id()
    while True:
        parent = user32.GetParent(hwnd)
        if not parent:
            return hwnd
        hwnd = parent


# -- raising the window that hosts another process ---------------------------

# Windows the user was actually working in, oldest first, our own HUD excluded.
_foreground: list[int] = []
# The jump we performed last: (session key, raised window, where to go back, was it minimised).
_jump: tuple[str, int, int, bool] | None = None


def track_foreground(win: tk.Misc) -> None:
    """Tick from the animation loop: the click lands on the HUD, so the window the
    user came from is only knowable from what was foreground a moment earlier."""
    if sys.platform != "win32":
        return
    try:
        own = _toplevel_hwnd(win)
    except tk.TclError:
        return
    current = ctypes.windll.user32.GetForegroundWindow()
    if not current or current == own:
        return
    if _foreground and _foreground[-1] == current:
        return
    _foreground.append(current)
    del _foreground[:-8]


def focus_process(pid: int, key: str) -> str:
    """Raise the window hosting `pid`; a second click on the same session goes back.

    Returns "" (nothing to raise), "raised", "back" or "tab" (its window came up, but
    the terminal is showing one of its other tabs — a tab has no addressable handle).
    """
    if sys.platform != "win32" or pid <= 0:
        return ""
    user32 = ctypes.windll.user32
    console, title = console_of(pid)
    hwnd = _host_window(pid, console)
    if not hwnd:
        return ""
    global _jump
    last_key, raised, back, was_iconic = _jump or ("", 0, 0, False)
    if key == last_key and _active_window() == hwnd and user32.IsWindow(back):
        _jump = None
        if was_iconic:
            user32.ShowWindow(raised, _SW_MINIMIZE)
        _set_foreground(back)
        return "back"
    iconic = bool(user32.IsIconic(hwnd))
    if iconic:
        user32.ShowWindow(hwnd, _SW_RESTORE)
    if not _set_foreground(hwnd):
        return ""
    _jump = (key, hwnd, _window_behind(hwnd), iconic)
    # The window is only ever titled after its active tab, plus whatever the terminal adds.
    return "raised" if not title or title in _window_title(hwnd) else "tab"


def _window_title(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value.strip()


def _active_window() -> int:
    """The window the user is looking at, HUD clicks included.

    Pressing a card activates the borderless HUD itself, so the instantaneous
    foreground window is the wrong question; the tracked history says where the user
    actually was a few milliseconds ago.
    """
    user32 = ctypes.windll.user32
    for candidate in reversed(_foreground):
        if user32.IsWindow(candidate):
            return candidate
    return user32.GetForegroundWindow()


def _window_behind(hwnd: int) -> int:
    """Where the user was before this jump, skipping the window being raised."""
    user32 = ctypes.windll.user32
    for candidate in reversed(_foreground):
        if candidate != hwnd and user32.IsWindow(candidate):
            return candidate
    return 0


def _host_window(pid: int, console: int) -> int:
    """The window hosting `pid`: its console's owner first, else an ancestor's.

    The console is authoritative — a shell in the same session can own an unrelated
    console window of its own, and the z-ordered ancestor walk would happily pick that.
    The stub handle is never the answer itself: it is 0x0 and untitled, so it is only
    trusted once it resolves to a window that passed `_app_windows()`.
    """
    windows = _app_windows()
    if console:
        stub = ctypes.windll.user32.GetParent(console) or console
        if stub in windows:
            return stub
    chain, current = [], pid
    parents = {proc: ppid for proc, (_, ppid) in processes().items()}
    while current and current not in chain:
        chain.append(current)
        current = parents.get(current, 0)
    for hwnd in windows:
        if _window_pid(hwnd) in chain:
            return hwnd
    return 0


def _window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _app_windows() -> list[int]:
    """Visible, uncloaked, titled top-level windows in front-to-back order."""
    user32 = ctypes.windll.user32
    found: list[int] = []

    def keep(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or not user32.GetWindowTextLengthW(hwnd):
            return True
        cloak = wt.DWORD()
        try:
            ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, _CLOAKED, ctypes.byref(cloak), 4)
        except OSError:
            cloak.value = 0
        if cloak.value:
            return True
        buffer = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buffer, 64)
        if buffer.value in _NON_APP_CLASSES:
            return True
        found.append(hwnd)
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(keep)
    user32.EnumWindows(callback, 0)
    return found


def _set_foreground(hwnd: int) -> bool:
    """SetForegroundWindow is refused for a background process, and the HUD never is
    one: borrowing the foreground thread's input queue is what makes it stick."""
    user32 = ctypes.windll.user32
    if user32.GetForegroundWindow() == hwnd:
        return True
    kernel32 = ctypes.windll.kernel32
    mine = kernel32.GetCurrentThreadId()
    holder = user32.GetForegroundWindow()
    target = user32.GetWindowThreadProcessId(holder, None) if holder else 0
    attached = bool(target) and bool(user32.AttachThreadInput(mine, target, True))
    try:
        user32.BringWindowToTop(hwnd)
        return bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(mine, target, False)
