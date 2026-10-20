"""Кто говорит: диаризация через sherpa-onnx и разметка слов транскрипта по говорящим.

Если есть отдельная дорожка собеседников (запись созвона), «Я» определяется надёжно:
это всё, что сказано, пока на дорожке собеседников тишина. Голоса собеседников
разделяются диаризацией только этой дорожки — без примеси вашего голоса она точнее.
Для импортированного файла диаризация идёт по всей записи.
"""

import bisect
import hashlib
import os
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .config import DATA_DIR

MODELS_DIR = DATA_DIR / "models"
_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION = {
    "url": f"{_RELEASES}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
    "sha256": "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
    "member": "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
    "file": "pyannote-segmentation-3.0.onnx",
}
EMBEDDING = {
    "url": f"{_RELEASES}/speaker-recongition-models/nemo_en_titanet_small.onnx",
    "sha256": "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e",
    "file": "nemo_en_titanet_small.onnx",
}
# Порог кластеризации: чем больше, тем меньше спикеров. 0.5 дробил одного человека на несколько;
# на настоящем 16-минутном созвоне 0.8 дал 15 спикеров вместо 42 и ни разу не слил разных людей,
# а с 0.9 разные люди уже начинали сливаться.
CLUSTER_THRESHOLD = 0.8
# После кластеризации: спикеров с настолько похожими голосами считаем одним человеком,
# а «спикеров» короче MIN_TALK секунд (обрывки, «угу») присоединяем к самому похожему.
MERGE_SIMILARITY = 0.7
MIN_TALK = 6.0
ABSORB_SIMILARITY = 0.4
# «спикер» с речью короче этого — почти всегда обрывок соседней реплики: голос по нему не понять
FRAGMENT_TALK = 3.0
SAMPLE_RATE = 16000
ME = "me"


class Cancelled(Exception):
    pass


# ---------------- модели ----------------

def _download(url: str, sha256: str, dst: Path, on_status: Callable[[str, float | None], None], label: str):
    fd, name = tempfile.mkstemp(dir=dst.parent, suffix=".part")
    os.close(fd)  # в Windows файл с открытым дескриптором нельзя переименовать
    tmp = Path(name)
    try:
        h = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 16):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                on_status(label, done / total if total else None)
        if h.hexdigest() != sha256:
            raise RuntimeError(f"Модель скачалась повреждённой ({url}). Попробуйте ещё раз.")
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def ensure_models(on_status: Callable[[str, float | None], None]) -> tuple[Path, Path]:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    seg = MODELS_DIR / SEGMENTATION["file"]
    emb = MODELS_DIR / EMBEDDING["file"]
    label = "Загрузка моделей для определения говорящих (~47 МБ, только в первый раз)…"
    if not seg.exists():
        archive = MODELS_DIR / "segmentation.tar.bz2"
        _download(SEGMENTATION["url"], SEGMENTATION["sha256"], archive, on_status, label)
        try:
            with tarfile.open(archive) as tar:
                src = tar.extractfile(SEGMENTATION["member"])
                if src is None:
                    raise RuntimeError("В архиве модели нет файла model.onnx")
                tmp = seg.with_suffix(".part")
                tmp.write_bytes(src.read())
                tmp.replace(seg)
        finally:
            archive.unlink(missing_ok=True)
    if not emb.exists():
        _download(EMBEDDING["url"], EMBEDDING["sha256"], emb, on_status, label)
    return seg, emb


# ---------------- диаризация ----------------

