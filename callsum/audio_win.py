"""Запись звука в Windows: WASAPI через библиотеку soundcard.

Микрофон пишется как есть, а звук собеседников — «петлёй» (loopback) с устройства вывода:
это то, что вы слышите в колонках или наушниках. Ни «Стерео микшер», ни драйверы, ни права
администратора не нужны. Когда в динамиках тишина, Windows не присылает данных, и soundcard
сама подставляет тишину нужной длины — поэтому дорожки остаются выровненными по времени.
Сжатие в Ogg/Opus делает ffmpeg из пакета imageio-ffmpeg.
"""

import ctypes
import subprocess
import threading
import warnings
from pathlib import Path

import numpy as np

from . import osdeps
from .audio_base import AudioError, level_of

SAMPLE_RATE = 48000
BLOCK = SAMPLE_RATE // 20  # 50 мс — и для индикатора, и для записи
RATES = (48000, 44100, 16000)


def _sc():
    import soundcard

    warnings.filterwarnings("ignore", message="data discontinuity")
    return soundcard


def list_sources() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(микрофоны, устройства вывода) как списки (id, название)."""
    try:
        sc = _sc()
        mics = [(m.id, m.name) for m in sc.all_microphones()]
        outputs = [(s.id, s.name) for s in sc.all_speakers()]
    except Exception as e:  # noqa: BLE001 — любая ошибка WASAPI
        raise AudioError(f"Не удалось получить список звуковых устройств: {e}") from e
    return mics, outputs


def default_mic() -> str:
    try:
        return _sc().default_microphone().id
    except Exception as e:  # noqa: BLE001
        raise AudioError(f"Не найден микрофон по умолчанию: {e}") from e


def default_monitor() -> str:
    try:
        return _sc().default_speaker().id
    except Exception as e:  # noqa: BLE001
        raise AudioError(f"Не найдено устройство вывода по умолчанию: {e}") from e


class TrackRecorder:
    """Пишет один источник (микрофон или «петлю» устройства вывода) в Ogg/Opus."""

    def __init__(self, device: str, out_path: Path, label: str, kind: str = "mic"):
        self.device = device
        self.out_path = out_path
        self.label = label
        self.loopback = kind == "system"
        self.level = 0.0
        self.peak_seen = 0.0
        self._enc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: str | None = None

    def start(self):
        # устройство открывается в своём потоке (там свой COM), ffmpeg запускается уже под его формат
        self.format = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _encoder(self, rate: int) -> subprocess.Popen:
        cmd = [
            osdeps.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "32k", "-application", "voip", str(self.out_path),
        ]
        return osdeps.popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    @staticmethod
    def _open(source):
        """Открывает устройство в формате, который примет Windows.

        soundcard меняет в формате устройства число каналов, но не маску каналов, поэтому
        «2 канала» для микрофона-массива из 4 (частое дело в ноутбуках) или для вывода 5.1
        Windows отвергает как «invalid argument». Сначала просим все каналы устройства (их
        сводим в моно сами), потом пробуем другие сочетания."""
        own = int(getattr(source, "channels", 0) or 0)
        channels = list(dict.fromkeys(c for c in (own, 2, 1, 4, 6, 8) if c > 0))
        last: Exception | None = None
        for rate in RATES:
            for ch in channels:
                try:
                    rec = source.recorder(samplerate=rate, channels=ch, blocksize=rate // 20)
                    return rec.__enter__(), rec, rate, ch
                except (RuntimeError, TypeError, ValueError) as e:
                    last = e
        raise last or RuntimeError("устройство не открылось")

    def _run(self):
        # soundcard работает через COM; в своём потоке его нужно инициализировать отдельно
        ole32 = ctypes.windll.ole32 if osdeps.IS_WIN else None
        if ole32:
            ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        ctx = None
        try:
            sc = _sc()
            source = sc.get_microphone(self.device, include_loopback=self.loopback)
            rec, ctx, rate, ch = self._open(source)
            self.format = f"{ch} кан., {rate} Гц"
            try:
                self._enc = self._encoder(rate)
            except OSError as e:
                self._error = f"Не удалось запустить ffmpeg: {e}"
                return
            block = rate // 20
            while not self._stop.is_set():
                data = rec.record(numframes=block)
                # все каналы устройства (у «петли» и микрофона-массива их несколько) сводим в моно
                mono = data.mean(axis=1) if data.ndim == 2 else data
                self.level = level_of(mono)
                self.peak_seen = max(self.peak_seen, self.level)
                try:
                    self._enc.stdin.write(np.ascontiguousarray(mono, dtype="<f4").tobytes())
                except OSError:
                    # ffmpeg закрылся; Windows сообщает об этом как «[Errno 22] Invalid argument» —
                    # настоящая причина в выводе самого ffmpeg
                    if not self._stop.is_set():
                        self._error = self._encoder_message()
                    return
        except Exception as e:  # noqa: BLE001 — показываем пользователю любую ошибку устройства
            if not self._stop.is_set():
                text = str(e) or e.__class__.__name__
                self._error = ("Windows не дала открыть устройство в подходящем формате (invalid argument). "
                               "Попробуйте выбрать другое устройство в «Настройках» → «Запись»."
                               if text == "invalid argument" else text)
        finally:
            self.level = 0.0
            if ctx is not None:
                try:
                    ctx.__exit__(None, None, None)
                except Exception:  # noqa: BLE001 — устройство уже могло пропасть
                    pass
            if self._enc is not None:
                try:
                    self._enc.stdin.close()  # ffmpeg допишет и закроет файл
                except OSError:
                    pass
            if ole32:
                ole32.CoUninitialize()

    def _encoder_message(self) -> str:
        """Почему закрылся ffmpeg: его собственные слова или код выхода."""
        try:
            self._enc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            return "ffmpeg перестал принимать звук"
        err = ""
        if self._enc.stderr:
            try:
                err = (self._enc.stderr.read() or b"").decode(errors="replace").strip()
            except (OSError, ValueError):
                pass
        if not err:
            return f"ffmpeg неожиданно завершился (код {self._enc.returncode})"
        return f"ffmpeg: {err.splitlines()[-1]}"

    def error(self) -> str | None:
        if self._error:
            return self._error
        if self._enc and self._enc.poll() not in (None, 0) and not self._stop.is_set():
            return self._encoder_message()
        return None

    def request_stop(self):
        self._stop.set()

    def wait_stopped(self):
        if self._thread:
            self._thread.join(timeout=5)
        if self._enc:
            try:
                self._enc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._enc.kill()
                self._enc.wait()

    def stop(self):
        self.request_stop()
        self.wait_stopped()
