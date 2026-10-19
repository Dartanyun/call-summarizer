"""Запомненные голоса: «отпечаток» голоса (вектор TitaNet) для каждого имени.

Хранятся только на этом компьютере, в ~/.local/share/call-summarizer/voices.json.
"""

import json
import threading
from datetime import datetime

import numpy as np

from .config import DATA_DIR

VOICES_FILE = DATA_DIR / "voices.json"
# Голос считается узнанным, если похож на профиль не меньше чем на MATCH и заметно
# ближе к нему, чем ко второму кандидату. Пороги взяты с запасом: лучше не узнать,
# чем подписать чужим именем.
MATCH = 0.65
MARGIN = 0.10
MAX_WEIGHT = 20  # старые созвоны перестают перевешивать новые
OWN_VOICE = "Я"
_lock = threading.Lock()


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def load() -> dict[str, dict]:
    try:
        return json.loads(VOICES_FILE.read_text(encoding="utf-8")).get("voices", {})
    except (OSError, ValueError):
        return {}


def _save(profiles: dict[str, dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = VOICES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "voices": profiles}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(VOICES_FILE)


def remember(name: str, embedding) -> None:
    """Добавляет голос к профилю имени (скользящее среднее)."""
    emb = _unit(embedding)
    with _lock:
        profiles = load()
        p = profiles.get(name)
        if p and len(p["emb"]) == emb.size:
            n = min(int(p.get("n", 1)), MAX_WEIGHT)
            emb = _unit(_unit(p["emb"]) * n + emb)
            count = int(p.get("calls", n)) + 1
        else:
            n, count = 0, 1
        profiles[name] = {"emb": [round(float(x), 5) for x in emb], "n": n + 1, "calls": count,
                          "updated": datetime.now().isoformat(timespec="seconds")}
        _save(profiles)


def forget(name: str) -> None:
    with _lock:
        profiles = load()
        profiles.pop(name, None)
        _save(profiles)


def forget_all() -> None:
    with _lock:
        _save({})


def match(embeddings: dict[str, list[float]], exclude: set[str] = frozenset()) -> dict[str, tuple[str, float]]:
    """Сопоставляет говорящих созвона с запомненными голосами: {sid: (имя, сходство)}."""
    profiles = {k: _unit(v["emb"]) for k, v in load().items() if k not in exclude}
    if not profiles or not embeddings:
        return {}
    pairs = []
    for sid, emb in embeddings.items():
        e = _unit(emb)
        sims = sorted(((float(e @ p), name) for name, p in profiles.items() if p.size == e.size), reverse=True)
        if not sims:
            continue
        best, name = sims[0]
        second = sims[1][0] if len(sims) > 1 else -1.0
        if best >= MATCH and best - second >= MARGIN:
            pairs.append((best, sid, name))
    # одно имя — одному говорящему: сначала самые уверенные совпадения
    result, used = {}, set()
    for sim, sid, name in sorted(pairs, reverse=True):
        if sid not in result and name not in used:
            result[sid] = (name, sim)
            used.add(name)
    return result
