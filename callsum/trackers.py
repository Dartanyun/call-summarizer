"""Задачи из саммари — в трекер: YouTrack, Todoist или напоминания в календарь (.ics).

Ключи доступа (токены) вводит пользователь в «Настройках» → «Интеграции»; они хранятся
в файле настроек рядом с ключом Claude.
"""

import json
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from .sections import Task
from .storage import Call

TODOIST_API = "https://api.todoist.com/api/v1"


class TrackerError(RuntimeError):
    pass


def _request(method: str, url: str, token: str, body: dict | None = None, timeout: int = 20):
    req = urllib.request.Request(
        url, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "Content-Type": "application/json", "User-Agent": "call-summarizer"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            data = json.loads(e.read() or b"{}")
            detail = data.get("error_description") or data.get("error") or data.get("message") or ""
        except ValueError:
            pass
        if e.code in (401, 403):
            raise TrackerError("Токен не подошёл или у него нет прав. Проверьте его в «Настройках» → «Интеграции».") from e
        raise TrackerError(f"Ошибка {e.code}: {detail or e.reason}") from e
    except (urllib.error.URLError, OSError) as e:
        raise TrackerError(f"Нет соединения: {getattr(e, 'reason', e)}") from e
    except ValueError as e:
        raise TrackerError("Сервер ответил непонятно — проверьте адрес.") from e


def description(task: Task, call: Call) -> str:
    lines = [f"Из созвона «{call.title}» ({call.created:%d.%m.%Y %H:%M})."]
    if task.assignee:
        lines.append(f"Исполнитель: {task.assignee}")
    if task.deadline:
        lines.append(f"Срок: {task.deadline}")
    if task.at is not None:
        s = task.at
        lines.append(f"Момент в записи: {s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}")
    return "\n".join(lines)


def title(task: Task) -> str:
    what = task.what or task.text
    who = task.assignee if task.assignee.lower() not in ("я", "мне") else ""
    return (f"{what[:1].upper()}{what[1:]}" + (f" ({who})" if who else ""))[:250]


# ---------------- YouTrack ----------------

def youtrack_ready(cfg: dict) -> bool:
    return all((cfg.get(k) or "").strip() for k in ("youtrack_url", "youtrack_token", "youtrack_project"))


def _youtrack_base(cfg: dict) -> str:
    base = cfg["youtrack_url"].strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    return base


def youtrack_project_id(cfg: dict) -> str:
    """Внутренний id проекта по его короткому имени (например, «APP»)."""
    want = cfg["youtrack_project"].strip()
    projects = _request("GET", f"{_youtrack_base(cfg)}/api/admin/projects?fields=id,shortName,name&$top=500",
                        cfg["youtrack_token"].strip()) or []
    for p in projects:
        if want.lower() in (str(p.get("shortName", "")).lower(), str(p.get("name", "")).lower()):
            return p["id"]
    raise TrackerError(f"В YouTrack нет проекта «{want}». Укажите его короткое имя, например APP.")


def send_youtrack(cfg: dict, call: Call, items: list[Task]) -> list[str]:
    """Создаёт задачи; возвращает их номера (APP-123)."""
    pid = youtrack_project_id(cfg)
    created = []
    for task in items:
        issue = _request("POST", f"{_youtrack_base(cfg)}/api/issues?fields=idReadable", cfg["youtrack_token"].strip(),
                         {"project": {"id": pid}, "summary": title(task), "description": description(task, call)})
        created.append((issue or {}).get("idReadable", "?"))
    return created


# ---------------- Todoist ----------------

def todoist_ready(cfg: dict) -> bool:
    return bool((cfg.get("todoist_token") or "").strip())


def send_todoist(cfg: dict, call: Call, items: list[Task]) -> int:
    for task in items:
        body = {"content": title(task), "description": description(task, call)}
        if task.due_date:
            body["due_date"] = task.due_date.isoformat()
        elif task.deadline:
            body["due_string"], body["due_lang"] = task.deadline, "ru"
        _request("POST", f"{TODOIST_API}/tasks", cfg["todoist_token"].strip(), body)
    return len(items)


# ---------------- напоминания в календарь ----------------

def _ics_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Строки iCal длиннее 75 байт переносятся (RFC 5545)."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > 74:
            out.append(cur.decode())
            cur = b" "
        cur += b
    out.append(cur.decode())
    return "\r\n".join(out)


def reminders_ics(call: Call, items: list[Task]) -> str:
    """Событие на день срока (в 10:00) для каждой задачи; без срока — на следующий рабочий день."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//call-summarizer//RU", "CALSCALE:GREGORIAN"]
    for task in items:
        day = task.due_date or (call.created.date() + timedelta(days=1))
        while not task.due_date and day.weekday() >= 5:
            day += timedelta(days=1)
        start = datetime(day.year, day.month, day.day, 10, 0)
        lines += [
            "BEGIN:VEVENT", f"UID:{uuid.uuid4()}@call-summarizer", f"DTSTAMP:{stamp}",
            f"DTSTART:{start:%Y%m%dT%H%M%S}", f"DTEND:{start + timedelta(minutes=30):%Y%m%dT%H%M%S}",
            f"SUMMARY:{_ics_text(title(task))}", f"DESCRIPTION:{_ics_text(description(task, call))}",
            "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_text(title(task))}", "TRIGGER:-PT15M", "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
