"""Проверка установленной программы на настоящей Windows (запускается в GitHub Actions).

У машины CI нет звуковых устройств и пользователя за экраном, поэтому запись с устройств
и нажатие горячей клавиши здесь не проверить — только что всё ставится, загружается и работает.
"""

import math
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

failures: list[str] = []


def check(name, fn, optional=False):
    try:
        print(f"OK    {name}: {fn()}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"{'SKIP' if optional else 'FAIL'}  {name}: {e!r}", flush=True)
        if not optional:
            failures.append(name)


def make_tone(path: Path, seconds: float = 3.0):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000)))
                          for i in range(int(16000 * seconds)))
        w.writeframes(frames)


tmp = Path(tempfile.mkdtemp())
tone = tmp / "tone.wav"
make_tone(tone)

from callsum import audio, callwatch, config, osdeps  # noqa: E402

check("платформа", lambda: f"IS_WIN={osdeps.IS_WIN}, папки {config.CONFIG_DIR} / {config.DATA_DIR}")
check("движок записи — WASAPI", lambda: audio.TrackRecorder.__module__)
check("определение звонков — реестр", lambda: callwatch.CallWatcher.__module__)
check("ffmpeg из imageio-ffmpeg", lambda: osdeps.ffmpeg())


def opus_roundtrip():
    audio.extract_audio(tone, tmp / "tone.ogg")
    return f"{audio.probe_duration(tmp / 'tone.ogg'):.2f} с в Ogg/Opus"


check("кодирование в Opus и длительность", opus_roundtrip)

from callsum import callwatch_win  # noqa: E402

check("кто держит микрофон (реестр)", lambda: callwatch_win.mic_users())
check("звуковые устройства", lambda: audio.list_sources(), optional=True)


def autostart():
    import winreg

    config.set_autostart(True)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
        value = winreg.QueryValueEx(k, "CallSummarizer")[0]
    config.set_autostart(False)
    return value


check("автозапуск через HKCU\\Run", autostart)


def window():
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from callsum import theme, ui

    app = QApplication.instance() or QApplication(sys.argv[:1])
    theme.apply(app, "system")
    w = ui.MainWindow(QIcon())
    w.setup_hotkey(app)
    w.show()
    app.processEvents()
    label = w.hotkey_label or f"нет (ошибка Windows {getattr(w.hotkey, 'error', '?')})"
    tray = w.tray_available()
    w._quitting = True
    if w.hotkey:
        w.hotkey.unregister()
    w.watcher.stop()
    w.hide()
    return f"окно открылось, горячая клавиша: {label}, трей доступен: {tray}"


check("главное окно и горячая клавиша", window)


def whisper():
    from callsum import transcribe

    t = time.time()
    words = transcribe.transcribe({"audio": tone}, "tiny", "ru", lambda *a: None)
    return f"модель tiny загружена, слов в тоне: {len(words)} ({time.time() - t:.0f} с)"


check("распознавание (faster-whisper)", whisper)


def diarization():
    from callsum import speakers, transcribe

    models = speakers.ensure_models(lambda *a: None)
    turns = speakers.diarize(transcribe.decode(tone), 0, lambda f: None, lambda: False, models)
    return f"модели на месте, отрезков: {len(turns)}"


check("определение говорящих (sherpa-onnx)", diarization)


def icon():
    from PySide6.QtGui import QGuiApplication  # noqa: F401 — приложение уже создано выше

    from callsum import make_icon

    make_icon.render(tmp / "icon.ico")
    return f"{(tmp / 'icon.ico').stat().st_size} байт"


check("иконка .ico", icon)

print()
print("ИТОГ:", "всё в порядке" if not failures else f"не прошли: {', '.join(failures)}")
sys.exit(1 if failures else 0)
