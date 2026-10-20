"""Самопроверка: `call-summarizer --self-check [--full] [--report файл]`.

Проверяет, что на месте всё, без чего программа не заработает: ffmpeg и кодек Opus, модули Qt,
распознавание речи и определение говорящих. Нужна прежде всего для готовых сборок, в которые
библиотеки упакованы заранее. С --full программа ещё и по-настоящему распознаёт пробный звук
(скачает модель Whisper tiny, ~75 МБ, и модели определения говорящих, ~40 МБ) и открывает главное окно.
Сборка для Windows работает без консоли, поэтому результат там смотрите в файле из --report.
"""

import math
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path

from . import __version__, config, osdeps


def _tone(path: Path, seconds: float = 3.0) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000)))
                               for i in range(int(16000 * seconds))))


def run(args: list[str]) -> int:
    full = "--full" in args
    report = None
    if "--report" in args and args.index("--report") + 1 < len(args):
        report = Path(args[args.index("--report") + 1])
    lines: list[str] = []
    failed: list[str] = []

    def out(text: str) -> None:
        lines.append(text)
        print(text, flush=True)

    def check(name: str, fn, required: bool = True) -> None:
        try:
            out(f"OK    {name}: {fn()}")
        except Exception as e:  # noqa: BLE001 — проверка должна дойти до конца и всё перечислить
            out(f"{'FAIL' if required else 'WARN'}  {name}: {e!r}")
            if required:
                failed.append(name)

    kind = "готовая сборка" if osdeps.FROZEN else f"Python {sys.version.split()[0]}"
    out(f"{config.APP_NAME} {__version__} ({sys.platform}, {kind})")
    tmp = Path(tempfile.mkdtemp(prefix="callsum-check-"))
    tone = tmp / "tone.wav"
    _tone(tone)

    from PySide6.QtCore import QLibraryInfo, QLocale, QRectF, QTranslator, Qt, qVersion
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    check("Qt", lambda: f"{qVersion()}, платформа {app.platformName()}")

    def svg():
        from PySide6.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(str(Path(__file__).parent / "icon.svg"))
        img = QImage(64, 64, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        renderer.render(p, QRectF(0, 0, 64, 64))
        p.end()
        assert renderer.isValid() and img.pixelColor(32, 32).alpha() > 0, "иконка не нарисовалась"
        return "иконка рисуется"

    check("значки (SVG)", svg)

    def translation():
        t = QTranslator()
        path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        assert t.load(QLocale("ru_RU"), "qtbase", "_", path), f"нет qtbase_ru.qm в {path}"
        return "qtbase_ru"

    check("русские подписи кнопок Qt", translation, required=False)

    def multimedia():
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

        player = QMediaPlayer()
        player.setAudioOutput(QAudioOutput())
        assert player.isAvailable(), "воспроизведение недоступно"
        return "плеер создаётся"

    check("Qt Multimedia (плеер)", multimedia)

    def ffmpeg():
        r = osdeps.run([osdeps.ffmpeg(), "-hide_banner", "-version"], capture_output=True, text=True)
        assert r.returncode == 0, f"{osdeps.ffmpeg()} не запускается"
        return r.stdout.splitlines()[0]

    check("ffmpeg", ffmpeg)

    def opus():
        from . import audio

        audio.extract_audio(tone, tmp / "tone.ogg")
        return f"{audio.probe_duration(tmp / 'tone.ogg'):.2f} с в Ogg/Opus"

    check("кодирование в Opus", opus)

    def recorder_pipe():
        """Запись дорожки целиком, как во время созвона, — только вместо устройства пробный звук.
        Устройство — как микрофон-массив ноутбука: 4 канала, другое число каналов Windows
        отвергает («invalid argument»). Папка — с кириллицей, как бывает у профилей Windows."""
        import time

        import numpy as np

        from . import audio_win

        class Rec:
            t = 0

            def __init__(self, rate, channels):
                self.rate, self.channels = rate, channels

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def record(self, numframes):
                time.sleep(numframes / self.rate)
                n = np.arange(self.t, self.t + numframes)
                self.t += numframes
                wave = (0.2 * np.sin(2 * np.pi * 440 * n / self.rate)).astype(np.float32)
                return np.repeat(wave[:, None], self.channels, axis=1)

        class Source:
            channels = 4

            def recorder(self, samplerate, channels, blocksize):
                if channels != 4:
                    raise RuntimeError("invalid argument")
                return Rec(samplerate, channels)

        class FakeSoundcard:
            @staticmethod
            def get_microphone(_id, include_loopback=False):
                return Source()

        real = audio_win._sc
        audio_win._sc = lambda: FakeSoundcard
        try:
            folder = tmp / "Проверка записи"
            folder.mkdir()
            out = folder / "mic.seg0.ogg"
            tr = audio_win.TrackRecorder("проба", out, "Микрофон", "mic")
            tr.start()
            time.sleep(1.5)
            err = tr.error()
            tr.stop()
            assert not err, err
            from . import audio

            secs = audio.probe_duration(out)
            assert secs > 1.0, f"записалось {secs:.2f} с"
            return f"{secs:.1f} с в Ogg/Opus, устройство открыто как {tr.format}, уровень {tr.peak_seen:.2f}"
        finally:
            audio_win._sc = real

    check("запись дорожки (ffmpeg, пробный звук)", recorder_pipe, required=osdeps.IS_WIN)

    if osdeps.IS_WIN:
        def wasapi():
            import soundcard

            return f"микрофонов: {len(soundcard.all_microphones())}, выходов: {len(soundcard.all_speakers())}"

        check("запись звука (WASAPI)", wasapi)
    else:
        def tool_path(tool: str, package: str) -> str:
            path = shutil.which(tool)
            if not path:
                raise FileNotFoundError(f"не найден — установите пакет {package}")
            return path

        # системные программы: запись (pactl), поиск звонков (pw-dump), уведомления с кнопками (gdbus)
        for tool, package in (("pactl", "pulseaudio-utils"), ("pw-dump", "pipewire-bin"), ("gdbus", "libglib2.0-bin")):
            check(tool, lambda tool=tool, package=package: tool_path(tool, package), required=tool == "pactl")

    def whisper():
        import ctranslate2
        import faster_whisper

        types = sorted(ctranslate2.get_supported_compute_types("cpu"))
        assert "int8" in types, f"нет int8 среди {types}"
        return f"faster-whisper {faster_whisper.__version__}, ctranslate2 {ctranslate2.__version__}"

    check("распознавание речи (faster-whisper)", whisper)

    def vad():
        from faster_whisper.vad import get_vad_model

        get_vad_model()
        return "модель Silero VAD загружается (onnxruntime)"

    check("поиск речи в записи (VAD)", vad)

    def sherpa():
        import sherpa_onnx

        sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.5)
        return f"sherpa-onnx {sherpa_onnx.__version__}"

    check("определение говорящих (sherpa-onnx)", sherpa)

    def claude():
        import anthropic

        return f"anthropic {anthropic.__version__}"

    check("саммари (Claude API)", claude)

    def calendar():
        import icalendar
        import recurring_ical_events

        cal = icalendar.Calendar.from_ical(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Планёрка\r\n"
            "DTSTART;TZID=Europe/Moscow:20260105T100000\r\nDTEND;TZID=Europe/Moscow:20260105T101500\r\n"
            "RRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        found = recurring_ical_events.of(cal).between((2026, 1, 1), (2026, 2, 1))
        assert len(found) == 4, f"повторов найдено {len(found)} вместо 4"
        return f"icalendar {icalendar.__version__}, часовые пояса на месте"

    check("календарь (iCal)", calendar)

    def help_files():
        from .help_view import topics

        found = topics()
        assert len(found) >= 10, f"тем справки: {len(found)}"
        return f"тем справки: {len(found)}"

    check("справка", help_files)

    def certificates():
        import ssl

        if osdeps.IS_WIN:
            n = ssl.create_default_context().cert_store_stats()["x509_ca"]
            assert n, "в хранилище Windows нет корневых сертификатов"
            return f"корневых сертификатов Windows: {n}"
        paths = ssl.get_default_verify_paths()
        for where in (paths.cafile, paths.capath):
            if where and Path(where).exists():
                return where
        raise FileNotFoundError(f"нет ни {paths.cafile}, ни {paths.capath}")

    check("сертификаты для HTTPS", certificates)

    if full:
        def transcribe_tone():
            from . import transcribe

            words = transcribe.transcribe({"audio": tone}, "tiny", "ru", lambda *a: None)
            return f"модель tiny работает, слов в пробном звуке: {len(words)}"

        check("пробное распознавание (Whisper tiny)", transcribe_tone)

        def diarize_tone():
            from . import speakers, transcribe

            models = speakers.ensure_models(lambda *a: None)
            turns = speakers.diarize(transcribe.decode(tone), 0, lambda f: None, lambda: False, models)
            return f"модели на месте, отрезков: {len(turns)}"

        check("пробное определение говорящих", diarize_tone)

        def https():
            import anthropic

            # запрос без настоящего ключа: ответ «неверный ключ» значит, что соединение HTTPS работает
            try:
                anthropic.Anthropic(api_key="self-check", max_retries=0).models.list()
            except anthropic.AuthenticationError:
                return "api.anthropic.com отвечает"
            raise AssertionError("неожиданный ответ")

        check("соединение с Claude API", https)

        def window():
            from PySide6.QtGui import QIcon

            from . import theme, ui

            theme.apply(app, "system")
            w = ui.MainWindow(QIcon())
            w.setup_hotkey(app)
            app.processEvents()
            hotkey = w.hotkey_label or "нет"
            w._quitting = True
            w.close()
            app.processEvents()
            return f"открывается, горячая клавиша: {hotkey}"

        check("главное окно", window)

    shutil.rmtree(tmp, ignore_errors=True)
    out("")
    out("ИТОГ: всё в порядке" if not failed else f"ИТОГ: не прошли: {', '.join(failed)}")
    if report:
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failed else 0
