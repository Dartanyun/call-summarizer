"""Горячая клавиша Meta+Shift+Z в KDE.

KDE находит сочетание в скрытом ярлыке call-summarizer-toggle.desktop (X-KDE-Shortcuts), но само
его не включает. install.sh включает его сразу после установки; здесь то же самое делается один раз
при первом запуске — для установки из пакета .deb, где установщик не может обратиться к сеансу
пользователя. Сочетание назначается, только если оно свободно и вы не назначили своё.
"""

import shutil
import subprocess
import threading
import time

from . import config, osdeps

COMPONENT = f"{config.APP_ID}-toggle.desktop"
OBJECT = "/component/" + COMPONENT.replace("-", "_").replace(".", "_")
ACTION = "Саммари созвонов: начать или остановить запись"
KEY = 301989978  # Meta+Shift+Z
DONE = config.CONFIG_DIR / "kde-hotkey.done"


def _qdbus(*args: str) -> str:
    r = osdeps.run(["qdbus6", *args], capture_output=True, text=True, timeout=5)
    return r.stdout if r.returncode == 0 else ""


def ensure() -> bool:
    """True — больше пробовать не нужно (назначено, или сочетание уже настроено, или занято)."""
    if not (shutil.which("qdbus6") and shutil.which("dbus-send")):
        return True
    if not _qdbus("org.kde.kglobalaccel"):
        return True  # не KDE
    for _ in range(10):  # KDE замечает новый ярлык не сразу
        if OBJECT in _qdbus("org.kde.kglobalaccel").split():
            break
        time.sleep(1)
    else:
        return False  # ярлык ещё не виден — попробуем при следующем запуске
    info = _qdbus("--literal", "org.kde.kglobalaccel", OBJECT, "org.kde.kglobalaccel.Component.allShortcutInfos")
    if "[Argument: ai {0}], [Argument: ai" not in info:
        return True  # у действия уже есть своё сочетание
    free = _qdbus("org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel.isGlobalShortcutAvailable",
                  str(KEY), COMPONENT)
    if free.strip() == "true":
        osdeps.run(["dbus-send", "--session", "--dest=org.kde.kglobalaccel", "/kglobalaccel",
                    "org.kde.KGlobalAccel.setForeignShortcut",
                    f"array:string:{COMPONENT},_launch,{ACTION},{ACTION}", f"array:int32:{KEY}"],
                   capture_output=True, timeout=5)
    return True


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
