"""Автоопределение звонка в Windows: какие программы сейчас держат микрофон.

Windows отмечает это в реестре текущего пользователя (тот же признак, по которому в трее
загорается значок микрофона):
    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone
У программы, которая сейчас пишет звук, LastUsedTimeStop = 0. Чтение не требует прав
администратора. Обычные программы лежат в подразделе NonPackaged (путь к exe, «\\» заменены на «#»),
программы из Microsoft Store — отдельными подразделами по имени пакета.
"""

import ntpath  # пути Windows разбираем явно — так работает и в тестах на Linux
import os
import sys

from PySide6.QtCore import QObject, QTimer, Signal

from . import callwatch as common

KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def _subkeys(winreg, key) -> list[str]:
    names, i = [], 0
    while True:
        try:
            names.append(winreg.EnumKey(key, i))
        except OSError:
            return names
        i += 1


def _in_use(winreg, key) -> bool:
    try:
        start = winreg.QueryValueEx(key, "LastUsedTimeStart")[0]
        stop = winreg.QueryValueEx(key, "LastUsedTimeStop")[0]
    except OSError:
        return False
    return bool(start) and stop == 0


def _is_us(path: str) -> bool:
    """Наш собственный процесс тоже держит микрофон, пока идёт запись."""
    p = path.lower()
    own = os.path.normcase(sys.executable).lower()
    return p == own or p == own.replace("python.exe", "pythonw.exe") or "\\callsummarizer\\" in p


def mic_users(winreg=None) -> list[dict]:
    """Программы, которые сейчас пишут с микрофона, — в том же виде, что потоки в Linux."""
    if winreg is None:
        import winreg  # noqa: PLC0415 — модуль есть только в Windows
    found = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY)
    except OSError:
        return found
    with root:
        for name in _subkeys(winreg, root):
            with winreg.OpenKey(root, name) as sub:
                if name == "NonPackaged":
                    for exe in _subkeys(winreg, sub):
                        with winreg.OpenKey(sub, exe) as k:
                            path = exe.replace("#", "\\")
                            if _in_use(winreg, k) and not _is_us(path):
                                found.append(_stream(path, ntpath.splitext(ntpath.basename(path))[0]))
                elif _in_use(winreg, sub):
                    # пакет из Store: «MSTeams_8wekyb3d8bbwe» → «MSTeams»
                    found.append(_stream(name, name.split("_")[0]))
    return found


def _stream(path: str, binary: str) -> dict:
    return {"kind": "capture", "name": binary, "binary": binary, "node": path, "app_id": "",
            "media": "", "role": "", "device": "", "state": "running"}


def list_streams() -> list[dict]:
    return mic_users()


def call_streams(apps: list[str], streams: list[dict] | None = None) -> list[dict]:
    streams = list_streams() if streams is None else streams
    return [{"app": common.display_name(s["name"], s["binary"]), "kind": s["kind"], "device": ""}
            for s in streams if common._is_call(s, apps)]


def call_output_monitor(apps: list[str]) -> str | None:
    # В Windows звук собеседников пишется «петлёй» с устройства вывода по умолчанию;
    # при смене устройства (подключили гарнитуру) запись переключится следом.
    return None


class CallWatcher(QObject):
    """То же, что в Linux, но вместо событий PipeWire — опрос реестра раз в 2 секунды."""

    call_started = Signal(str)
    call_ended = Signal(str)

    def __init__(self, apps: list[str], parent=None):
        super().__init__(parent)
        self.apps = apps
        self.active: str | None = None
        self._last_log = ""
        self._poll = QTimer(self, interval=2000, timeout=self._refresh)
        self._end_timer = QTimer(self, singleShot=True, interval=6000, timeout=self._ended)

    def start(self):
        common.log(f"слежу за звонками (Windows); приложения: {', '.join(self.apps)}")
        self._poll.start()
        self._refresh()

    def stop(self):
        self._poll.stop()
        self._end_timer.stop()
        self.active = None

    def _match(self) -> str | None:
        streams = list_streams()
        calls = call_streams(self.apps, streams)
        found = calls[0]["app"] if calls else None
        summary = f"{common.describe(streams)} ⇒ {'звонок: ' + found if found else 'звонка нет'}"
        if summary != self._last_log:
            self._last_log = summary
            common.log(summary)
        return found

    def _refresh(self):
        found = self._match()
        if found:
            self._end_timer.stop()
            if self.active is None:
                self.active = found
                self.call_started.emit(found)
        elif self.active is not None and not self._end_timer.isActive():
            self._end_timer.start()

    def _ended(self):
        if self._match() is None and self.active is not None:
            name, self.active = self.active, None
            self.call_ended.emit(name)
