"""Итоги недели: главное, решения, задачи по людям и что зависло — по саммари всех созвонов недели."""

import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config, llm, sections
from .storage import Call

DIGEST_DIR = config.DATA_DIR / "digests"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

SYSTEM = """\
Ты готовишь для человека итоги его рабочей недели по саммари созвонов. Пиши по-русски, \
коротко и конкретно, только по материалам, без воды и домыслов.

Ссылайся на созвоны в формате [[ID]] или на момент записи [[ID@ЧЧ:ММ:СС]] — ID из материалов, \
не выдумывай их."""

TASK = """\
Собери итоги недели {period} в формате Markdown:

# Итоги недели, {period}

## Главное
3–6 пунктов: что произошло важного за неделю.

## Решения
Что решили, со ссылкой на созвон.

## Задачи по людям
Сгруппируй открытые задачи по исполнителям (**Имя**, затем чек-лист «- [ ] что — срок [[ID]]»). \
Задачи без исполнителя — в группу «Не назначено». Выполненные (отмеченные [x]) не включай.

## Что зависло
Вопросы, которые обсуждались несколько раз без решения, задачи без срока или исполнителя, \
открытые вопросы, к которым так и не вернулись. Если такого нет — «—».

## Созвоны недели
По одной строке на созвон: «- [[ID]] Название — чем закончился»."""


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def period(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.day}–{sunday.day} {MONTHS[sunday.month - 1]}"
    return f"{monday.day} {MONTHS[monday.month - 1]} – {sunday.day} {MONTHS[sunday.month - 1]}"


def path(monday: date) -> Path:
    y, w, _ = monday.isocalendar()
    return DIGEST_DIR / f"{y}-W{w:02d}.md"


def calls_in_week(calls: list[Call], monday: date) -> list[Call]:
    end = monday + timedelta(days=7)
    return sorted((c for c in calls if monday <= c.created.date() < end and c.summary()), key=lambda c: c.created)


def load(monday: date) -> tuple[str, list[str]]:
    """Готовые итоги и ID созвонов, по которым они собраны (чтобы понять, что появились новые)."""
    try:
        text = path(monday).read_text(encoding="utf-8")
    except OSError:
        return "", []
    m = re.match(r"\A<!--\s*calls:\s*([^>]*?)\s*-->\n?", text)
    if not m:
        return text, []
    return text[m.end():], m.group(1).split()


def build(cfg: dict, calls: list[Call], monday: date, on_text: Callable[[str], None],
          cancelled: Callable[[], bool] = lambda: False) -> str:
    week = calls_in_week(calls, monday)
    if not week:
        raise llm.LLMError("На этой неделе нет созвонов с готовым саммари.")
    material = []
    for c in week:
        people = ", ".join(n for n in c.participants() if n != "Я")
        summary = sections.without_section(c.summary(), sections.CHAPTER_HEADINGS)
        material.append(f'<call id="{c.id}" date="{c.created:%d.%m.%Y %H:%M}" title="{c.title}"'
                        f' participants="{people}">\n{summary}\n</call>')
    text, _truncated = llm.stream(cfg, SYSTEM, [{"role": "user", "content": [
        llm.Block("\n\n".join(material)), llm.Block(TASK.format(period=period(monday)))]}],
        on_text, cancelled, max_tokens=16000)
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path(monday).write_text(f"<!-- calls: {' '.join(c.id for c in week)} -->\n{text}\n", encoding="utf-8")
    return text


def due(cfg: dict, calls: list[Call], now: datetime | None = None) -> date | None:
    """Неделя, итоги которой пора собрать по расписанию из настроек (или None)."""
    now = now or datetime.now()
    schedule = cfg.get("digest_schedule", "fri17")
    if schedule == "fri17":
        if now.weekday() < 4 or (now.weekday() == 4 and now.hour < 17):
            return None
        monday = week_start(now.date())
    elif schedule == "mon10":
        if now.weekday() == 0 and now.hour < 10:
            return None
        monday = week_start(now.date()) - timedelta(days=7)
    else:
        return None
    if path(monday).exists() or not calls_in_week(calls, monday):
        return None
    return monday
