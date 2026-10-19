"""Автоопределение звонка и устройства, на котором он звучит.

Слушаем события PipeWire/PulseAudio (`pactl subscribe`) и при каждом изменении потоков
смотрим (через `pw-dump`), кто сейчас пишет и воспроизводит звук. Звонок — это:
- приложение из списка начало брать звук с микрофона, или
- играет голосовой поток WebRTC (так звучат звонки и голосовые каналы Discord),
  или поток с ролью «телефон» — даже если ваш микрофон в звонке выключен.

Что программа увидела и решила, пишется в ~/.local/share/call-summarizer/callwatch.log.
"""

import json
import subprocess
from datetime import datetime

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .audio import RECORDER_NAME
from .config import DATA_DIR

DEFAULT_APPS = ("zoom, telegram, discord, slack, teams, skype, chrome, chromium, firefox, yandex, "
                "opera, vivaldi, brave, edge, signal, whatsapp, element, mattermost, jitsi, webex, vesktop")
LOG_FILE = DATA_DIR / "callwatch.log"

# у этих «имён» название приложения ничего не говорит — показываем имя процесса
_GENERIC_NAMES = {"webrtc voiceengine", "chromium input", "chromium", "openal soft", "alsa plug-in", ""}
_VOICE_ROLES = {"phone", "communication"}


def parse_apps(text: str) -> list[str]:
    return [a.strip().lower() for a in text.replace("\n", ",").split(",") if a.strip()]


def display_name(name: str, binary: str) -> str:
    if name.lower().split(" [")[0] in _GENERIC_NAMES and binary:
        return binary[:1].upper() + binary[1:]
    return name or binary


