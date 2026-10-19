"""Горячие клавиши в KDE: Meta+Shift+Z — запись, Meta+Shift+B — отметка «важный момент».

KDE находит сочетания в скрытых ярлыках call-summarizer-toggle.desktop и
call-summarizer-bookmark.desktop (X-KDE-Shortcuts), но сам их не включает. install.sh включает его сразу после установки; здесь то же самое делается один раз
при первом запуске — для установки из пакета .deb, где установщик не может обратиться к сеансу
пользователя. Сочетание назначается, только если оно свободно и вы не назначили своё.
"""

import shutil
import subprocess
import threading
import time

from . import config, osdeps

# ярлык, его название и код сочетания в KDE (Meta=0x10000000, Shift=0x02000000 + код клавиши)
SHORTCUTS = [
    (f"{config.APP_ID}-toggle.desktop", "Саммари созвонов: начать или остановить запись", 0x1200005A),  # Meta+Shift+Z
    (f"{config.APP_ID}-bookmark.desktop", "Саммари созвонов: отметить важный момент", 0x12000042),  # Meta+Shift+B
]
DONE = config.CONFIG_DIR / "kde-hotkeys-2.done"


def _qdbus(*args: str) -> str:
    r = osdeps.run(["qdbus6", *args], capture_output=True, text=True, timeout=5)
    return r.stdout if r.returncode == 0 else ""


def _ensure_one(component: str, action: str, key: int) -> bool:
    obj = "/component/" + component.replace("-", "_").replace(".", "_")
    for _ in range(10):  # KDE замечает новый ярлык не сразу
        if obj in _qdbus("org.kde.kglobalaccel").split():
            break
        time.sleep(1)
    else:
        return False  # ярлык ещё не виден — попробуем при следующем запуске
    info = _qdbus("--literal", "org.kde.kglobalaccel", obj, "org.kde.kglobalaccel.Component.allShortcutInfos")
    if "[Argument: ai {0}], [Argument: ai" not in info:
        return True  # у действия уже есть своё сочетание
    free = _qdbus("org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel.isGlobalShortcutAvailable",
                  str(key), component)
    if free.strip() == "true":
        osdeps.run(["dbus-send", "--session", "--dest=org.kde.kglobalaccel", "/kglobalaccel",
                    "org.kde.KGlobalAccel.setForeignShortcut",
                    f"array:string:{component},_launch,{action},{action}", f"array:int32:{key}"],
                   capture_output=True, timeout=5)
    return True


def ensure() -> bool:
    """True — больше пробовать не нужно (назначено, или сочетания уже настроены, или заняты)."""
    if not (shutil.which("qdbus6") and shutil.which("dbus-send")):
        return True
    if not _qdbus("org.kde.kglobalaccel"):
        return True  # не KDE
    return all([_ensure_one(*s) for s in SHORTCUTS])


def ensure_once() -> None:
    """Проверить в фоне при первом запуске; окно программы при этом не ждёт."""
    if DONE.exists():
        return

    def work():
        try:
            if ensure():
                DONE.parent.mkdir(parents=True, exist_ok=True)
                DONE.touch()
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    threading.Thread(target=work, name="kde-hotkey", daemon=True).start()
