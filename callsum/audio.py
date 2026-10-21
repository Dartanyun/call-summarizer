"""Запись звука и работа с аудиофайлами.

Как именно пишется звук, зависит от системы: в Linux — PipeWire/PulseAudio через ffmpeg
(audio_pulse.py), в Windows — WASAPI через soundcard (audio_win.py). Здесь общее: запись
кусками с паузой, склейка, сведение для прослушивания, длительность файла.
"""

import re
import time
from pathlib import Path

from . import osdeps
from .audio_base import LEVEL_RATE, RECORDER_NAME, AudioError  # noqa: F401 — используются снаружи

if osdeps.IS_WIN:
    from .audio_win import TrackRecorder, default_mic, default_monitor, list_sources
else:
    from .audio_pulse import TrackRecorder, default_mic, default_monitor, list_sources

__all__ = ["AudioError", "RECORDER_NAME", "Recorder", "default_mic", "default_monitor", "list_sources",
           "extract_audio", "make_playback", "probe_duration"]


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
            self.tracks[key] = TrackRecorder(device, path, label, key)
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
        # одна и та же причина у обеих дорожек (антивирус, служба звука) — один раз
        found: dict[str, list[str]] = {}
        for t in self.tracks.values():
            if e := t.error():
                found.setdefault(e, []).append(t.label)
        return [" и ".join([labels[0], *(x.lower() for x in labels[1:])]) + f": {e}" for e, labels in found.items()]

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
        r = osdeps.run(
            [osdeps.ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0",
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
        osdeps.ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src), "-vn", "-ac", "1", "-ar", "48000",
        "-c:a", "libopus", "-b:a", "32k", "-application", "voip", str(dst),
    ]
    r = osdeps.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AudioError(f"ffmpeg не смог прочитать файл:\n{r.stderr.strip()}")


def make_playback(tracks: dict[str, Path], dst: Path) -> None:
    """Сводит микрофон и собеседников в один файл для прослушивания."""
    inputs = [str(p) for p in tracks.values()]
    tmp = dst.with_suffix(".part.ogg")
    cmd = [osdeps.ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for p in inputs:
        cmd += ["-i", p]
    cmd += ["-filter_complex", f"amix=inputs={len(inputs)}:duration=longest:normalize=0",
            "-ac", "1", "-c:a", "libopus", "-b:a", "40k", "-application", "voip", str(tmp)]
    r = osdeps.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise AudioError(f"Не удалось подготовить запись для прослушивания:\n{r.stderr.strip()}")
    tmp.replace(dst)


def probe_duration(path: Path) -> float:
    """Длительность файла по ffmpeg (ffprobe в Windows-сборке ffmpeg нет)."""
    r = osdeps.run([osdeps.ffmpeg(), "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    # длительность не записана в заголовке — считаем, проиграв файл «вхолостую»
    r = osdeps.run([osdeps.ffmpeg(), "-hide_banner", "-nostdin", "-i", str(path), "-f", "null", "-"],
                   capture_output=True, text=True)
    times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
    if times:
        h, m_, sec = times[-1]
        return int(h) * 3600 + int(m_) * 60 + float(sec)
    return 0.0
