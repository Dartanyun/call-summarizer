"""Запись звука в Windows: WASAPI через библиотеку soundcard.

Микрофон пишется как есть, а звук собеседников — «петлёй» (loopback) с устройства вывода:
это то, что вы слышите в колонках или наушниках. Ни «Стерео микшер», ни драйверы, ни права
администратора не нужны. Когда в динамиках тишина, Windows не присылает данных, и soundcard
сама подставляет тишину нужной длины — поэтому дорожки остаются выровненными по времени.
Сжатие в Ogg/Opus делает ffmpeg из пакета imageio-ffmpeg.
"""

import ctypes
import struct
import subprocess
import sys
import threading
import time
import uuid
import warnings
from pathlib import Path

import numpy as np

from . import osdeps
from .audio_base import AudioError, level_of

SAMPLE_RATE = 48000
BLOCK = SAMPLE_RATE // 20  # 50 мс — и для индикатора, и для записи
RATES = (48000, 44100, 16000)
STALL = 10.0  # столько секунд без звука от Windows считаем зависанием


_import_lock = threading.Lock()


# ошибки soundcard, после которых есть смысл попробовать другой формат
_FORMAT_ERRORS = ("invalid argument", "unsupported format")
_SERVICE_DOWN = ("Похоже, служба «Windows Audio» не работает или перезапускается. Перезапустите её "
                 "(«Службы» → «Windows Audio» → «Перезапустить») или перезагрузите компьютер.")
# антивирусы с «защитой микрофона» встраиваются в звук Windows и отвечают программам отказом
_ANTIVIRUS = (" Если стоит антивирус с защитой микрофона (например, Kaspersky), разрешите в нём доступ "
              "к микрофону для «Саммари созвонов» или Python.")
# коды WASAPI/COM → что сказать человеку (soundcard пишет их как «Error 0x…»)
_HRESULTS = {
    "0x88890004": "Устройство отключилось или сменилось. Подключите его снова или выберите другое "
                  "в «Настройках» → «Запись».",
    "0x80070490": "Устройство не найдено — возможно, оно отключено. Выберите другое в «Настройках» → «Запись».",
    "0x8889000a": "Устройство занято другой программой в монопольном режиме. Закройте её или снимите "
                  "в свойствах устройства галочку «Разрешить приложениям монопольный режим».",
    "0x80070005": "Windows не даёт программам доступ к микрофону. Включите его: «Параметры» → "
                  "«Конфиденциальность и защита» → «Микрофон» → «Разрешить классическим приложениям»." + _ANTIVIRUS,
    "0x88890010": _SERVICE_DOWN,
    "0x800706cc": _SERVICE_DOWN,
    "0x800706ba": _SERVICE_DOWN,
    "0x80040154": _SERVICE_DOWN,
}


def _explain(e: Exception) -> str:
    text = str(e) or e.__class__.__name__
    if text in _FORMAT_ERRORS:
        return ("Windows не дала открыть устройство в подходящем формате (invalid argument). "
                "Попробуйте выбрать другое устройство в «Настройках» → «Запись»." + _ANTIVIRUS)
    code = text.removeprefix("Error ").lower()
    return f"{_HRESULTS[code]} (код {code})" if code in _HRESULTS else text


def _log(line: str) -> None:
    """В консоль, а у программы без консоли — в app.log (см. __main__._log_to_file)."""
    try:
        print(f"[запись] {line}", file=sys.stderr, flush=True)
    except (OSError, ValueError, AttributeError):
        pass


def _sc():
    """Библиотека soundcard. Загружать её надо там, где COM ещё не инициализирован в режиме MTA:
    при загрузке она сама вызывает CoInitializeEx, и ответ «уже инициализирован» (S_FALSE,
    «Error 0x100000001») принимает за ошибку — и загрузка срывается. Поэтому грузим её один раз,
    из главного потока программы (см. TrackRecorder.start), под блокировкой."""
    with _import_lock:
        import soundcard

        if sys.platform == "win32":
            _fix_format(soundcard.mediafoundation)
    warnings.filterwarnings("ignore", message="data discontinuity")
    return soundcard


# KSDATAFORMAT_SUBTYPE_IEEE_FLOAT и стандартные раскладки колонок (KSAUDIO_SPEAKER_*)
_IEEE_FLOAT = uuid.UUID("00000003-0000-0010-8000-00aa00389b71").bytes_le
_MASKS = {1: 0x4, 2: 0x3, 3: 0x7, 4: 0x33, 5: 0x37, 6: 0x3F, 7: 0x13F, 8: 0x63F}