def diarize(audio: np.ndarray, num_speakers: int, on_progress: Callable[[float], None],
            cancelled: Callable[[], bool], models: tuple[Path, Path]) -> list[tuple[float, float, int]]:
    """Возвращает отрезки речи [(начало, конец, номер говорящего)]."""
    import sherpa_onnx as so

    if audio.size < SAMPLE_RATE * 2:
        return []
    threads = max(1, (os.cpu_count() or 4) - 2)
    seg, emb = models
    cfg = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            # шаг окна 0.5 вместо 0.1: в 5 раз быстрее без потери точности на наших тестах
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg), window_shift_ratio=0.5),
            num_threads=threads,
        ),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=str(emb), num_threads=threads),
        clustering=so.FastClusteringConfig(
            num_clusters=num_speakers if num_speakers > 0 else -1, threshold=CLUSTER_THRESHOLD
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not cfg.validate():
        raise RuntimeError("Не удалось загрузить модели определения говорящих.")
    sd = so.OfflineSpeakerDiarization(cfg)

    def callback(done, total):
        on_progress(done / total if total else 0.0)
        return 1 if cancelled() else 0

    result = sd.process(audio, callback=callback)
    if cancelled():
        raise Cancelled()
    return [(r.start, r.end, r.speaker) for r in result.sort_by_start_time()]


def _extractor(models: tuple[Path, Path]):
    import sherpa_onnx as so

    threads = max(1, (os.cpu_count() or 4) - 2)
    return so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(model=str(models[1]), num_threads=threads))


def _embed(extractor, chunk: np.ndarray) -> np.ndarray:
    stream = extractor.create_stream()
    stream.accept_waveform(SAMPLE_RATE, chunk)
    stream.input_finished()
    v = np.asarray(extractor.compute(stream), dtype=np.float32)
    return v / (np.linalg.norm(v) or 1.0)


def _mean_voice(spans: list[tuple[float, float]], audio: np.ndarray, extractor, limit: float = 30.0) -> np.ndarray | None:
    """Средний «отпечаток» по самым длинным отрезкам (края обрезаем — там бывает чужая речь)."""
    vectors, total = [], 0.0
    for s, e in sorted(spans, key=lambda x: x[0] - x[1]):
        s, e = s + 0.2, e - 0.2
        chunk = audio[int(s * SAMPLE_RATE):int(e * SAMPLE_RATE)]
        if chunk.size < SAMPLE_RATE:
            continue
        vectors.append(_embed(extractor, chunk))
        total += e - s
        if total >= limit or len(vectors) >= 12:
            break
    if not vectors:
        return None
    m = np.mean(vectors, axis=0)
    return m / (np.linalg.norm(m) or 1.0)


def merge_clusters(turns: list[tuple[float, float, int]], audio: np.ndarray, models: tuple[Path, Path],
                   max_speakers: int = 0) -> list[tuple[float, float, int]]:
    """Склеивает одного человека, разбитого на нескольких спикеров.

    1. Спикеры с очень похожими голосами (MERGE_SIMILARITY) — один человек.
    2. Если известно, сколько людей было (приглашённые из календаря), а спикеров больше — склеиваем
       самые похожие, пока не станет столько же (но только достаточно похожих).
    3. Короткие обрывки присоединяем к самому похожему голосу, а совсем короткие (по ним голос
       не определить) — к тому, кто говорил рядом по времени.
    """
    talk: dict[int, float] = {}
    spans: dict[int, list[tuple[float, float]]] = {}
    for s, e, k in turns:
        talk[k] = talk.get(k, 0.0) + e - s
        spans.setdefault(k, []).append((s, e))
    if len(talk) < 2:
        return turns
    extractor = _extractor(models)
    voice = {k: v for k in talk if (v := _mean_voice(spans[k], audio, extractor)) is not None}
    parent = {k: k for k in talk}

    def root(k: int) -> int:
        while parent[k] != k:
            k = parent[k]
        return k

    def join(a: int, b: int) -> None:
        """b — в a (a остаётся тот, кто говорил больше); голос — среднее с весом по времени речи."""
        if talk[b] > talk[a]:
            a, b = b, a
        parent[b] = a
        if a in voice and b in voice:
            m = voice[a] * talk[a] + voice[b] * talk[b]
            voice[a] = m / (np.linalg.norm(m) or 1.0)
        talk[a] += talk[b]
        voice.pop(b, None)

    while True:
        roots = [k for k in voice if root(k) == k]
        big = [k for k in roots if talk[k] >= MIN_TALK]
        best, pair = -1.0, None
        for i, a in enumerate(big):
            for b in big[i + 1:]:
                sim = float(voice[a] @ voice[b])
                if sim > best:
                    best, pair = sim, (a, b)
        too_many = max_speakers > 0 and len(big) > max_speakers
        if pair and (best >= MERGE_SIMILARITY or (too_many and best >= 0.5)):
            join(*pair)
            continue
        break
    big = [k for k in voice if root(k) == k and talk[k] >= MIN_TALK]
    for k in [k for k in voice if root(k) == k and talk[k] < MIN_TALK]:
        sims = [(float(voice[k] @ voice[b]), b) for b in big]
        if sims:
            sim, b = max(sims)
            if sim >= ABSORB_SIMILARITY:
                join(b, k)
    voiced = [(s, e, k) for s, e, k in turns if talk[root(k)] >= FRAGMENT_TALK]
    for k in [k for k in talk if root(k) == k and (talk[k] < FRAGMENT_TALK or (k not in voice and talk[k] < MIN_TALK))]:
        gaps = [(max(ts - e, s - te, 0.0), root(vk)) for s, e, kk in turns if kk == k
                for ts, te, vk in voiced if root(vk) != k]
        if gaps:
            join(min(gaps)[1], k)
    return [(s, e, root(k)) for s, e, k in turns]


