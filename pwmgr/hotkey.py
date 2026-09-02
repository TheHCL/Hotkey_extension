"""全域熱鍵——使用 Win32 RegisterHotKey。

不需要 admin;訊息泵在獨立 daemon thread,Tk 端用 queue + after 接收事件。
"""

from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes
from typing import Callable

# Win32 常數
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_DESTROY = 0x0002

# window class / title for our message-only window
_HOTKEY_CLASS = "PwmgrHotkeyWindow"


# --- 結構與函式原型 ---------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt_x", wintypes.LONG),
        ("pt_y", wintypes.LONG),
    ]


# --- 對外 API ---------------------------------------------------------------


class GlobalHotkey:
    """登錄一個全域熱鍵。callback 會在背景 thread 觸發——若要操作 Tk 需自行排程到主執行緒。"""

    def __init__(
        self,
        modifiers: int,
        key: int,
        callback: Callable[[], None],
        event_queue: queue.Queue | None = None,
    ) -> None:
        self.modifiers = modifiers
        self.key = key
        self.callback = callback
        self.event_queue = event_queue or queue.Queue()
        self._hwnd: int | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._registered = False

    @property
    def hotkey_id(self) -> int:
        return 1

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="pwmgr-hotkey")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)

    def drain(self) -> int:
        """Tk 端用:取出佇列中所有事件並呼叫 callback,回傳處理數量。"""
        n = 0
        while True:
            try:
                self.event_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self.callback()
            except Exception:
                # callback 錯誤不影響下一次觸發
                pass
            n += 1
        return n

    # --- 內部 ---------------------------------------------------------------

    def _run(self) -> None:
        hInstance = kernel32.GetModuleHandleW(None)
        if not hInstance:
            return

        # callback 工廠——WNDPROC 必須由 ctypes 建立以免被 GC
        wndproc = WNDPROC(self._wnd_proc)

        wc = WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(WNDCLASSEX)
        wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p).value
        wc.hInstance = hInstance
        wc.lpszClassName = _HOTKEY_CLASS
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom and ctypes.get_last_error() != 1410:  # 1410 = ERROR_CLASS_ALREADY_EXISTS
            return

        hwnd = user32.CreateWindowExW(
            0,
            _HOTKEY_CLASS,
            "pwmgr hotkey",
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            hInstance,
            None,
        )
        if not hwnd:
            return
        self._hwnd = hwnd

        if not user32.RegisterHotKey(hwnd, self.hotkey_id, self.modifiers, self.key):
            self._hwnd = None
            user32.DestroyWindow(hwnd)
            return
        self._registered = True

        try:
            msg = MSG()
            while self._running:
                # GetMessageW 會阻塞直到收到訊息;WM_QUIT 會讓它回 0
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._registered:
                user32.UnregisterHotKey(hwnd, self.hotkey_id)
                self._registered = False
            user32.DestroyWindow(hwnd)
            self._hwnd = None

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_HOTKEY and wparam == self.hotkey_id:
            self.event_queue.put(True)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