def _fix_format(mf) -> None:
    """Исправляем, как soundcard описывает формат звука для Windows.

    soundcard объявляет WAVEFORMATEXTENSIBLE для cffi без упаковки, а в Windows структура упакована
    (#pragma pack(1)): у soundcard поля после первых 18 байт сдвинуты. Записывая «32 бита точности»,
    она на самом деле затирает маску каналов, и маска перестаёт совпадать с числом каналов. Старые
    сборки Windows это прощали, новые отвергают — «invalid argument» при любом формате. Здесь формат
    собирается побайтно, по настоящей раскладке, с маской каналов устройства."""
    if getattr(mf._AudioClient, "_callsum_fixed", False):
        return

    def init(self, ptr, samplerate, channels, blocksize, isloopback, exclusive_mode=False):
        self._ptr = ptr
        self.channelmap = list(range(channels)) if isinstance(channels, int) else list(channels)
        if blocksize is None:
            blocksize = self.deviceperiod[0] * samplerate
        pp = mf._ffi.new("WAVEFORMATEXTENSIBLE**")
        mf._com.check_error(ptr[0][0].lpVtbl.GetMixFormat(ptr[0], pp))
        try:
            raw = bytes(mf._ffi.buffer(pp[0], 40))
        finally:
            mf._ole32.CoTaskMemFree(pp[0])
        tag, mix_channels, _rate, _avg, _align, _bits, extra = struct.unpack_from("<HHIIHHH", raw, 0)
        mix_mask = struct.unpack_from("<I", raw, 20)[0] if tag == 0xFFFE and extra >= 22 else 0
        ch = len(set(self.channelmap))
        mask = mix_mask if ch == mix_channels and bin(mix_mask).count("1") == ch else _MASKS.get(ch, 0)
        rate = int(samplerate)
        fmt = bytearray(40)
        struct.pack_into("<HHIIHHHHI", fmt, 0, 0xFFFE, ch, rate, rate * ch * 4, ch * 4, 32, 22, 32, mask)
        fmt[24:40] = _IEEE_FLOAT
        self._format = mf._ffi.new("char[]", bytes(fmt))  # память нужна, пока жив клиент
        # перевод формата и частоты — силами Windows (autoconvert + хороший ресемплер), без «persist»
        flags = 0x80000000 | 0x08000000 | 0x00080000 | (0x00020000 if isloopback else 0)
        mode = mf._ole32.AUDCLNT_SHAREMODE_EXCLUSIVE if exclusive_mode else mf._ole32.AUDCLNT_SHAREMODE_SHARED
        hr = ptr[0][0].lpVtbl.Initialize(ptr[0], mode, flags, int(blocksize / samplerate * 10_000_000), 0,
                                         mf._ffi.cast("WAVEFORMATEXTENSIBLE*", self._format), mf._ffi.NULL)
        mf._com.check_error(hr)
        self.samplerate = samplerate
        self._idle_start_time = None

    mf._AudioClient.__init__ = init
    mf._AudioClient._callsum_fixed = True