# ---------------- «отпечатки» голосов ----------------

def voice_embeddings(paragraphs: list[dict], audio_for: Callable[[str], np.ndarray | None],
                     models: tuple[Path, Path]) -> dict[str, list[float]]:
    """Средний вектор голоса каждого говорящего — по его самым длинным репликам (до минуты речи)."""
    import sherpa_onnx as so

    threads = max(1, (os.cpu_count() or 4) - 2)
    extractor = so.SpeakerEmbeddingExtractor(
        so.SpeakerEmbeddingExtractorConfig(model=str(models[1]), num_threads=threads)
    )
    result: dict[str, list[float]] = {}
    for sid in speaker_ids(paragraphs):
        audio = audio_for(sid)
        if audio is None:
            continue
        # края реплик обрезаем: там бывает хвост чужой речи
        spans = sorted(((p["start"] + 0.2, p["end"] - 0.2) for p in paragraphs
                        if p.get("speaker") == sid and p["end"] - p["start"] >= 1.2),
                       key=lambda s: s[0] - s[1])
        vectors, total = [], 0.0
        for s, e in spans[:12]:
            chunk = audio[int(s * SAMPLE_RATE):int(e * SAMPLE_RATE)]
            if chunk.size < SAMPLE_RATE:
                continue
            stream = extractor.create_stream()
            stream.accept_waveform(SAMPLE_RATE, chunk)
            stream.input_finished()
            v = np.asarray(extractor.compute(stream), dtype=np.float32)
            vectors.append(v / (np.linalg.norm(v) or 1.0))
            total += e - s
            if total >= 60:
                break
        if vectors:
            m = np.mean(vectors, axis=0)
            result[sid] = [round(float(x), 5) for x in m / (np.linalg.norm(m) or 1.0)]
    return result


# ---------------- разметка слов ----------------

def _overlaps(words: list[dict], turns: list[tuple[float, float, int]]) -> list[dict[int, float]]:
    """Для каждого слова — сколько секунд оно перекрывается с речью каждого говорящего."""
    starts = [t[0] for t in turns]
    max_len = max((t[1] - t[0] for t in turns), default=0.0)
    out = []
    for w in words:
        acc: dict[int, float] = {}
        i = bisect.bisect_left(starts, w["s"] - max_len)
        while i < len(turns) and turns[i][0] < w["e"]:
            s, e, spk = turns[i]
            o = min(e, w["e"]) - max(s, w["s"])
            if o > 0:
                acc[spk] = acc.get(spk, 0.0) + o
            i += 1
        out.append(acc)
    return out


