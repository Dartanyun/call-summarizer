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


# ---------------- прокси ----------------

PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy")
# как было до программы: пустой прокси в настройках возвращает системный
_proxy_env_before = {k: os.environ.get(k) for k in _PROXY_VARS}
_LOCAL_HOSTS = "localhost,127.0.0.1,::1"  # Ollama и прочее на этом компьютере — всегда напрямую


def normalize_proxy(text: str) -> str:
    """«127.0.0.1:10809» → «http://127.0.0.1:10809». Пустая строка — без прокси.
    Неправильный адрес — ValueError с понятным текстом."""
    from urllib.parse import urlsplit

    text = (text or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as e:
        raise ValueError(f"Не понял адрес прокси «{text}»: {e}") from e
    if parts.scheme.lower() not in PROXY_SCHEMES:
        raise ValueError(f"Прокси должен начинаться с {', '.join(s + '://' for s in PROXY_SCHEMES)}")
    if not parts.hostname or not port:
        raise ValueError("Укажите адрес и порт прокси, например http://127.0.0.1:10809")
    return text


def proxy_label(url: str) -> str:
    """Адрес прокси для показа — без пароля."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.password is None:
        return url
    return parts._replace(netloc=f"{parts.username}:***@{parts.netloc.rsplit('@', 1)[1]}").geturl()


def apply_proxy(text: str) -> str:
    """Прокси из настроек — для всех запросов программы в интернет: Claude API, загрузка моделей,
    обновления, календари, трекеры. Ставим его в переменные окружения процесса: их читают и
    клиент Anthropic, и загрузчик моделей Whisper, и urllib. HTTP-прокси работает везде;
    SOCKS понимают только Claude API и загрузка Whisper (urllib его не умеет) — остальное идёт
    напрямую. Возвращает применённый адрес ("" — без прокси); неправильный адрес — ValueError."""
    import urllib.request

    url = normalize_proxy(text)
    for key, value in _proxy_env_before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    if url:
        http_keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        if url.lower().startswith("socks"):
            for key in http_keys:  # иначе системный HTTP-прокси перебил бы наш: он точнее ALL_PROXY
                os.environ.pop(key, None)
            keys: tuple[str, ...] = ("ALL_PROXY", "all_proxy")
        else:
            keys = http_keys
        for key in keys:
            os.environ[key] = url
        no = [h for h in (_proxy_env_before.get("NO_PROXY") or "").split(",") if h.strip()]
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join([*no, _LOCAL_HOSTS])
    # urllib запоминает прокси при первом запросе — пусть при следующем прочитает заново
    urllib.request.install_opener(None)
    return url


def check_proxy(text: str, url: str = "https://api.anthropic.com/v1/models", timeout: float = 15) -> str:
    """Доходит ли запрос через этот прокси (или напрямую, если он пустой) до Claude API."""
    import httpx2

    proxy = normalize_proxy(text)
    try:
        with httpx2.Client(proxy=proxy or None, trust_env=not proxy, timeout=timeout) as client:
            r = client.get(url)
    except ImportError as e:  # SOCKS без пакета socksio
        raise RuntimeError(f"для SOCKS-прокси не хватает пакета socksio ({e})") from e
    except httpx2.TimeoutException as e:
        raise RuntimeError(f"нет ответа за {timeout:.0f} с — прокси не отвечает или не пускает к сайту") from e
    except httpx2.HTTPError as e:
        text = str(e) or e.__class__.__name__
        if "407" in text or "NO ACCEPTABLE METHODS" in text.upper():
            raise RuntimeError("прокси требует логин и пароль — укажите их в адресе: "
                               "http://логин:пароль@адрес:порт") from e
        raise RuntimeError(text) from e
    how = f"через прокси {proxy_label(proxy)}" if proxy else "напрямую"
    return f"{r.url.host} отвечает {how} (код {r.status_code})"


def user_id() -> str:
    return str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
