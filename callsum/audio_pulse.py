"""Запись звука в Linux: PipeWire/PulseAudio через ffmpeg."""

import ctypes
import json
import signal
import subprocess
import threading
from pathlib import Path

import numpy as np

from . import osdeps
from .audio_base import LEVEL_RATE, RECORDER_NAME, AudioError, level_of


def _pactl(*args: str) -> str:
    try:
        return osdeps.run(
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

    def __init__(self, device: str, out_path: Path, label: str, kind: str = ""):
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
            osdeps.ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
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
            self.level = level_of(samples, 32768.0)
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
