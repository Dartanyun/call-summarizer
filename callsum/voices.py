"""Запомненные голоса: «отпечатки» голоса (векторы TitaNet) для каждого имени.

У одного человека может быть несколько вариантов голоса: гарнитура и ноутбук, телефон, другая
комната звучат по-разному. Поэтому для имени хранится до MAX_VARIANTS «отпечатков», а голос
считается узнанным, если похож хотя бы на один из них.

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
MAX_VARIANTS = 4
VARIANT_SIMILARITY = 0.75  # настолько похожий голос — тот же вариант, иначе новый
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


def variants(profile: dict) -> list[dict]:
    """Варианты голоса: [{"emb": вектор, "n": сколько раз пополнялся}]. Старые профили — один вариант."""
    if profile.get("variants"):
        return [v for v in profile["variants"] if v.get("emb")]
    return [{"emb": profile["emb"], "n": int(profile.get("n", 1))}] if profile.get("emb") else []


def _round(v: np.ndarray) -> list[float]:
    return [round(float(x), 5) for x in v]


def remember(name: str, embedding) -> None:
    """Добавляет голос к профилю имени: к похожему варианту (скользящее среднее) или новым вариантом."""
    emb = _unit(embedding)
    with _lock:
        profiles = load()
        p = profiles.get(name) or {}
        found = [v for v in variants(p) if len(v["emb"]) == emb.size]
        sims = [float(_unit(v["emb"]) @ emb) for v in found]
        if found and max(sims) >= VARIANT_SIMILARITY:
            v = found[sims.index(max(sims))]
            n = min(int(v.get("n", 1)), MAX_WEIGHT)
            v["emb"], v["n"] = _round(_unit(_unit(v["emb"]) * n + emb)), n + 1
        elif len(found) < MAX_VARIANTS:
            found.append({"emb": _round(emb), "n": 1})
        else:
            # мест нет: вливаем в самый похожий вариант
            v = found[sims.index(max(sims))]
            n = min(int(v.get("n", 1)), MAX_WEIGHT)
            v["emb"], v["n"] = _round(_unit(_unit(v["emb"]) * n + emb)), n + 1
        profiles[name] = {"variants": found, "calls": int(p.get("calls", p.get("n", 0) or 0)) + 1,
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


def match_all(embeddings: dict[str, list[float]], exclude: set[str] = frozenset()) -> dict[str, tuple[str, float]]:
    """Каждому говорящему — запомненное имя, если голос уверенно похож на него: {sid: (имя, сходство)}.
    Одно имя может достаться нескольким говорящим — это один человек, разбитый на части."""
    profiles = {name: [_unit(v["emb"]) for v in variants(p)] for name, p in load().items() if name not in exclude}
    if not profiles or not embeddings:
        return {}
    result = {}
    for sid, emb in embeddings.items():
        e = _unit(emb)
        sims = sorted(((max(float(e @ v) for v in vs if v.size == e.size), name)
                       for name, vs in profiles.items() if any(v.size == e.size for v in vs)), reverse=True)
        if not sims:
            continue
        best, name = sims[0]
        second = sims[1][0] if len(sims) > 1 else -1.0
        if best >= MATCH and best - second >= MARGIN:
            result[sid] = (name, best)
    return result


def match(embeddings: dict[str, list[float]], exclude: set[str] = frozenset()) -> dict[str, tuple[str, float]]:
    """Как match_all, но одно имя — одному говорящему (самому уверенному)."""
    result, used = {}, set()
    for sid, (name, sim) in sorted(match_all(embeddings, exclude).items(), key=lambda x: -x[1][1]):
        if name not in used:
            result[sid] = (name, sim)
            used.add(name)
    return result


def similarity(a, b) -> float:
    return float(_unit(a) @ _unit(b))
