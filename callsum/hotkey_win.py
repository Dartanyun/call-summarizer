"""Общесистемная горячая клавиша в Windows (RegisterHotKey) — без прав администратора.

Запись: Win+Shift+Z (как Meta+Shift+Z в Linux), если сочетание занято, — Ctrl+Alt+Z.
Отметка «важный момент»: Win+Shift+B или Ctrl+Alt+B.
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
BOOKMARK_ID = 0xC5A2
BOOKMARK_CANDIDATES = [
    (MOD_WIN | MOD_SHIFT, ord("B"), "Win+Shift+B"),
    (MOD_CONTROL | MOD_ALT, ord("B"), "Ctrl+Alt+B"),
]


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, callback, hotkey_id: int):
        super().__init__()
        self._callback = callback
        self._id = hotkey_id

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self._id:
                self._callback()
                return True, 0
        return False, 0


ERROR_HOTKEY_ALREADY_REGISTERED = 1409


class GlobalHotkey:
    def __init__(self, app, hwnd: int, callback, candidates=CANDIDATES, hotkey_id: int = HOTKEY_ID):
        self.label: str | None = None
        self.error: int = 0  # код ошибки Windows, если не удалось
        self._hwnd: int | None = None
        self._id = hotkey_id
        self._filter = _Filter(callback, hotkey_id)
        app.installNativeEventFilter(self._filter)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32 = user32
        # сначала привязываем к окну; если окна нет (или дескриптор не настоящий) — к потоку
        for target in (hwnd, None):
            for mods, vk, label in candidates:
                if user32.RegisterHotKey(wintypes.HWND(target) if target else None,
                                         hotkey_id, mods | MOD_NOREPEAT, vk):
                    self.label, self._hwnd, self.error = label, target, 0
                    return
                self.error = ctypes.get_last_error()
            if self.error == ERROR_HOTKEY_ALREADY_REGISTERED:
                return  # сочетания заняты другими программами — к потоку тоже не выйдет

    def unregister(self):
        if self.label:
            self._user32.UnregisterHotKey(wintypes.HWND(self._hwnd) if self._hwnd else None, self._id)
            self.label = None
