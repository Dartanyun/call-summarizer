"""Различия между Linux и Windows: запуск процессов, ffmpeg, имя пользователя."""

import getpass
import os
import shutil
import subprocess
import sys

IS_WIN = sys.platform == "win32"
# готовая сборка (PyInstaller): программа — один исполняемый файл, а не python -m callsum
FROZEN = bool(getattr(sys, "frozen", False))
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


def app_command() -> list[str]:
    """Как запустить эту же программу: для автозапуска и ярлыков."""
    if FROZEN:
        return [sys.executable]
    exe = sys.executable
    if IS_WIN and exe.lower().endswith("python.exe"):
        exe = exe[: -len("python.exe")] + "pythonw.exe"  # без чёрного окна консоли
    return [exe, "-m", "callsum"]


def clean_child_env() -> None:
    """Готовая сборка для Linux ищет свои библиотеки через LD_LIBRARY_PATH. Дочерним программам
    (системные ffmpeg, pactl, gdbus, qdbus6) он не нужен: с ним они подхватили бы библиотеки
    сборки вместо своих. Сама программа свои уже нашла — загрузчик читает переменную один раз."""
    if not FROZEN or IS_WIN:
        return
    original = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        os.environ["LD_LIBRARY_PATH"] = original
        return
    bundle = getattr(sys, "_MEIPASS", "")
    parts = [p for p in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if p and p != bundle]
    if parts:
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(parts)
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


# где лежат корневые сертификаты в разных дистрибутивах Linux
_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/ssl/cert.pem",  # Alpine
)


def fix_ssl_certs() -> None:
    """Готовая сборка для Linux несёт свой OpenSSL, а он ищет сертификаты там, где они лежали
    на машине сборки (Ubuntu). Если там пусто, показываем ему системный набор этого дистрибутива."""
    if not FROZEN or IS_WIN or "SSL_CERT_FILE" in os.environ:
        return
    import ssl

    paths = ssl.get_default_verify_paths()
    if (paths.cafile and os.path.isfile(paths.cafile)) or (paths.capath and os.path.isdir(paths.capath)):
        return
    for bundle in _CA_BUNDLES:
        if os.path.isfile(bundle):
            os.environ["SSL_CERT_FILE"] = bundle
            return


def user_id() -> str:
    return str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
