"""Библиотека созвонов: каждая запись — отдельная папка с аудио, транскриптом и саммари."""

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import sections, speakers, transcribe
from .config import CALLS_DIR

TRACK_FILES = ("mic.ogg", "system.ogg", "audio.ogg")
_save_lock = threading.Lock()


def _word_pattern(word: str) -> re.Pattern:
    # «Спикер 1» не должен зацепить «Спикер 10», а «Ира» — «Ирак»
    return re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE)


def apply_corrections(text: str, corrections: dict[str, str]) -> str:
    for wrong, right in corrections.items():
        text = _word_pattern(wrong).sub(lambda _m, r=right: r, text)
    return text


class Call:
    def __init__(self, path: Path):
        self.path = path
        self.meta: dict = {}
        try:
            self.meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    @property
    def id(self) -> str:
        return self.path.name

    @property
    def title(self) -> str:
        return self.meta.get("title") or "Без названия"

    @property
    def created(self) -> datetime:
        try:
            return datetime.fromisoformat(self.meta["created"])
        except (KeyError, ValueError):
            return datetime.fromtimestamp(self.path.stat().st_mtime)

    @property
    def duration(self) -> float:
        return float(self.meta.get("duration") or 0)

    @property
    def status(self) -> str:
        return self.meta.get("status", "")

    def tracks(self) -> dict[str, Path]:
        return {f.split(".")[0]: self.path / f for f in TRACK_FILES if (self.path / f).exists()}

    def delete_audio(self) -> None:
        """Удаляет звук, оставляя транскрипт, саммари и «отпечатки» голосов."""
        for f in [*TRACK_FILES, "playback.ogg"]:
            (self.path / f).unlink(missing_ok=True)
        for f in self.path.glob("*.seg*.ogg"):
            f.unlink(missing_ok=True)
        self.save(audio_deleted=datetime.now().isoformat(timespec="seconds"))

    def playback_path(self) -> Path | None:
        """Файл для прослушивания: одна дорожка как есть, две — заранее сведённые в playback.ogg."""
        tracks = self.tracks()
        if len(tracks) == 1:
            return next(iter(tracks.values()))
        mixed = self.path / "playback.ogg"
        return mixed if mixed.exists() else None

    def save(self, **changes) -> None:
        # meta.json пишут и окно, и фоновый поток обработки
        with _save_lock:
            self.meta.update(changes)
            tmp = self.path / "meta.json.tmp"
            tmp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path / "meta.json")

    # транскрипт
    def has_transcript(self) -> bool:
        return (self.path / "transcript.txt").exists()

    def transcript(self) -> str:
        try:
            return (self.path / "transcript.txt").read_text(encoding="utf-8")
        except OSError:
            return ""

    def _read_json(self, name: str, default):
        try:
            return json.loads((self.path / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write_json(self, name: str, data) -> None:
        tmp = self.path / f"{name}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path / name)

    # слова с таймкодами (результат Whisper) — из них заново собираются абзацы
    def words(self) -> list[dict]:
        words = self._read_json("words.json", None)
        if words is None:
            # старые записи: слов нет, есть только абзацы
            words = [{"s": p["start"], "e": p["end"], "w": " " + p["text"]} for p in self.paragraphs()]
        return words

    def write_words(self, words: list[dict]) -> None:
        self._write_json("words.json", words)

    # абзацы с метками говорящих: [{"start", "end", "text", "speaker": "me" | "S1" | None}]
    def paragraphs(self) -> list[dict]:
        data = self._read_json("segments.json", [])
        return data if isinstance(data, list) else []

    def write_paragraphs(self, paragraphs: list[dict], reset_names: bool = False) -> None:
        corrections = self.meta.get("corrections") or {}
        if corrections:
            paragraphs = [{**p, "text": apply_corrections(p["text"], corrections)} for p in paragraphs]
        self._write_json("segments.json", paragraphs)
        if reset_names:
            # номера спикеров после нового разбора не совпадают со старыми; своё имя («Я») оставляем
            kept = {k: v for k, v in (self.meta.get("speakers") or {}).items() if k == speakers.ME}
            self.save(speakers=kept, speaker_suggestions={}, names_checked=False)
        self._write_transcript_text()

    def _write_transcript_text(self) -> None:
        text = transcribe.format_transcript(self.paragraphs(), self.speaker_names())
        (self.path / "transcript.txt").write_text(text, encoding="utf-8")

    def label_transcript(self) -> str:
        """Транскрипт со стандартными метками («Я», «Спикер 1») — его и видит Claude."""
        paras = self.paragraphs()
        return transcribe.format_transcript(paras, speakers.default_names(speakers.speaker_ids(paras)))

    # имена говорящих
    def speaker_names(self) -> dict[str, str]:
        names = speakers.default_names(speakers.speaker_ids(self.paragraphs()))
        for sid, info in (self.meta.get("speakers") or {}).items():
            if info.get("name") and sid in names:
                names[sid] = info["name"]
        return names

    def speaker_is_auto(self, sid: str) -> bool:
        return bool((self.meta.get("speakers") or {}).get(sid, {}).get("auto"))

    def speaker_source(self, sid: str) -> str:
        """Откуда взялось автоматическое имя: voice — узнали по голосу, claude — по разговору."""
        return (self.meta.get("speakers") or {}).get(sid, {}).get("source", "")

    def rename_speaker(self, sid: str, name: str, auto: bool = False, source: str = "") -> None:
        """Меняет имя в транскрипте и подставляет его в готовое саммари."""
        old = self.speaker_names().get(sid)
        names = dict(self.meta.get("speakers") or {})
        if name.strip():
            names[sid] = {"name": name.strip(), "auto": auto, "source": source}
        else:
            names.pop(sid, None)
        self.save(speakers=names)
        new = self.speaker_names().get(sid)
        self._write_transcript_text()
        summary = self.summary()
        if summary and old and new and old != new:
            self.write_summary(_word_pattern(old).sub(lambda _m: new, summary))

    # исправления распознавания: «Эпиция» → «API» во всём транскрипте этого созвона
    def add_correction(self, wrong: str, right: str) -> None:
        corrections = dict(self.meta.get("corrections") or {})
        corrections[wrong] = right
        self.save(corrections=corrections)
        self.write_paragraphs(self.paragraphs())
        summary = self.summary()
        if summary:
            self.write_summary(apply_corrections(summary, {wrong: right}))

    # «отпечатки» голосов участников этого созвона — нужны, чтобы запомнить голос при переименовании
    def voice_embeddings(self) -> dict[str, list[float]]:
        return self._read_json("voices.json", {})

    def write_voice_embeddings(self, embs: dict[str, list[float]]) -> None:
        self._write_json("voices.json", embs)

    # отметки «важный момент», поставленные во время записи или прослушивания: [{"t": секунды, "note": подпись}]
    def bookmarks(self) -> list[dict]:
        items = [b for b in self.meta.get("bookmarks") or [] if isinstance(b, dict) and "t" in b]
        return sorted(items, key=lambda b: float(b["t"]))

    def add_bookmark(self, t: float, note: str = "") -> dict:
        mark = {"t": round(max(0.0, t), 1), "note": note.strip()}
        self.save(bookmarks=[*self.bookmarks(), mark])
        return mark

    def set_bookmarks(self, items: list[dict]) -> None:
        self.save(bookmarks=sorted(items, key=lambda b: float(b["t"])))

    # разобранное саммари: главы для шкалы плеера и задачи для трекеров
    def chapters(self) -> list[tuple[int, str]]:
        return sections.chapters(self.summary())

    def tasks(self) -> list[sections.Task]:
        return sections.tasks(self.summary(), self.created.date())

    def participants(self) -> list[str]:
        """Имена участников без повторов (спикеры с одинаковым именем — один человек)."""
        return list(dict.fromkeys(self.speaker_names().values()))

    # саммари
    def summary(self) -> str:
        try:
            return (self.path / "summary.md").read_text(encoding="utf-8")
        except OSError:
            return ""

    def write_summary(self, text: str) -> None:
        (self.path / "summary.md").write_text(text, encoding="utf-8")
        # название встречи берём из первого заголовка, если пользователь не переименовал её сам
        # и оно не пришло из календаря
        m = re.match(r"\s*#\s+(.+)", text)
        if m and not self.meta.get("title_manual") and self.meta.get("title_source") != "calendar":
            self.save(title=m.group(1).strip()[:120])


def new_call(source: str, title: str = "") -> Call:
    now = datetime.now()
    CALLS_DIR.mkdir(parents=True, exist_ok=True)
    base = now.strftime("%Y-%m-%d_%H-%M-%S")
    path = CALLS_DIR / base
    n = 1
    while path.exists():
        n += 1
        path = CALLS_DIR / f"{base}_{n}"
    path.mkdir()
    call = Call(path)
    call.save(
        created=now.isoformat(timespec="seconds"),
        source=source,
        title=title or f"Созвон {now:%d.%m.%Y %H:%M}",
        status="new",
    )
    return call


def cleanup_audio(calls: list[Call], days: int, busy: set[str]) -> list[Call]:
    """Удаляет аудио у созвонов старше days дней (кроме отмеченных «не удалять» и тех, что в работе)."""
    if days <= 0:
        return []
    border = datetime.now() - timedelta(days=days)
    cleaned = []
    for call in calls:
        if (call.created < border and call.tracks() and not call.meta.get("keep_audio")
                and call.id not in busy and call.status not in ("recording", "queued")):
            call.delete_audio()
            cleaned.append(call)
    return cleaned


def list_calls() -> list[Call]:
    if not CALLS_DIR.exists():
        return []
    calls = [Call(p) for p in CALLS_DIR.iterdir() if (p / "meta.json").exists()]
    return sorted(calls, key=lambda c: c.created, reverse=True)
