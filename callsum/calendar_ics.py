"""Календарь: по ссылке iCal (.ics) узнаём, какая встреча шла во время записи.

Подходит «секретный адрес в формате iCal» из Google Календаря, ссылка на публикацию
календаря Outlook, Яндекс Календаря, iCloud, Nextcloud — или просто путь к файлу .ics.
Из встречи берутся название и участники: созвон получает понятное имя, а участников
легче подписать по именам. Календарь только читается и кэшируется на 15 минут.
"""

import hashlib
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import config

CACHE_DIR = config.DATA_DIR / "calendar"
REFRESH = 15 * 60
MAX_SIZE = 50 * 2**20


@dataclass
class Event:
    title: str
    start: datetime
    end: datetime
    attendees: list[str] = field(default_factory=list)
    organizer: str = ""


def urls(cfg: dict) -> list[str]:
    out = []
    for line in (cfg.get("calendar_urls") or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append("https://" + line[len("webcal://"):] if line.lower().startswith("webcal://") else line)
    return out


def _cache_path(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".ics")


def fetch(url: str, max_age: float = REFRESH) -> bytes | None:
    """Содержимое календаря: свежая копия из кэша или скачанная заново (при ошибке — старая копия)."""
    parsed = urlparse(url)
    if parsed.scheme in ("", "file") or len(parsed.scheme) == 1:  # путь к файлу, в том числе C:\…
        path = Path(unquote(parsed.path) if parsed.scheme == "file" else url).expanduser()
        try:
            return path.read_bytes()
        except OSError:
            return None
    cache = _cache_path(url)
    try:
        if time.time() - cache.stat().st_mtime < max_age:
            return cache.read_bytes()
    except OSError:
        pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"{config.APP_ID} calendar"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read(MAX_SIZE + 1)
        if len(data) > MAX_SIZE or b"BEGIN:VCALENDAR" not in data[:4096]:
            raise ValueError("это не календарь iCal")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(cache)
        return data
    except (OSError, ValueError):
        try:
            return cache.read_bytes()
        except OSError:
            return None


def _person(addr) -> str:
    """Имя участника: CN из приглашения или начало адреса почты. Переговорки пропускаем."""
    if addr is None:
        return ""
    params = getattr(addr, "params", {}) or {}
    if str(params.get("CUTYPE", "")).upper() in ("ROOM", "RESOURCE"):
        return ""
    name = str(params.get("CN", "")).strip().strip('"')
    if name and "@" not in name:
        return name
    email = str(addr).split(":", 1)[-1]
    return email.split("@")[0] if email else ""


def _aware(d) -> datetime | None:
    if not isinstance(d, datetime):
        return None  # событие на весь день — не созвон
    return d if d.tzinfo else d.astimezone()


def events_between(cfg: dict, start: datetime, end: datetime) -> list[Event]:
    import icalendar
    import recurring_ical_events

    events = []
    for url in urls(cfg):
        data = fetch(url)
        if not data:
            continue
        try:
            cal = icalendar.Calendar.from_ical(data)
            found = recurring_ical_events.of(cal).between(start, end)
        except (ValueError, KeyError, TypeError):
            continue
        for ev in found:
            if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                continue
            s = _aware(ev.decoded("DTSTART", None))
            e = _aware(ev.decoded("DTEND", None)) if ev.get("DTEND") else None
            if s is None:
                continue
            if e is None:
                dur = ev.decoded("DURATION", None)
                e = s + (dur if isinstance(dur, timedelta) else timedelta(hours=1))
            if e - s > timedelta(hours=12):
                continue
            attendees = ev.get("ATTENDEE") or []
            if not isinstance(attendees, list):
                attendees = [attendees]
            names = [n for n in (_person(a) for a in attendees) if n]
            events.append(Event(title=str(ev.get("SUMMARY", "")).strip() or "Встреча",
                                start=s, end=e, attendees=list(dict.fromkeys(names)),
                                organizer=_person(ev.get("ORGANIZER"))))
    return events


def find_meeting(cfg: dict, started: datetime, duration: float) -> Event | None:
    """Встреча, которая больше всего пересекается с записью (или началась рядом с её началом)."""
    if not urls(cfg):
        return None
    start = _aware(started)
    end = start + timedelta(seconds=max(duration, 60))
    best, best_score = None, 0.0
    for ev in events_between(cfg, start - timedelta(hours=1), end + timedelta(minutes=15)):
        overlap = (min(end, ev.end) - max(start, ev.start)).total_seconds()
        near = abs((ev.start - start).total_seconds()) <= 15 * 60
        score = overlap if overlap > 0 else (1.0 if near else 0.0)
        if score > best_score:
            best, best_score = ev, score
    return best


def describe(ev: Event) -> dict:
    return {"title": ev.title, "start": ev.start.isoformat(timespec="minutes"),
            "end": ev.end.isoformat(timespec="minutes"), "attendees": ev.attendees, "organizer": ev.organizer}


def note(meta: dict) -> str:
    """Что известно из календаря — для модели, которая пишет саммари."""
    if not meta or not meta.get("title"):
        return ""
    when = ""
    try:
        s, e = datetime.fromisoformat(meta["start"]), datetime.fromisoformat(meta["end"])
        when = f" ({s.astimezone():%H:%M}–{e.astimezone():%H:%M})"
    except (KeyError, ValueError):
        pass
    text = f"По календарю это встреча «{meta['title']}»{when}."
    if meta.get("attendees"):
        text += " Приглашены: " + ", ".join(meta["attendees"]) + "."
    if meta.get("organizer"):
        text += f" Организатор: {meta['organizer']}."
    return text



def check(cfg: dict) -> str:
    """Проверка из настроек: скачать календари заново и рассказать, что в них нашлось."""
    sources = urls(cfg)
    if not sources:
        return "Добавьте ссылку на календарь."
    problems = []
    for url in sources:
        data = fetch(url, max_age=0)
        if not data or b"BEGIN:VCALENDAR" not in data[:4096]:
            problems.append(url if len(url) < 60 else url[:57] + "…")
    now = datetime.now().astimezone()
    events = sorted(events_between(cfg, now, now + timedelta(days=7)), key=lambda e: e.start)
    text = f"Встреч на ближайшую неделю: {len(events)}."
    if events:
        e = events[0]
        text += f" Ближайшая — «{e.title}», {e.start.astimezone():%d.%m %H:%M}."
    if problems:
        text += " Не загрузились: " + ", ".join(problems)
    return text
