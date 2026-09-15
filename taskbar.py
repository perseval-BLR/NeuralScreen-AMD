"""TaskbarWindow - a taskbar button for the program.

The overlay is a borderless click-through window and the worker window is
a tool window, so neither shows in the taskbar - the program lived only
in the tray. A taskbar button is what users expect from a desktop app
(user rule 2026-09-09: "всегда отображалась в панели задач а не только
в трее").

The button is a real top-level window with WS_EX_APPWINDOW: a 1x1 visible
window parked at the corner of the screen. Clicking its taskbar button
activates it; the window procedure turns that into the same "settings"
command the tray's left click sends, so both entry points open the same
overlay menu. The window itself never shows anything.

The icon comes from native/neuralscreen.ico (the same one the launcher
uses), so the taskbar button looks like the program.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_EX_APPWINDOW = 0x00040000
WM_ACTIVATE = 0x0006
WM_NCACTIVATE = 0x0086
WA_CLICKACTIVE = 0x2
WA_ACTIVE = 0x1
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE = 0xF020
SC_RESTORE = 0xF120
SC_CLOSE = 0xF060
WM_QUIT = 0x0012
WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    """WNDCLASSW - not in ctypes.wintypes, defined here."""
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class TaskbarWindow:
    """The taskbar button: a 1x1 APPWINDOW window that reports clicks.

    Commands go into the same queue.Queue the tray uses - the main loop
    already drains it, so a click here opens the overlay menu exactly
    like a left click on the tray icon.
    """

    def __init__(self, commands: queue.Queue, title: str = "NeuralScreen AMD"):
        self._commands = commands
        self._title = title
        self._hwnd = None
        self._thread: threading.Thread | None = None
        self._proc = WNDPROC(self._wnd_proc)
        self._last_cmd = 0.0

    def _wnd_proc(self, hwnd, msg, wparam, lparam) -> int:
        if msg == WM_ACTIVATE:
            # A click on the taskbar button arrives as WA_ACTIVE here (not
            # WA_CLICKACTIVE - measured on Win11 26200). System activations
            # (another window minimized/closed, Alt+Tab, Win+D) arrive the
            # same way, so wparam alone cannot tell them apart. The reliable
            # cue is the cursor: a click on the taskbar button happens IN the
            # taskbar rectangle; system activations leave the cursor
            # elsewhere (user: menu popped up by itself when another program
            # was minimized).
            if wparam == WA_CLICKACTIVE or (
                    wparam == WA_ACTIVE and self._cursor_over_taskbar()):
                self._emit("settings")
            return 0
        if msg == WM_NCACTIVATE:
            # The click on an ALREADY-active taskbar button arrives as
            # WM_NCACTIVATE(WA_ACTIVE), not WM_ACTIVATE (measured on Win11
            # 26200: the button click delivered 0x86 wp=1 with the cursor
            # over the taskbar and nothing else). Without handling it the
            # second click was dead (user: "залипает"). wparam=0 is a
            # deactivation - never a user click, ignore it.
            if wparam == 1 and self._cursor_over_taskbar():
                self._emit("settings")
            return 0
        if msg == WM_SYSCOMMAND and (wparam & 0xFFF0) in (SC_MINIMIZE, SC_RESTORE):
            # The taskbar button sends these when the window is already
            # minimized (restore) or when the user asks to minimize it. The
            # 1x1 window must NEVER actually minimize: the taskbar button
            # disappears with it, and the button becomes a toggle for the
            # overlay menu - clicking it must always reach the menu command.
            # So SC_MINIMIZE/SC_RESTORE are turned into the same menu
            # toggle instead of letting the system minimize the window
            # (user: the button stopped responding on the second click).
            self._emit("settings")
            return 0
        if msg == WM_SYSCOMMAND and (wparam & 0xFFF0) == SC_CLOSE:
            # 'Close window' in the thumbnail's right-click menu destroys
            # the 1x1 window and the taskbar button is gone for the session.
            # The user must quit through the tray (Exit) - ignore SC_CLOSE
            # (audit 10.09 F3).
            return 0
        if msg == WM_QUIT:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _cursor_over_taskbar(self) -> bool:
        """Whether the cursor is inside a taskbar rectangle.

        The only reliable way to tell a taskbar-button click (WA_ACTIVE with
        the cursor over the taskbar) from a system activation (WA_ACTIVE with
        the cursor anywhere else): the two are indistinguishable by wparam.
        The primary taskbar is Shell_TrayWnd; on Windows 11 a secondary
        monitor's taskbar is a separate top-level window, Shell_SecondaryTrayWnd
        - both are checked (audit 10.09 F1: multi-monitor users could not use
        the button on the second screen).
        """
        try:
            pt = wt.POINT()
            if not user32.GetCursorPos(ctypes.byref(pt)):
                return False
            for cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                tb = user32.FindWindowW(cls, None)
                if not tb:
                    continue
                rect = wt.RECT()
                if not user32.GetWindowRect(tb, ctypes.byref(rect)):
                    continue
                if (rect.left <= pt.x < rect.right
                        and rect.top <= pt.y < rect.bottom):
                    return True
            return False
        except Exception:
            return False

    def _emit(self, command: str) -> None:
        """Queue a command, deduped: one click can deliver both
        WA_CLICKACTIVE and WA_ACTIVE, and SC_RESTORE may follow a click."""
        now = time.monotonic()
        if now - self._last_cmd > 0.5:
            self._last_cmd = now
            try:
                self._commands.put(command)
            except Exception:
                pass

    def start(self) -> None:
        """Create the window in its own thread (the message loop blocks)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="taskbar")
        self._thread.start()

    def _run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        cls = "NeuralScreenTaskbar"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = cls
        wc.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
        if not user32.RegisterClassW(ctypes.byref(wc)):
            # Already registered (a second instance in the same process).
            pass
        # A 1x1 window at the corner: visible to the system (so the
        # taskbar button exists) but nothing the eye can catch. The caption
        # style bits matter: without WS_CAPTION/WS_SYSMENU/WS_MINIMIZEBOX
        # the taskbar button has no minimize behaviour at all - clicking an
        # ALREADY-active button sends nothing (no WM_ACTIVATE, no
        # SC_MINIMIZE), which made the second click dead (user: "залипает").
        # With the styles the system sends SC_MINIMIZE on the active button,
        # which the window procedure converts into the menu toggle.
        self._hwnd = user32.CreateWindowExW(
            WS_EX_APPWINDOW, cls, self._title,
            WS_POPUP | WS_VISIBLE | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
            0, 0, 1, 1, None, None, hinst, None)
        if not self._hwnd:
            return
        # CreateWindowExW may drop WS_VISIBLE for a popup with caption styles
        # until the first ShowWindow - force it, or the taskbar button never
        # appears (measured: window came up hidden without it).
        user32.ShowWindow(self._hwnd, 5)  # SW_SHOW
        self._set_icon(hinst)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.DestroyWindow(self._hwnd)
        self._hwnd = None

    def _set_icon(self, hinst) -> None:
        """The launcher's icon, so the taskbar button looks like the app."""
        ico = Path(__file__).resolve().parent / "native" / "neuralscreen.ico"
        if not ico.is_file():
            return
        hicon = user32.LoadImageW(hinst, str(ico), IMAGE_ICON, 32, 32,
                                  LR_LOADFROMFILE)
        if hicon:
            user32.SendMessageW(self._hwnd, WM_SETICON, ICON_SMALL, hicon)
            user32.SendMessageW(self._hwnd, WM_SETICON, ICON_BIG, hicon)

    def stop(self) -> None:
        """Close the window and join the thread."""
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def hwnd(self):
        return self._hwnd