def _pactl_json(*args) -> list:
    try:
        out = subprocess.run(["pactl", "-f", "json", "list", *args],
                             capture_output=True, text=True, timeout=3).stdout
        return json.loads(out or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def _from_pw_dump() -> list[dict] | None:
    """Звуковые потоки из PipeWire. pw-dump, в отличие от `pactl -f json`, не портит кириллицу
    и показывает свойства приложения-владельца, если у самого потока их нет."""
    try:
        data = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    clients, nodes, links = {}, {}, []
    for o in data:
        t = o.get("type", "")
        if t.endswith(":Client"):
            clients[o["id"]] = (o.get("info") or {}).get("props") or {}
        elif t.endswith(":Node"):
            nodes[o["id"]] = o
        elif t.endswith(":Link"):
            info = o.get("info") or {}
            links.append((info.get("output-node-id"), info.get("input-node-id")))

    def props(nid):
        return ((nodes.get(nid) or {}).get("info") or {}).get("props") or {}

    streams = []
    for nid, o in nodes.items():
        p = props(nid)
        mc = p.get("media.class", "")
        if mc not in ("Stream/Input/Audio", "Stream/Output/Audio"):
            continue
        c = clients.get(p.get("client.id"), {})

        def get(key, p=p, c=c):
            return str(p.get(key) or c.get(key) or "")

        kind = "capture" if mc == "Stream/Input/Audio" else "playback"
        # куда подключён поток: для воспроизведения — устройство вывода, для записи — источник
        peers = [i for out, i in links if out == nid] if kind == "playback" else [out for out, i in links if i == nid]
        device = next((props(x).get("node.name") for x in peers
                       if props(x).get("media.class", "").startswith("Audio/")), "")
        streams.append({"kind": kind, "name": get("application.name"), "binary": get("application.process.binary"),
                        "node": get("node.name"), "app_id": get("application.id"), "media": get("media.name"),
                        "role": get("media.role"), "device": device or "", "state": (o.get("info") or {}).get("state", "")})
    return streams


def _from_pactl() -> list[dict]:
    """Запасной путь, если pw-dump недоступен."""
    sinks = {s.get("index"): s.get("name") for s in _pactl_json("sinks")}
    sources = {s.get("index"): s.get("name") for s in _pactl_json("sources")}
    streams = []
    for kind, items in (("capture", _pactl_json("source-outputs")), ("playback", _pactl_json("sink-inputs"))):
        for o in items:
            p = o.get("properties", {})
            dev = sinks.get(o.get("sink")) if kind == "playback" else sources.get(o.get("source"))
            streams.append({"kind": kind, "name": p.get("application.name") or "",
                            "binary": p.get("application.process.binary") or "", "node": p.get("node.name") or "",
                            "app_id": p.get("application.id") or "", "media": p.get("media.name") or "",
                            "role": p.get("media.role") or "", "device": dev or "", "state": ""})
    return streams


def list_streams() -> list[dict]:
    streams = _from_pw_dump()
    if streams is None:
        streams = _from_pactl()
    # наши собственные потоки (запись и плеер) — не звонок
    return [s for s in streams if s["name"] != RECORDER_NAME]


def _is_call(s: dict, apps: list[str]) -> bool:
    hay = f"{s['name']} {s['binary']} {s['node']} {s['app_id']}".lower()
    listed = any(a in hay for a in apps)
    voice = s["name"].lower() == "webrtc voiceengine" or s["role"].lower() in _VOICE_ROLES
    # микрофон берут многие программы, поэтому захват считаем звонком только у приложений из списка;
    # голосовой поток воспроизведения — звонок сам по себе
    return (s["kind"] == "capture" and listed) or (s["kind"] == "playback" and voice)


def call_streams(apps: list[str], streams: list[dict] | None = None) -> list[dict]:
    """Потоки, похожие на звонок: [{app, kind: capture|playback, device}]."""
    streams = list_streams() if streams is None else streams
    return [{"app": display_name(s["name"], s["binary"]), "kind": s["kind"], "device": s["device"]}
            for s in streams if _is_call(s, apps)]


def call_output_monitor(apps: list[str]) -> str | None:
    """Монитор устройства, на котором сейчас звучит звонок (если звонок что-то воспроизводит)."""
    streams = list_streams()
    for s in call_streams(apps, streams):
        if s["kind"] == "playback" and s["device"]:
            return f"{s['device']}.monitor"
    # поток звонка не голосовой (например, Telegram через OpenAL) — ищем любой поток этого приложения
    for s in streams:
        hay = f"{s['name']} {s['binary']} {s['node']} {s['app_id']}".lower()
        if s["kind"] == "playback" and s["device"] and any(a in hay for a in apps):
            return f"{s['device']}.monitor"
    return None


def log(line: str) -> None:
    """Журнал определения звонков — чтобы разобраться, если звонок не заметили."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 300_000:
            LOG_FILE.write_text(LOG_FILE.read_text(encoding="utf-8")[-150_000:], encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass


def describe(streams: list[dict]) -> str:
    if not streams:
        return "потоков нет"
    return "; ".join(
        f"[{'запись' if s['kind'] == 'capture' else 'звук'}] {s['name'] or '?'} ({s['binary'] or '?'})"
        f"{' role=' + s['role'] if s['role'] else ''}{' «' + s['media'][:40] + '»' if s['media'] else ''}"
        f"{' → ' + s['device'] if s['device'] else ''}"
        for s in streams
    )


class CallWatcher(QObject):
    call_started = Signal(str)  # название приложения
    call_ended = Signal(str)

    def __init__(self, apps: list[str], parent=None):
        super().__init__(parent)
        self.apps = apps
        self.active: str | None = None
        self._last_log = ""
        self._proc: QProcess | None = None
        self._enabled = False
        self._refresh_timer = QTimer(self, singleShot=True, interval=400, timeout=self._refresh)
        # звонок считается законченным, только если признаков нет несколько секунд:
        # приложения переоткрывают потоки при смене устройства и между репликами
        self._end_timer = QTimer(self, singleShot=True, interval=6000, timeout=self._ended)

    def start(self):
        self._enabled = True
        log(f"слежу за звонками; приложения: {', '.join(self.apps)}")
        self._spawn()
        self._refresh()

    def stop(self):
        self._enabled = False
        self._end_timer.stop()
        if self._proc:
            self._proc.finished.disconnect()
            self._proc.kill()
            self._proc.waitForFinished(1000)
            self._proc = None
        self.active = None

    def _spawn(self):
        self._proc = QProcess(self)
        self._proc.readyReadStandardOutput.connect(self._on_output)
        self._proc.finished.connect(lambda *_: self._enabled and QTimer.singleShot(5000, self._respawn))
        self._proc.start("pactl", ["subscribe"])

    def _respawn(self):
        if self._enabled:
            self._spawn()

    def _on_output(self):
        data = bytes(self._proc.readAllStandardOutput())
        if b"source-output" in data or b"sink-input" in data:
            self._refresh_timer.start()

    def _match(self) -> str | None:
        streams = list_streams()
        calls = call_streams(self.apps, streams)
        found = calls[0]["app"] if calls else None
        summary = f"{describe(streams)} ⇒ {'звонок: ' + found if found else 'звонка нет'}"
        if summary != self._last_log:
            self._last_log = summary
            log(summary)
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