def _smooth(words: list[dict], labels: list[str | None]) -> list[str | None]:
    """Убирает «дребезг»: короткий кусок другого собеседника посреди реплики одного человека."""
    labels = list(labels)
    runs = []  # (начало, конец) индексов подряд идущих одинаковых меток
    i = 0
    while i < len(labels):
        j = i
        while j + 1 < len(labels) and labels[j + 1] == labels[i]:
            j += 1
        runs.append((i, j))
        i = j + 1
    for k in range(1, len(runs) - 1):
        a, b = runs[k]
        prev, nxt = labels[runs[k - 1][0]], labels[runs[k + 1][0]]
        lab = labels[a]
        if lab == ME or prev != nxt or prev in (None, ME):
            continue
        if b - a < 2 and words[b]["e"] - words[a]["s"] < 0.8:
            for x in range(a, b + 1):
                labels[x] = prev
    return labels


_SENTENCE_END = (".", "!", "?", "…")


def _snap_to_sentences(words: list[dict], labels: list[str | None], window: int = 3) -> list[str | None]:
    """Сдвигает смену говорящего к ближайшему концу предложения.

    Таймкоды слов и границы речи немного расходятся, и на стыке реплик слово-другое
    уходит соседу. Люди почти всегда сменяют друг друга на конце фразы — подтягиваем границу туда.
    """
    labels = list(labels)
    ends = [w["w"].rstrip().endswith(_SENTENCE_END) for w in words]
    changes = [i for i in range(1, len(labels)) if labels[i] != labels[i - 1]]
    for n, i in enumerate(changes):
        if ends[i - 1]:
            continue
        lo = max(changes[n - 1] + 1 if n else 1, i - window)
        hi = min(changes[n + 1] - 1 if n + 1 < len(changes) else len(labels) - 1, i + window)
        candidates = [k for k in range(lo, hi + 1) if ends[k - 1]]
        if not candidates:
            continue
        k = min(candidates, key=lambda c: abs(c - i))
        if k < i:
            for x in range(k, i):
                labels[x] = labels[i]
        else:
            for x in range(i, k):
                labels[x] = labels[i - 1]
    return labels


def _fold_fragments(words: list[dict], labels: list[str | None]) -> list[str | None]:
    """Собеседник, которому досталось меньше FRAGMENT_TALK секунд слов, — почти всегда обрывок
    чужой реплики (кусок звука ушёл не тому кластеру). Отдаём его слова ближайшему по времени
    другому собеседнику, а не отдельному «Спикеру 17» с одним словом."""
    labels = list(labels)
    talk: dict[str, float] = {}
    for w, lab in zip(words, labels, strict=True):
        if lab not in (None, ME):
            talk[lab] = talk.get(lab, 0.0) + max(0.0, w["e"] - w["s"])
    small = {lab for lab, t in talk.items() if t < FRAGMENT_TALK}
    if not small or len(talk) - len(small) < 1:
        return labels
    keep = [i for i, lab in enumerate(labels) if lab in talk and lab not in small]
    starts = [words[i]["s"] for i in keep]
    for i, lab in enumerate(labels):
        if lab in small:
            j = bisect.bisect_left(starts, words[i]["s"])
            near = [k for k in (j - 1, j) if 0 <= k < len(keep)]
            best = min(near, key=lambda k: abs(words[keep[k]]["s"] - words[i]["s"]))
            labels[i] = labels[keep[best]]
    return labels


def _renumber(labels: list[str | None]) -> list[str | None]:
    """Номера собеседников по порядку первого появления: S1, S2…"""
    order: dict[str, str] = {}
    out = []
    for lab in labels:
        if lab is None or lab == ME:
            out.append(lab)
            continue
        if lab not in order:
            order[lab] = f"S{len(order) + 1}"
        out.append(order[lab])
    return out


