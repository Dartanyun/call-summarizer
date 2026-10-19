"""Разбор готового саммари: главы («Хронология») и задачи — для шкалы плеера, трекеров и экспорта."""

import re
from dataclasses import dataclass
from datetime import date

_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
TIMECODE = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\](?!\()")
CHAPTER_HEADINGS = ("хронология", "главы", "оглавление", "chapters", "timeline")
TASK_HEADINGS = ("задачи", "следующие шаги", "action items", "tasks", "что сделать")


def seconds(tc: str) -> int:
    parts = [int(x) for x in tc.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def section(md: str, names: tuple[str, ...]) -> str:
    """Текст раздела, заголовок которого начинается с одного из names (без учёта регистра)."""
    out: list[str] = []
    inside = False
    for line in md.splitlines():
        m = _HEADING.match(line)
        if m:
            title = m.group(2).strip(" *_:").lower()
            inside = any(title.startswith(n) for n in names)
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def without_section(md: str, names: tuple[str, ...]) -> str:
    """Саммари без раздела (например, без «Хронологии» — для сообщения в мессенджер)."""
    out: list[str] = []
    skip = False
    for line in md.splitlines():
        m = _HEADING.match(line)
        if m:
            skip = any(m.group(2).strip(" *_:").lower().startswith(n) for n in names)
        if not skip:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def _clean(text: str) -> str:
    return re.sub(r"\*\*|__|`", "", text).strip(" -—*_:\t")


def chapters(md: str) -> list[tuple[int, str]]:
    """[(секунда начала, тема)] из раздела «Хронология», по порядку."""
    result = []
    for line in section(md, CHAPTER_HEADINGS).splitlines():
        m = TIMECODE.search(line)
        if not m:
            continue
        title = _clean(TIMECODE.sub("", line))
        if title:
            result.append((seconds(m.group(1)), title[:80]))
    return sorted(result)


@dataclass
class Task:
    text: str               # строка задачи целиком, без чек-бокса и таймкода
    who: str = ""
    what: str = ""
    due: str = ""           # срок как написан
    due_date: date | None = None
    at: int | None = None   # таймкод момента в записи, секунды
    done: bool = False

    @property
    def assignee(self) -> str:
        return "" if self.who.lower() in ("не указан", "не указана", "—", "-") else self.who

    @property
    def deadline(self) -> str:
        return "" if self.due.lower().startswith("не указ") or self.due in ("—", "-") else self.due


_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[( |x|X)\]\s*)?(.+)$")
_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?")


def parse_due(due: str, today: date) -> date | None:
    m = _DATE.search(due)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year = int(m.group(3)) if m.group(3) else today.year
    if year < 100:
        year += 2000
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if not m.group(3) and d < today:  # «05.01» в декабре — это январь следующего года
        try:
            d = date(year + 1, month, day)
        except ValueError:
            return None
    return d


def tasks(md: str, call_date: date) -> list[Task]:
    """Задачи из раздела «Задачи»: «- [ ] Кто — что сделать — срок [00:12:34]»."""
    result = []
    for line in section(md, TASK_HEADINGS).splitlines():
        m = _ITEM.match(line)
        if not m:
            continue
        body = m.group(2)
        tc = TIMECODE.search(body)
        text = _clean(TIMECODE.sub("", body))
        if not text or text == "—":
            continue
        task = Task(text=text, done=(m.group(1) or "").lower() == "x",
                    at=seconds(tc.group(1)) if tc else None)
        parts = [_clean(p) for p in re.split(r"\s+[—–]\s+", text)]
        if len(parts) >= 3:
            task.who, task.what, task.due = parts[0], " — ".join(parts[1:-1]), parts[-1]
        elif len(parts) == 2:
            task.who, task.what = parts
        else:
            task.what = text
        task.due_date = parse_due(task.deadline, call_date) if task.deadline else None
        result.append(task)
    return result
