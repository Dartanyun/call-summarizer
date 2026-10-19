"""Локальное распознавание речи через faster-whisper."""

import os
from collections.abc import Callable
from pathlib import Path

import numpy as np

from . import osdeps

SAMPLE_RATE = 16000

_model = None
_model_key = None
device_used = ""  # где работает модель: "cpu" или "cuda" (для подписи в окне)


class Cancelled(Exception):
    pass


def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except (ImportError, RuntimeError, OSError):
        return False


def _load_model(name: str, device: str = "auto"):
    """device: auto — видеокарта NVIDIA, если она есть и для неё стоят библиотеки CUDA, иначе процессор."""
    global _model, _model_key, device_used
    want = "cuda" if device == "cuda" or (device == "auto" and cuda_available()) else "cpu"
    if _model_key != (name, want):
        from faster_whisper import WhisperModel

        _model = None  # освобождаем память от предыдущей модели
        if want == "cuda":
            try:
                model = WhisperModel(name, device="cuda", compute_type="float16")
                # библиотеки CUDA (cuBLAS, cuDNN) подгружаются только при работе — проверяем сразу
                list(model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="ru")[0])
                _model, _model_key, device_used = model, (name, want), "cuda"
                return _model
            except (RuntimeError, OSError, ValueError) as e:
                if device == "cuda":
                    raise RuntimeError(
                        "Не получилось распознавать на видеокарте: нет драйвера NVIDIA или библиотек CUDA "
                        "(cuBLAS 12, cuDNN 9). Выберите в настройках «Процессор» или см. справку → «Видеокарта».") from e
                want = "cpu"
        threads = max(1, (os.cpu_count() or 4) - 2)
        _model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)
        _model_key, device_used = (name, want), "cpu"
    return _model


# Шумоподавление: срезаем гул ниже 80 Гц и убираем постоянный шум (вентилятор, улица).
# Длительность не меняется, так что таймкоды остаются точными.
DENOISE_FILTER = "highpass=f=80,afftdn=nr=12:nf=-30:tn=1"


def decode(path: Path, denoise: bool = False) -> np.ndarray:
    # системный ffmpeg вместо PyAV: не зависим от несовместимых версий av
    r = osdeps.run(
        [osdeps.ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
         *(["-af", DENOISE_FILTER] if denoise else []),
         "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Не удалось прочитать {path.name}: {r.stderr.decode(errors='replace').strip()}")
    return np.frombuffer(r.stdout, dtype=np.float32)


def mix(tracks: list[np.ndarray]) -> np.ndarray:
    n = max(len(t) for t in tracks)
    out = np.zeros(n, dtype=np.float32)
    for t in tracks:
        out[: len(t)] += t
    return np.clip(out, -1.0, 1.0)


def transcribe(
    tracks: dict[str, Path],
    model_name: str,
    language: str,
    on_status: Callable[[str, float | None], None],
    cancelled: Callable[[], bool] = lambda: False,
    vocabulary: list[str] = (),
    device: str = "auto",
    denoise: bool = False,
) -> list[dict]:
    """Распознаёт созвон и возвращает слова с таймкодами: [{"s": начало, "e": конец, "w": " слово"}].

    tracks: {"mic": путь, "system": путь} для записи или {"audio": путь} для импорта.
    Дорожки смешиваются: так речь не задваивается, даже если микрофон слышит колонки.
    on_status(текст, доля 0..1 или None для неопределённого прогресса).
    """
    if not _model_key or _model_key[0] != model_name:
        on_status(f"Загрузка модели {model_name} (в первый раз она скачивается, это может занять несколько минут)…", None)
    model = _load_model(model_name, device)

    on_status("Подготовка звука" + (" и шумоподавление…" if denoise else "…"), None)
    decoded = [a for a in (decode(p, denoise) for p in tracks.values()) if a.size > SAMPLE_RATE // 2]
    if not decoded:
        raise RuntimeError("В записи нет звука.")
    audio = mix(decoded)

    segments, info = model.transcribe(
        audio,
        language=language or None,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 700},
        # без этого Whisper на длинных записях иногда зацикливается на одной фразе
        condition_on_previous_text=False,
        # таймкоды слов нужны, чтобы разделить речь по говорящим
        word_timestamps=True,
        # словарь пользователя: подсказка в каждом окне распознавания (длина подсказки ограничена)
        hotwords=", ".join(vocabulary)[:300] or None,
    )
    total = info.duration or 1.0
    words: list[dict] = []
    for seg in segments:
        if cancelled():
            raise Cancelled()
        if seg.words:
            for w in seg.words:
                if w.word.strip():
                    words.append({"s": round(float(w.start), 2), "e": round(float(max(w.end, w.start + 0.05)), 2),
                                  "w": w.word})
        elif seg.text.strip():
            words.append({"s": round(float(seg.start), 2), "e": round(float(seg.end), 2), "w": " " + seg.text.strip()})
        on_status("Распознавание речи…", min(1.0, seg.end / total))
    return words


def _ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def format_transcript(paragraphs: list[dict], names: dict[str, str]) -> str:
    """Транскрипт текстом: «[00:01:23] Ира: …»."""
    lines = []
    for p in paragraphs:
        sid = p.get("speaker")
        who = f"{names.get(sid, sid)}: " if sid else ""
        lines.append(f"[{_ts(p['start'])}] {who}{p['text']}")
    return "\n\n".join(lines)
