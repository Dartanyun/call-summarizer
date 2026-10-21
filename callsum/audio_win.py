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


class _Blocked(RuntimeError):
    """Windows отвергла даже собственный формат устройства — значит, дело не в формате."""


_KASPERSKY = ("Разрешите её в Kaspersky: «Безопасность» → «Предотвращение вторжений» → «Управлять "
              "приложениями» → {app} → вкладка «Права» → «Изменение операционной системы» → «Подозрительные "
              "изменения в операционной системе» → «Доступ к устройствам записи звука» → «Разрешить» → "
              "«Сохранить». И проверьте «Настройки приватности» → «Контроль камеры и микрофона»: там не должно "
              "быть «Запретить всем приложениям подключаться к микрофону».")
_antivirus_names: list[str] | None = None


def _antivirus() -> list[str]:
    """Антивирусы, о которых знает «Безопасность Windows», кроме самой Microsoft Defender."""
    global _antivirus_names
    if _antivirus_names is None:
        names: list[str] = []
        try:
            out = osdeps.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-CimInstance -Namespace "
                 "root/SecurityCenter2 -ClassName AntiVirusProduct | ForEach-Object displayName"],
                capture_output=True, text=True, timeout=15).stdout
            names = [n.strip() for n in out.splitlines() if n.strip() and "defender" not in n.lower()]
        except (OSError, subprocess.SubprocessError):
            pass
        _antivirus_names = list(dict.fromkeys(names))
    return _antivirus_names


def _blocked_message() -> str:
    app = "«Саммари созвонов»" if osdeps.FROZEN else "«Python»"
    names = _antivirus()
    text = ("Windows отказала программе в записи звука — даже в собственном формате устройства "
            "(invalid argument), так что дело не в устройстве. ")
    if any("kaspersky" in n.lower() for n in names):
        return text + "Похоже, запись блокирует Kaspersky. " + _KASPERSKY.format(app=app) + \
            " Потом начните запись заново."
    if names:
        return text + (f"Похоже, запись блокирует антивирус ({', '.join(names)}). Разрешите в нём {app} "
                       "доступ к микрофону и записи звука или добавьте программу в доверенные — проверить "
                       "можно, приостановив защиту на пару минут.")
    return text + (f"Так бывает, когда запись блокирует антивирус с защитой микрофона: разрешите в нём {app} "
                   "запись звука или добавьте программу в доверенные. В Kaspersky: " +
                   _KASPERSKY.format(app=app).removeprefix("Разрешите её в Kaspersky: "))


def _explain(e: Exception) -> str:
    text = str(e) or e.__class__.__name__
    if isinstance(e, _Blocked):
        return _blocked_message()
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
# флаги потока: перевод формата и частоты силами Windows (autoconvert + хороший ресемплер), без «persist»
_CONVERT = 0x80000000 | 0x08000000 | 0x00080000
_LOOPBACK = 0x00020000
# как описать Windows формат потока, по порядку попыток: формат смешивания устройства байт в байт и без
# флагов (как в примере записи от Microsoft); свой WAVEFORMATEXTENSIBLE; простой WAVEFORMATEX
STYLES = ("exact", "ext", "plain")


def _mix_bytes(mf, ptr) -> bytes:
    """Формат, в котором Windows смешивает звук устройства, — байтами, как его отдала Windows."""
    pp = mf._ffi.new("WAVEFORMATEXTENSIBLE**")
    mf._com.check_error(ptr[0][0].lpVtbl.GetMixFormat(ptr[0], pp))
    try:
        extra = struct.unpack_from("<H", bytes(mf._ffi.buffer(pp[0], 18)), 16)[0]
        return bytes(mf._ffi.buffer(pp[0], 18 + min(extra, 22)))
    finally:
        mf._ole32.CoTaskMemFree(pp[0])


def _mix_info(raw: bytes) -> tuple[int, int, int, bool]:
    """(частота, каналы, маска каналов, float32 ли) формата смешивания."""
    tag, ch, rate, _avg, _align, bits = struct.unpack_from("<HHIIHH", raw)
    ext = tag == 0xFFFE and len(raw) >= 40
    mask = struct.unpack_from("<I", raw, 20)[0] if ext else 0
    return rate, ch, mask, bits == 32 and (tag == 3 or (ext and raw[24:40] == _IEEE_FLOAT))


def describe_mix(raw: bytes) -> str:
    rate, ch, mask, is_float = _mix_info(raw)
    return f"{ch} кан. {rate} Гц, {'float32' if is_float else 'не float32'}, маска {mask:#x}, байты {raw.hex()}"


def _stream_format(mix: bytes, rate: int, ch: int, style: str) -> tuple[bytes, int, str]:
    """(формат потока, флаги потока, что выбрано — для журнала).

    Формат смешивания Windows принимает без преобразований, поэтому он отдаётся как есть, если
    совпадает с нужным. Иначе — float32 с переводом формата силами Windows; маска каналов —
    устройства, если число каналов то же, иначе стандартная раскладка."""
    mix_rate, mix_ch, mix_mask, is_float = _mix_info(mix)
    if style == "exact" and (rate, ch) == (mix_rate, mix_ch) and is_float:
        return mix, 0, "формат устройства как есть"
    if style == "plain":
        return struct.pack("<HHIIHHH", 3, ch, rate, rate * ch * 4, ch * 4, 32, 0), _CONVERT, "WAVEFORMATEX"
    mask = mix_mask if ch == mix_ch and bin(mix_mask).count("1") == ch else _MASKS.get(ch, 0)
    fmt = struct.pack("<HHIIHHHHI", 0xFFFE, ch, rate, rate * ch * 4, ch * 4, 32, 22, 32, mask) + _IEEE_FLOAT
    return fmt, _CONVERT, f"маска {mask:#x}"