def list_sources() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(микрофоны, устройства вывода) как списки (id, название)."""
    try:
        sc = _sc()
        mics = [(m.id, m.name) for m in sc.all_microphones()]
        outputs = [(s.id, s.name) for s in sc.all_speakers()]
    except Exception as e:  # noqa: BLE001 — любая ошибка WASAPI
        raise AudioError(f"Не удалось получить список звуковых устройств: {_explain(e)}") from e
    return mics, outputs


def default_mic() -> str:
    try:
        return _sc().default_microphone().id
    except Exception as e:  # noqa: BLE001
        raise AudioError(f"Не найден микрофон по умолчанию: {_explain(e)}") from e


def default_monitor() -> str:
    try:
        return _sc().default_speaker().id
    except Exception as e:  # noqa: BLE001
        raise AudioError(f"Не найдено устройство вывода по умолчанию: {_explain(e)}") from e


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
        self.format = ""
        self._alive = self._checked = time.monotonic()

    def start(self):
        try:
            _sc()  # загрузка — здесь, в главном потоке (см. _sc), а не в потоке записи
        except Exception as e:  # noqa: BLE001 — любая ошибка WASAPI
            raise AudioError(f"Не удалось подготовить запись звука: {_explain(e)}") from e
        # устройство открывается в своём потоке (там свой COM), ffmpeg запускается уже под его формат
        self.format = ""
        self._alive = self._checked = time.monotonic()
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
    def _mix_format(source) -> tuple[int, int] | None:
        """(частота, каналы), в которых Windows сама смешивает звук устройства. Поток ровно в этом
        формате общий режим WASAPI принимает всегда, без всяких преобразований."""
        try:
            from soundcard import mediafoundation as mf

            client = source._audio_client()
            try:
                pp = mf._ffi.new("WAVEFORMATEXTENSIBLE**")
                mf._com.check_error(client[0][0].lpVtbl.GetMixFormat(client[0], pp))
                fmt = pp[0][0].Format
                found = int(fmt.nSamplesPerSec), int(fmt.nChannels)
                mf._ole32.CoTaskMemFree(pp[0])
            finally:
                mf._com.release(client)
            return found
        except Exception:  # noqa: BLE001 — не узнали: обойдёмся перебором
            return None

    def _open(self, source):
        """Открывает устройство в формате, который примет Windows.

        Сначала — ровно в формате, в котором Windows смешивает звук этого устройства: его она
        принимает всегда. Если узнать его не вышло — перебираем частоты и число каналов:
        soundcard меняет в формате число каналов, но не маску каналов, и «2 канала» для
        микрофона-массива из 4 или для вывода 5.1 Windows отвергает как «invalid argument».
        Все каналы сводим в моно сами."""
        mix = self._mix_format(source)
        own = int(getattr(source, "channels", 0) or 0)
        tries = [mix] if mix else []
        tries += [(rate, ch) for rate in RATES for ch in (own, 2, 1, 4, 6, 8) if ch > 0]
        failed: list[str] = []
        last: Exception | None = None
        for rate, ch in dict.fromkeys(tries):
            try:
                rec = source.recorder(samplerate=rate, channels=ch, blocksize=rate // 20)
            except (RuntimeError, TypeError, ValueError, AssertionError) as e:
                last = e
                failed.append(f"{ch} кан. {rate} Гц: {e or e.__class__.__name__}")
                if isinstance(e, RuntimeError) and str(e) not in _FORMAT_ERRORS:
                    break  # дело не в формате (устройство пропало, служба звука не работает…)
                continue
            try:
                return rec.__enter__(), rec, rate, ch
            except (RuntimeError, TypeError, ValueError, AssertionError) as e:
                last = e
                failed.append(f"{ch} кан. {rate} Гц, запуск: {e or e.__class__.__name__}")
                try:
                    rec.__exit__(None, None, None)
                except Exception:  # noqa: BLE001 — устройство и так не работает
                    pass
        _log(f"{self.label}: устройство не открылось (формат устройства: "
             f"{f'{mix[1]} кан. {mix[0]} Гц' if mix else 'неизвестен'}); " + "; ".join(failed))
        raise last or RuntimeError("устройство не открылось")

    def _run(self):
        ctx = None
        ole32 = None
        try:
            # сначала библиотека (если её ещё нет, она сама настроит COM в этом потоке),
            # потом COM для этого потока — ответ «уже инициализирован» здесь не ошибка
            sc = _sc()
            if osdeps.IS_WIN:
                ole32 = ctypes.windll.ole32
                ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
            source = sc.get_microphone(self.device, include_loopback=self.loopback)
            rec, ctx, rate, ch = self._open(source)
            self.format = f"{ch} кан., {rate} Гц"
            self._alive = time.monotonic()
            _log(f"{self.label}: {getattr(source, 'name', self.device)} — {self.format}")
            try:
                self._enc = self._encoder(rate)
            except OSError as e:
                self._error = f"Не удалось запустить ffmpeg: {e}"
                return
            block = rate // 20
            while not self._stop.is_set():
                data = rec.record(numframes=block)
                self._alive = time.monotonic()
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
                self._error = _explain(e)
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
        now = time.monotonic()
        if now - self._checked > 5:
            self._alive = now  # компьютер спал или окно подвисло — это не зависание звука
        self._checked = now
        if not self._stop.is_set() and self._thread and self._thread.is_alive() and now - self._alive > STALL:
            # вызов внутри Windows не вернулся: так бывает, когда зависла служба «Windows Audio»
            what = "перестала присылать звук с устройства" if self.format else "не отвечает при открытии устройства"
            self._error = (f"Windows {what} — похоже, зависла служба «Windows Audio». Перезапустите её "
                           "(«Службы» → «Windows Audio» → «Перезапустить») или перезагрузите компьютер." + _ANTIVIRUS)
            _log(f"{self.label}: {self._error}")
            return self._error
        return None

    def request_stop(self):
        self._stop.set()

    def wait_stopped(self):
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive() and self._enc:
                # поток завис внутри Windows — закрываем ffmpeg сами, чтобы записанное сохранилось
                try:
                    self._enc.stdin.close()
                except (OSError, ValueError):
                    pass
        if self._enc:
            try:
                self._enc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._enc.kill()
                self._enc.wait()

    def stop(self):
        self.request_stop()
        self.wait_stopped()