def label_words(words: list[dict], turns: list[tuple[float, float, int]], me_by_track: bool) -> list[str | None]:
    """Метка говорящего для каждого слова.

    me_by_track: turns получены с дорожки собеседников, и всё, что вне их речи, — «Я».
    """
    if not words:
        return []
    ov = _overlaps(words, turns)
    labels: list[str | None] = []
    if me_by_track:
        for w, acc in zip(words, ov, strict=True):
            dur = max(0.05, w["e"] - w["s"])
            if acc and max(acc.values()) / dur >= 0.3:
                labels.append(f"_{max(acc, key=acc.get)}")
            else:
                labels.append(ME)
    else:
        if not turns:
            return [None] * len(words)
        mids = [(t[0] + t[1]) / 2 for t in turns]
        for w, acc in zip(words, ov, strict=True):
            if acc:
                labels.append(f"_{max(acc, key=acc.get)}")
            else:
                # слово попало в паузу между отрезками — берём ближайший
                m = (w["s"] + w["e"]) / 2
                k = min(range(len(turns)), key=lambda i: abs(mids[i] - m))
                labels.append(f"_{turns[k][2]}")
    return _renumber(_snap_to_sentences(words, _fold_fragments(words, _smooth(words, labels))))


def activity_turns(audio: np.ndarray, threshold_db: float = -45.0) -> list[tuple[float, float, int]]:
    """Простой детектор речи по громкости — когда определение по голосу выключено."""
    frame = SAMPLE_RATE // 10
    n = audio.size // frame
    if n == 0:
        return []
    rms = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
    active = 20 * np.log10(rms) > threshold_db
    turns, start = [], None
    for i, a in enumerate(active):
        if a and start is None:
            start = i
        elif not a and start is not None:
            turns.append((start / 10, i / 10, 0))
            start = None
    if start is not None:
        turns.append((start / 10, n / 10, 0))
    # склеиваем отрезки с паузами короче 0.4 с
    merged: list[tuple[float, float, int]] = []
    for t in turns:
        if merged and t[0] - merged[-1][1] < 0.4:
            merged[-1] = (merged[-1][0], t[1], 0)
        else:
            merged.append(t)
    return merged


# ---------------- абзацы и имена ----------------

def build_paragraphs(words: list[dict], labels: list[str | None]) -> list[dict]:
    paras: list[dict] = []
    for w, lab in zip(words, labels, strict=True):
        if paras:
            p = paras[-1]
            long_enough = p["end"] - p["start"] > 30 and p["text"].rstrip()[-1:] in ".!?…"
            if p["speaker"] == lab and w["s"] - p["end"] < 2.0 and not long_enough and p["end"] - p["start"] < 90:
                p["text"] += w["w"]
                p["end"] = w["e"]
                continue
        paras.append({"start": round(w["s"], 2), "end": round(w["e"], 2), "text": w["w"], "speaker": lab})
    for p in paras:
        p["text"] = p["text"].strip()
        p["end"] = round(p["end"], 2)
    return [p for p in paras if p["text"]]


def speaker_ids(paragraphs: list[dict]) -> list[str]:
    seen: list[str] = []
    for p in paragraphs:
        sid = p.get("speaker")
        if sid and sid not in seen:
            seen.append(sid)
    return seen


def default_names(ids: list[str]) -> dict[str, str]:
    remote = [s for s in ids if s.startswith("S")]
    names = {}
    for sid in ids:
        if sid == ME:
            names[sid] = "Я"
        elif sid.startswith("S"):
            names[sid] = "Собеседник" if len(remote) == 1 else f"Спикер {sid[1:]}"
        else:
            names[sid] = sid  # старые записи: «Я» / «Собеседник»
    return names


def talk_time(paragraphs: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in paragraphs:
        sid = p.get("speaker")
        if sid:
            out[sid] = out.get(sid, 0.0) + max(0.0, p["end"] - p["start"])
    return out
