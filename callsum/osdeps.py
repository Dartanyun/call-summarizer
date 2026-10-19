"""Различия между Linux и Windows: запуск процессов, ffmpeg, имя пользователя."""

import getpass
import os
import shutil
import subprocess
import sys

IS_WIN = sys.platform == "win32"
# В Windows программа запущена без консоли (pythonw), и каждый дочерний процесс
# открыл бы своё чёрное окно — запрещаем это.
_NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

_ffmpeg: str | None = None


def _prepare(kw: dict) -> dict:
    if IS_WIN:
        kw.setdefault("creationflags", _NO_WINDOW)
    if kw.get("text") or kw.get("universal_newlines"):
        # вывод ffmpeg и pactl — UTF-8, а не кодировка консоли Windows
        kw.setdefault("encoding", "utf-8")
        kw.setdefault("errors", "replace")
    return kw


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **_prepare(kw))


def popen(cmd: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen(cmd, **_prepare(kw))


def ffmpeg() -> str:
    """Путь к ffmpeg: в Linux — системный, в Windows — из пакета imageio-ffmpeg."""
    global _ffmpeg
    if _ffmpeg is None:
        found = None
        if IS_WIN:
            try:
                import imageio_ffmpeg

                found = imageio_ffmpeg.get_ffmpeg_exe()
            except (ImportError, RuntimeError):
                found = None
        _ffmpeg = found or shutil.which("ffmpeg") or "ffmpeg"
    return _ffmpeg


def user_id() -> str:
    return str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
