"""Системные уведомления с кнопками (org.freedesktop.Notifications).

QSystemTrayIcon.showMessage отправляет уведомление без действий — KDE показывает его,
но нажатие ничего не вызывает. Здесь уведомление уходит напрямую, с кнопками
(«Записать», «Открыть»), а нажатия ловятся по сигналу ActionInvoked.
Используется `gdbus` (пакет libglib2.0-bin, есть почти везде).
"""

import re
import shutil
import subprocess

from PySide6.QtCore import QObject, QProcess, Signal

from . import config

_DEST = ["--session", "--dest", "org.freedesktop.Notifications", "--object-path", "/org/freedesktop/Notifications"]
_ACTION = re.compile(r"ActionInvoked \(uint32 (\d+), '((?:[^'\\]|\\.)*)'\)")
_CLOSED = re.compile(r"NotificationClosed \(uint32 (\d+), uint32 \d+\)")
_TOKEN = re.compile(r"ActivationToken \(uint32 (\d+), '([^']*)'\)")


def _gv_str(s: str) -> str:
    """Строка в текстовом формате GVariant."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


class Notifier(QObject):
    # тег уведомления (например, "offer" или "call:<id>") и ключ нажатой кнопки ("default" — само уведомление)
    action = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ids: dict[int, str] = {}
        self._monitor: QProcess | None = None
        self._buffer = ""
        # токен активации Wayland: с ним окно, открытое по нажатию, выйдет на передний план
        self.activation_token = ""

    @staticmethod
    def available() -> bool:
        return shutil.which("gdbus") is not None

    def show(self, tag: str, title: str, body: str, actions: list[tuple[str, str]] = (),
             timeout_ms: int = 8000, urgency: int = 1) -> int | None:
        """Показывает уведомление; возвращает его номер или None, если не получилось."""
        self._ensure_monitor()
        flat = []
        for key, label in actions:
            flat += [key, label]
        hints = (f"{{'desktop-entry': <{_gv_str(config.APP_ID)}>, 'urgency': <byte {urgency}>}}")
        try:
            out = subprocess.run(
                ["gdbus", "call", *_DEST, "--method", "org.freedesktop.Notifications.Notify",
                 _gv_str(config.APP_NAME), "0", _gv_str(config.ICON_NAME), _gv_str(title), _gv_str(body),
                 "[" + ", ".join(_gv_str(x) for x in flat) + "]" if flat else "@as []", hints, str(timeout_ms)],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.search(r"uint32 (\d+)", out)
        if not m:
            return None
        nid = int(m.group(1))
        self._ids[nid] = tag
        return nid

    def close(self, tag: str):
        for nid, t in list(self._ids.items()):
            if t == tag:
                subprocess.run(["gdbus", "call", *_DEST, "--method", "org.freedesktop.Notifications.CloseNotification",
                                str(nid)], capture_output=True, timeout=5)
                self._ids.pop(nid, None)

    def stop(self):
        if self._monitor:
            self._monitor.kill()
            self._monitor.waitForFinished(1000)
            self._monitor = None

    # ---------- сигналы от сервера уведомлений ----------

    def _ensure_monitor(self):
        if self._monitor and self._monitor.state() != QProcess.ProcessState.NotRunning:
            return
        self._monitor = QProcess(self)
        self._monitor.readyReadStandardOutput.connect(self._on_output)
        # --dest: слушаем только настоящий сервер уведомлений, чужие процессы не подделают нажатие
        self._monitor.start("gdbus", ["monitor", *_DEST])

    def _on_output(self):
        self._buffer += bytes(self._monitor.readAllStandardOutput()).decode(errors="replace")
        *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            self.handle_line(line)

    def handle_line(self, line: str):
        if m := _TOKEN.search(line):
            if int(m.group(1)) in self._ids:
                self.activation_token = m.group(2)
        elif m := _ACTION.search(line):
            nid, key = int(m.group(1)), m.group(2)
            tag = self._ids.get(nid)
            if tag is not None:
                self.action.emit(tag, key)
        elif m := _CLOSED.search(line):
            self._ids.pop(int(m.group(1)), None)
