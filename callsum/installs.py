"""Две копии программы на одном компьютере.

Linux: install.sh ставит копию в домашнюю папку (~/.local/share/call-summarizer), и её ярлык
в ~/.local/share/applications перекрывает ярлык пакета .deb — из меню запускается старая копия.
Windows: install.cmd ставит Python и библиотеки в %LOCALAPPDATA%\\CallSummarizer, а установщик —
программу в %LOCALAPPDATA%\\Programs\\CallSummarizer; старая копия занимает ~1,3 ГБ.

Программа замечает это при запуске и предлагает оставить одну, новую копию. Записи, настройки
и голоса общие — их это не касается.
"""

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__, config, osdeps

DEB_DIR = Path("/opt/call-summarizer")
USER_DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
USER_BIN = Path.home() / ".local/bin" / config.APP_ID
USER_APP_DIRS = [config.DATA_DIR / "app", config.DATA_DIR / "venv", config.DATA_DIR / "program"]
USER_DESKTOP = [USER_DATA / "applications" / f"{config.APP_ID}{suffix}.desktop" for suffix in ("", "-toggle", "-bookmark")]
WIN_SCRIPT_DIRS = [config.DATA_DIR / "venv", config.DATA_DIR / "python", config.DATA_DIR / "uv"]
WIN_SETUP_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "CallSummarizer" / "CallSummarizer.exe"


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def current_kind() -> str:
    """deb | user (install.sh) | setup (установщик Windows) | script (install.cmd) | portable | source."""
    exe = Path(sys.executable)
    if osdeps.IS_WIN:
        if osdeps.FROZEN:
            return "setup" if _under(exe, WIN_SETUP_EXE.parent) else "portable"
        return "script" if _under(exe, config.DATA_DIR / "venv") else "source"
    if osdeps.FROZEN and _under(exe, DEB_DIR):
        return "deb"
    if _under(exe, config.DATA_DIR) or _under(Path(__file__), config.DATA_DIR):
        return "user"
    return "source"


KIND_LABELS = {"deb": "пакет .deb", "user": "установка в домашнюю папку", "setup": "установщик Windows",
               "script": "установка из install.cmd", "portable": "без установки", "source": "из исходников"}


def _deb_version() -> str:
    if not (DEB_DIR / config.APP_ID).exists():
        return ""
    try:
        r = osdeps.run(["dpkg-query", "-W", "-f=${Version}", config.APP_ID], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else "?"
    except (OSError, subprocess.SubprocessError):
        return "?"


def _user_copy() -> bool:
    return any(p.exists() for p in USER_APP_DIRS) or USER_BIN.exists() or USER_DESKTOP[0].exists()


@dataclass
class Conflict:
    text: str          # что не так — для окна с вопросом
    action: str        # подпись кнопки
    restart: bool      # нужно перезапуститься в другую копию


def find() -> Conflict | None:
    kind = current_kind()
    if osdeps.IS_WIN:
        script = any(p.exists() for p in WIN_SCRIPT_DIRS)
        if kind == "setup" and script:
            return Conflict("На компьютере осталась старая установка из install.cmd (Python и библиотеки, "
                            "около 1,3 ГБ). Она больше не нужна — программа уже стоит через установщик.",
                            "Удалить старую установку", False)
        if kind == "script" and WIN_SETUP_EXE.exists():
            return Conflict("Установлена новая версия программы (через установщик), а сейчас запущена старая — "
                            f"из install.cmd, версия {__version__}. Удалить старую и перейти на новую?",
                            "Перейти на новую версию", True)
        return None
    deb = _deb_version()
    if kind == "user" and deb:
        return Conflict(f"В системе установлен пакет .deb (версия {deb}), а запущена старая копия {__version__} "
                        "из домашней папки: её ярлык в меню перекрывает пакет. Удалить старую копию и перейти "
                        "на версию из пакета?", f"Перейти на версию {deb}", True)
    if kind == "deb" and _user_copy():
        return Conflict("В домашней папке осталась старая копия программы (её ставил install.sh). Её ярлык "
                        "в меню перекрывает пакет, поэтому из меню запускается она. Удалить её?",
                        "Удалить старую копию", False)
    return None


def _remove_linux_shortcuts() -> None:
    for f in [USER_BIN, *USER_DESKTOP]:
        f.unlink(missing_ok=True)
    icons = USER_DATA / "icons" / "hicolor"
    for f in [*icons.glob(f"*/apps/{config.ICON_NAME}.*"), *icons.glob(f"scalable/status/{config.APP_ID}-*.svg")]:
        f.unlink(missing_ok=True)
    for tool in (["kbuildsycoca6"], ["update-desktop-database", "-q", str(USER_DATA / "applications")]):
        if shutil.which(tool[0]):
            osdeps.popen(tool, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _fix_windows_autostart() -> None:
    """Автозапуск указывал на Python старой установки — переводим его на установленную программу."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, config._RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            value = winreg.QueryValueEx(key, "CallSummarizer")[0]
            if str(config.DATA_DIR / "venv").lower() in str(value).lower():
                winreg.SetValueEx(key, "CallSummarizer", 0, winreg.REG_SZ, f'"{WIN_SETUP_EXE}" --hidden')
    except OSError:
        pass


def resolve(conflict: Conflict) -> None:
    """Оставить одну копию. Если удаляем ту, что запущена, — удалим её папки после выхода
    и сразу запустим новую (программа после этого должна закрыться)."""
    if osdeps.IS_WIN:
        _fix_windows_autostart()
        dirs = [str(p) for p in WIN_SCRIPT_DIRS if p.exists()]
        if conflict.restart:
            cmd = "timeout /t 3 /nobreak >nul & " + " & ".join(f'rmdir /s /q "{d}"' for d in dirs) + \
                  f' & start "" "{WIN_SETUP_EXE}"'
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: без окна и не закроется вместе с нами
            subprocess.Popen(["cmd", "/c", cmd], creationflags=0x00000008 | 0x00000200, close_fds=True)
        else:
            for d in dirs:
                shutil.rmtree(d, ignore_errors=True)
            (config.DATA_DIR / "call-summarizer.ico").unlink(missing_ok=True)
        return
    _remove_linux_shortcuts()
    dirs = [str(p) for p in USER_APP_DIRS if p.exists()]
    # автозапуск указывал на старую команду ~/.local/bin/call-summarizer — переписываем на пакет
    if config.AUTOSTART_FILE.exists():
        text = config.AUTOSTART_FILE.read_text(encoding="utf-8")
        config.AUTOSTART_FILE.write_text(text.replace(f"Exec={USER_BIN} ", f"Exec={config.APP_ID} "), encoding="utf-8")
    if conflict.restart:
        script = f"sleep 2; rm -rf {' '.join(map(shlex.quote, dirs))}; exec /usr/bin/{config.APP_ID}"
        subprocess.Popen(["sh", "-c", script], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
