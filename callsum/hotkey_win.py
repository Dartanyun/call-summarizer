"""Общесистемная горячая клавиша в Windows (RegisterHotKey) — без прав администратора.

Пробуем Win+Shift+Z (как Meta+Shift+Z в Linux); если сочетание занято, — Ctrl+Alt+Z.
Нажатие приходит окну сообщением WM_HOTKEY, его ловит фильтр системных событий Qt.
"""

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
HOTKEY_ID = 0xC5A1
CANDIDATES = [
    (MOD_WIN | MOD_SHIFT, ord("Z"), "Win+Shift+Z"),
    (MOD_CONTROL | MOD_ALT, ord("Z"), "Ctrl+Alt+Z"),
]


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self._callback()
                return True, 0
        return False, 0


class GlobalHotkey:
    def __init__(self, app, hwnd: int, callback):
        self.label: str | None = None
        self._hwnd = hwnd
        self._filter = _Filter(callback)
        app.installNativeEventFilter(self._filter)
        user32 = ctypes.windll.user32
        for mods, vk, label in CANDIDATES:
            if user32.RegisterHotKey(wintypes.HWND(hwnd), HOTKEY_ID, mods | MOD_NOREPEAT, vk):
                self.label = label
                break

    def unregister(self):
        if self.label:
            ctypes.windll.user32.UnregisterHotKey(wintypes.HWND(self._hwnd), HOTKEY_ID)
            self.label = None