def _fix_format(mf) -> None:
    """Исправляем, как soundcard описывает формат звука для Windows.

    soundcard объявляет WAVEFORMATEXTENSIBLE для cffi без упаковки, а в Windows структура упакована
    (#pragma pack(1)): у soundcard поля после первых 18 байт сдвинуты. Записывая «32 бита точности»,
    она на самом деле затирает маску каналов, и маска перестаёт совпадать с числом каналов; к тому же
    просит у Windows лишнее (подстройку частоты). Где-то это прощается, где-то — «invalid argument»
    при любом формате. Здесь формат собирается побайтно (см. _stream_format), а у recorder() есть
    параметр style — как описать формат (см. STYLES)."""
    if getattr(mf._AudioClient, "_callsum_fixed", False):
        return

    def init(self, ptr, samplerate, channels, blocksize, isloopback, exclusive_mode=False, style="exact"):
        self._ptr = ptr
        self.channelmap = list(range(channels)) if isinstance(channels, int) else list(channels)
        if blocksize is None:
            blocksize = self.deviceperiod[0] * samplerate
        fmt, flags, self.format_note = _stream_format(_mix_bytes(mf, ptr), int(samplerate),
                                                      len(set(self.channelmap)), style)
        if isloopback:
            flags |= _LOOPBACK
        self.format_note += f", флаги {flags:#x}"
        self._format = mf._ffi.new("char[]", fmt)  # память нужна, пока жив клиент
        mode = mf._ole32.AUDCLNT_SHAREMODE_EXCLUSIVE if exclusive_mode else mf._ole32.AUDCLNT_SHAREMODE_SHARED
        hr = ptr[0][0].lpVtbl.Initialize(ptr[0], mode, flags, int(blocksize / samplerate * 10_000_000), 0,
                                         mf._ffi.cast("WAVEFORMATEXTENSIBLE*", self._format), mf._ffi.NULL)
        if hr != 0:
            mf._com.release(ptr)  # клиент так и не заработал
            try:
                mf._com.check_error(hr)
            except RuntimeError as e:
                e.format_note = self.format_note
                raise
        self.samplerate = samplerate
        self._idle_start_time = None

    def recorder(self, samplerate, channels=None, blocksize=None, exclusive_mode=False, style="exact"):
        if channels is None:
            channels = self.channels
        return mf._Recorder(self._audio_client(), samplerate, channels, blocksize, self.isloopback,
                            exclusive_mode, style)

    mf._AudioClient.__init__ = init
    mf._Microphone.recorder = recorder
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
    def _mix_format(source) -> bytes | None:
        """Формат, в котором Windows сама смешивает звук устройства (см. _mix_bytes). Поток ровно
        в этом формате общий режим WASAPI принимает всегда, без всяких преобразований."""
        try:
            from soundcard import mediafoundation as mf

            client = source._audio_client()
            try:
                return _mix_bytes(mf, client)
            finally:
                mf._com.release(client)
        except Exception:  # noqa: BLE001 — не узнали: обойдёмся перебором
            return None

    def _open(self, source):
        """Открывает устройство в формате, который примет Windows.

        Сначала — ровно в формате, в котором Windows смешивает звук этого устройства: его она
        принимает всегда; он передаётся байт в байт, потом — собранным заново (см. STYLES). Если
        не вышло — перебираем частоты и число каналов: микрофон-массив из 4 каналов или вывод 5.1
        Windows может не открыть как «2 канала». Все каналы сводим в моно сами."""
        mix = self._mix_format(source)
        own = int(getattr(source, "channels", 0) or 0)
        tries = []
        if mix:
            rate, ch, _mask, is_float = _mix_info(mix)
            tries = [(rate, ch, style) for style in (STYLES if is_float else STYLES[1:])]
        tries += [(rate, ch, "ext") for rate in RATES for ch in (own, 2, 1, 4, 6, 8) if ch > 0]
        failed: list[str] = []
        last: Exception | None = None
        about = f"формат устройства: {describe_mix(mix) if mix else 'неизвестен'}"
        for rate, ch, style in dict.fromkeys(tries):
            try:
                rec = source.recorder(samplerate=rate, channels=ch, blocksize=rate // 20, style=style)
            except (RuntimeError, TypeError, ValueError, AssertionError) as e:
                last = e
                failed.append(f"{ch} кан. {rate} Гц ({getattr(e, 'format_note', style)}): "
                              f"{e or e.__class__.__name__}")
                if isinstance(e, RuntimeError) and str(e) not in _FORMAT_ERRORS:
                    break  # дело не в формате (устройство пропало, служба звука не работает…)
                continue
            try:
                opened = rec.__enter__()
            except (RuntimeError, TypeError, ValueError, AssertionError) as e:
                last = e
                failed.append(f"{ch} кан. {rate} Гц, запуск: {e or e.__class__.__name__}")
                try:
                    rec.__exit__(None, None, None)
                except Exception:  # noqa: BLE001 — устройство и так не работает
                    pass
                continue
            if failed:
                _log(f"{self.label}: открылось не с первой попытки ({about}); до этого: " + "; ".join(failed))
            return opened, rec, rate, ch
        _log(f"{self.label}: устройство не открылось ({about}); " + "; ".join(failed))
        if mix and last is not None and str(last) in _FORMAT_ERRORS:
            # отвергнут и формат самого устройства — так Windows отвечает, когда запись блокирует антивирус
            _log(f"{self.label}: антивирусы: {', '.join(_antivirus()) or 'не найдены'}")
            raise _Blocked(str(last)) from last
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
            note = getattr(rec, "format_note", "")
            _log(f"{self.label}: {getattr(source, 'name', self.device)} — {self.format}" + (f" ({note})" if note else ""))
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
