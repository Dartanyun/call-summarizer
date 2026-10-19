"""Настройки приложения и пути к данным."""

import json
import os
import re
import sys
from pathlib import Path

APP_ID = "call-summarizer"
APP_NAME = "Саммари созвонов"
# имя иконки в теме; при смене рисунка меняем и имя — Plasma кэширует иконки по имени
ICON_NAME = "call-summarizer-wave"

IS_WIN = sys.platform == "win32"
if IS_WIN:
    # настройки — в перемещаемом профиле, записи и модели — в локальном (их бывает много)
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "CallSummarizer"
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CallSummarizer"
else:
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_ID
    DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP_ID
CALLS_DIR = DATA_DIR / "calls"
CONFIG_FILE = CONFIG_DIR / "config.json"

CLAUDE_MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5 — лучшее качество"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5 — быстрее и дешевле"),
    ("claude-haiku-5-5", "Claude Haiku 5.5 — самый дешёвый"),
]

EFFORTS = [
    ("low", "Низкая"),
    ("medium", "Средняя"),
    ("high", "Высокая"),
]

WHISPER_MODELS = [
    ("small", "small — быстро, качество среднее (~0.5 ГБ)"),
    ("medium", "medium — баланс (~1.5 ГБ)"),
    ("large-v3-turbo", "large-v3-turbo — точно и довольно быстро (~1.6 ГБ)"),
    ("large-v3", "large-v3 — максимальная точность, медленно (~3 ГБ)"),
]

THEMES = [
    ("system", "Как в системе"),
    ("dark", "Тёмное"),
    ("light", "Светлое"),
]

AUTO_RECORD = [
    ("off", "Выключена"),
    ("ask", "Предлагать записать"),
    ("auto", "Записывать сразу"),
]

NOTIFICATIONS = [
    ("all", "Все"),
    ("summary", "Только «саммари готово» и ошибки"),
    ("none", "Не показывать — только значок в трее"),
]

RETENTION = [(0, "Хранить всегда"), (7, "7 дней"), (14, "14 дней"), (30, "30 дней"),
             (90, "90 дней"), (180, "180 дней")]

AUTOSTART_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / f"{APP_ID}.desktop"


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _set_autostart_windows(enabled: bool) -> None:
    """Windows: значение в разделе автозапуска текущего пользователя (HKCU, Run) — без прав администратора."""
    import winreg

    # на «свежем» профиле раздела Run может ещё не быть — создаём его
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            from subprocess import list2cmdline

            from .osdeps import app_command

            winreg.SetValueEx(key, "CallSummarizer", 0, winreg.REG_SZ, list2cmdline([*app_command(), "--hidden"]))
        else:
            try:
                winreg.DeleteValue(key, "CallSummarizer")
            except FileNotFoundError:
                pass


def set_autostart(enabled: bool) -> None:
    """Запуск при входе в систему: ярлык в ~/.config/autostart (KDE, GNOME) или реестр (Windows)."""
    if IS_WIN:
        _set_autostart_windows(enabled)
        return
    if not enabled:
        AUTOSTART_FILE.unlink(missing_ok=True)
        return
    import shlex
    import shutil

    from .osdeps import app_command

    launcher = shutil.which(APP_ID) or str(Path.home() / ".local/bin" / APP_ID)
    if not Path(launcher).exists():
        launcher = shlex.join(app_command())
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={APP_NAME}\nExec={launcher} --hidden\nIcon={ICON_NAME}\n"
        "X-GNOME-Autostart-enabled=true\nNoDisplay=false\n",
        encoding="utf-8",
    )


LANGUAGES = [
    ("ru", "Русский"),
    ("en", "Английский"),
    ("", "Определять автоматически"),
]

DEFAULT_PROMPT = """\
Сделай саммари этого созвона на русском языке в формате Markdown.

Первой строкой напиши заголовок первого уровня с коротким названием встречи (3–7 слов), например «# Планирование релиза 2.4».

Дальше разделы:

## Кратко
2–4 предложения: о чём был созвон и чем он закончился.

## Ключевые темы
Маркированный список обсуждённых тем с главными тезисами по каждой.

## Решения
Что решили. Если решений не было, так и напиши.

## Задачи
Чек-лист в формате «- [ ] Кто — что сделать — срок». Если исполнитель или срок не прозвучали, пиши «не указан».

## Открытые вопросы
Что осталось нерешённым или требует уточнения.

Пиши только то, что действительно прозвучало в разговоре, ничего не додумывай. Если раздел пустой, напиши «—».\
"""

DEFAULTS = {
    "api_key": "",
    "claude_model": "claude-opus-5-5",
    "effort": "medium",
    "whisper_model": "large-v3-turbo",
    "language": "ru",
    "mic_source": "",       # "" — микрофон по умолчанию
    "system_source": "",    # "" — монитор устройства вывода по умолчанию
    "record_mic": True,
    "record_system": True,
    "diarize": True,        # определять говорящих по голосу
    "auto_process": True,
    "theme": "system",
    "vocabulary": "",       # термины и имена, по одному на строку
    "remember_voices": True,
    # тихий режим
    "tray_on_close": True,  # закрытие окна сворачивает в трей
    "autostart": False,     # запуск вместе с системой (свёрнутым)
    "auto_record": "off",   # off | ask | auto — автозапись звонков
    "call_apps": "",        # "" — список по умолчанию (callwatch.DEFAULT_APPS)
    "notifications": "all",  # all | summary | none
    # хранение
    "audio_retention_days": 0,  # 0 — хранить аудио всегда
    "summary_prompt": DEFAULT_PROMPT,
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    # В файле лежит API-ключ, поэтому он доступен только владельцу.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


def vocabulary_terms(cfg: dict) -> list[str]:
    seen, out = set(), []
    for part in re.split(r"[\n,;]+", cfg.get("vocabulary") or ""):
        term = part.strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


def add_vocabulary_term(cfg: dict, term: str) -> bool:
    if term.lower() in {t.lower() for t in vocabulary_terms(cfg)}:
        return False
    cfg["vocabulary"] = ((cfg.get("vocabulary") or "").rstrip() + "\n" + term).strip()
    return True


def api_key(cfg: dict) -> str:
    return cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
