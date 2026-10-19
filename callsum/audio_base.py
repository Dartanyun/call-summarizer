"""Общее для записи звука в Linux и Windows."""

import math

import numpy as np

LEVEL_RATE = 8000  # частота служебного потока для индикатора громкости (Linux)
RECORDER_NAME = "Саммари созвонов"  # так наши потоки записи видны в системе


class AudioError(RuntimeError):
    pass


def level_of(samples: np.ndarray, full_scale: float = 1.0) -> float:
    """Громкость для индикатора: -60 дБ … 0 дБ → 0…1."""
    if samples.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / full_scale
    db = 20 * math.log10(rms) if rms > 0 else -100.0
    return max(0.0, min(1.0, (db + 60) / 60))
