"""Запись звука через PipeWire/PulseAudio (ffmpeg) и работа с аудиофайлами."""

import ctypes
import json
import math
import signal
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

LEVEL_RATE = 8000  # частота служебного потока для индикатора громкости
RECORDER_NAME = "Саммари созвонов"  # так наши потоки записи видны в PipeWire


class AudioError(RuntimeError):
    pass


def _pactl(*args: str) -> str:
    try:
        return subprocess.run(
            ["pactl", *args], capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise AudioError(f"Не удалось выполнить pactl {' '.join(args)}: {e}") from e


def list_sources() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Возвращает (микрофоны, мониторы устройств вывода) как списки (name, description)."""
    data = json.loads(_pactl("-f", "json", "list", "sources") or "[]")
    mics, monitors = [], []
    for s in data:
        item = (s["name"], s.get("description") or s["name"])
        (monitors if s.get("monitor_source") else mics).append(item)
    return mics, monitors


def default_mic() -> str:
    return _pactl("get-default-source")


def default_monitor() -> str:
    return _pactl("get-default-sink") + ".monitor"


def _set_pdeathsig():
    # Если приложение упадёт, ffmpeg не должен остаться писать в фоне.
    PR_SET_PDEATHSIG = 1
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGINT)


class TrackRecorder:
    """Пишет один источник PulseAudio в Ogg/Opus и параллельно считает уровень сигнала."""

    def __init__(self, device: str, out_path: Path, label: str):
        self.device = device
        self.out_path = out_path
        self.label = label
        self.level = 0.0  # 0..1, для индикатора
        self.peak_seen = 0.0  # максимальный уровень за запись — чтобы заметить «тишину»
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr = b""

    def start(self):
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "pulse", "-name", RECORDER_NAME, "-stream_name", self.label,
            "-fragment_size", "19200",  # ~100 мс, чтобы индикатор не отставал
            "-i", self.device,
            # основная дорожка: моно Opus, ~15 МБ в час
            "-map", "0:a", "-ac", "1", "-ar", "48000",
            "-c:a", "libopus", "-b:a", "32k", "-application", "voip",
            str(self.out_path),
            # служебный поток для индикатора уровня
            "-map", "0:a", "-ac", "1", "-ar", str(LEVEL_RATE), "-f", "s16le",
            "-flush_packets", "1", "pipe:1",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                preexec_fn=_set_pdeathsig,
            )
        except OSError as e:
            raise AudioError(f"Не удалось запустить ffmpeg: {e}") from e
        self._reader = threading.Thread(target=self._read_levels, daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stderr(self):
        assert self._proc and self._proc.stderr
        self._stderr = self._proc.stderr.read()

    def _read_levels(self):
        assert self._proc and self._proc.stdout
        chunk = LEVEL_RATE // 10 * 2  # 100 мс
        while True:
            data = self._proc.stdout.read(chunk)
            if not data:
                break
            samples = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16)
            if samples.size == 0:
                continue
            rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / 32768.0
            # -60 дБ .. 0 дБ -> 0..1
            db = 20 * math.log10(rms) if rms > 0 else -100.0
            self.level = max(0.0, min(1.0, (db + 60) / 60))
            self.peak_seen = max(self.peak_seen, self.level)
        self.level = 0.0

    def error(self) -> str | None:
        """Текст ошибки, если ffmpeg завершился сам (например, нет устройства)."""
        if self._proc and self._proc.poll() not in (None, 0, 255):
            msg = self._stderr.decode(errors="replace").strip()
            return msg or f"ffmpeg завершился с кодом {self._proc.returncode}"
        return None

    def request_stop(self):
        # SIGINT: ffmpeg корректно допишет и закроет файл
        if self._proc and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGINT)

    def wait_stopped(self):
        if not self._proc:
            return
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        if self._reader:
            self._reader.join(timeout=2)

    def stop(self):
        self.request_stop()
        self.wait_stopped()


class Recorder:
    """Запись созвона: микрофон и звук собеседников на отдельные дорожки, с паузой.

    Пауза закрывает текущий кусок записи, продолжение начинает новый;
    при остановке куски склеиваются в mic.ogg / system.ogg.
    """

    def __init__(self, call_dir: Path, mic: str | None, system: str | None):
        self.call_dir = call_dir
        self.devices: dict[str, tuple[str, str]] = {}
        if mic is not None:
            self.devices["mic"] = (mic or default_mic(), "Микрофон")
        if system is not None:
            self.devices["system"] = (system or default_monitor(), "Собеседники")
        if not self.devices:
            raise AudioError("В настройках выключены оба источника звука.")
        self.tracks: dict[str, TrackRecorder] = {}
        self.segments: dict[str, list[Path]] = {k: [] for k in self.devices}
        self.peaks: dict[str, float] = {k: 0.0 for k in self.devices}
        self.paused = False
        self._done = 0.0          # длительность уже закрытых кусков
        self._seg_started = 0.0

    def _start_segment(self):
        n = len(next(iter(self.segments.values())))
        for key, (device, label) in self.devices.items():
            path = self.call_dir / f"{key}.seg{n}.ogg"
            self.segments[key].append(path)
            self.tracks[key] = TrackRecorder(device, path, label)
            self.tracks[key].start()
        self._seg_started = time.monotonic()

    def _close_segment(self):
        # сначала просим остановиться все дорожки разом, потом ждём: иначе, пока одна
        # дописывает файл, другая продолжает писать, и после паузы дорожки разъезжаются
        for t in self.tracks.values():
            t.request_stop()
        for key, t in self.tracks.items():
            t.wait_stopped()
            self.peaks[key] = max(self.peaks[key], t.peak_seen)
        self._done += time.monotonic() - self._seg_started

    def start(self):
        self._start_segment()

    def pause(self):
        if not self.paused:
            self._close_segment()
            self.paused = True

    def resume(self):
        if self.paused:
            self.paused = False
            self._start_segment()

    def device(self, key: str) -> str | None:
        return self.devices[key][0] if key in self.devices else None

    def switch_device(self, key: str, device: str):
        """Переключает источник на ходу (подключили гарнитуру, звонок ушёл на другое устройство).

        Начинается новый кусок записи на обеих дорожках, чтобы они оставались выровненными.
        """
        if key not in self.devices or self.devices[key][0] == device:
            return
        label = self.devices[key][1]
        if self.paused:
            self.devices[key] = (device, label)
            return
        self._close_segment()
        self.devices[key] = (device, label)
        self._start_segment()

    @property
    def elapsed(self) -> float:
        return self._done + (0.0 if self.paused else time.monotonic() - self._seg_started)

    def errors(self) -> list[str]:
        if self.paused:
            return []
        return [f"{t.label}: {e}" for t in self.tracks.values() if (e := t.error())]

    def silent_tracks(self) -> list[str]:
        return [self.devices[k][1].lower() for k, peak in self.peaks.items() if peak < 0.15]

    def stop(self) -> float:
        if not self.paused:
            self._close_segment()
        self.paused = True
        for key, parts in self.segments.items():
            parts = [p for p in parts if p.exists() and p.stat().st_size > 0]
            dst = self.call_dir / f"{key}.ogg"
            if len(parts) == 1:
                parts[0].replace(dst)
            elif parts:
                _concat(parts, dst)
                for p in parts:
                    p.unlink(missing_ok=True)
        return self._done


def _concat(parts: list[Path], dst: Path) -> None:
    listing = dst.with_suffix(".txt")
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0",
             "-i", listing.name, "-c", "copy", dst.name],
            capture_output=True, text=True, cwd=dst.parent,
        )
        if r.returncode != 0:
            raise AudioError(f"Не удалось склеить куски записи:\n{r.stderr.strip()}")
    finally:
        listing.unlink(missing_ok=True)


def extract_audio(src: Path, dst: Path) -> None:
    """Достаёт звук из любого аудио/видеофайла в компактный Ogg/Opus."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src), "-vn", "-ac", "1", "-ar", "48000",
        "-c:a", "libopus", "-b:a", "32k", "-application", "voip", str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AudioError(f"ffmpeg не смог прочитать файл:\n{r.stderr.strip()}")


def make_playback(tracks: dict[str, Path], dst: Path) -> None:
    """Сводит микрофон и собеседников в один файл для прослушивания."""
    inputs = [str(p) for p in tracks.values()]
    tmp = dst.with_suffix(".part.ogg")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for p in inputs:
        cmd += ["-i", p]
    cmd += ["-filter_complex", f"amix=inputs={len(inputs)}:duration=longest:normalize=0",
            "-ac", "1", "-c:a", "libopus", "-b:a", "40k", "-application", "voip", str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise AudioError(f"Не удалось подготовить запись для прослушивания:\n{r.stderr.strip()}")
    tmp.replace(dst)


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0
