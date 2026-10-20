"""Обновления: новая версия на GitHub и её установка — там, где программа может поставить её сама.

| Как установлена                | Что делаем                                                   |
|--------------------------------|--------------------------------------------------------------|
| установщик Windows             | скачиваем новый установщик и запускаем его тихо (без админа)   |
| пакет .deb                     | скачиваем пакет, ставим через pkexec — пароль спросит система  |
| архив .tar.gz или install.sh   | скачиваем архив и запускаем его install.sh (без пароля)        |
| без установки, из исходников   | только сообщаем и открываем страницу релиза                    |

Если репозиторий закрыть, GitHub будет отдавать релизы только с токеном (настройки → «Обновления»).
"""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, config, installs, osdeps

REPO = "Dartanyun/call-summarizer"
API = f"https://api.github.com/repos/{REPO}"
PAGE = f"https://github.com/{REPO}/releases/latest"
CACHE = config.DATA_DIR / "updates"
CHECK_EVERY = 24 * 3600


class UpdateError(RuntimeError):
    pass


@dataclass
class Asset:
    name: str
    url: str          # адрес в API: с заголовком Accept: application/octet-stream отдаёт сам файл
    size: int
    sha256: str = ""


@dataclass
class Release:
    version: str
    notes: str
    page: str
    assets: dict[str, Asset] = field(default_factory=dict)


def as_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split(".")[:3])
    except ValueError:
        return (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return as_tuple(candidate) > as_tuple(current)


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub отдаёт файл релиза перенаправлением на хранилище с подписанной ссылкой —
    туда токен посылать нельзя (и не нужно)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).netloc != urlparse(req.full_url).netloc:
            new.remove_header("Authorization")
        return new




def _request(url: str, cfg: dict, accept: str = "application/vnd.github+json"):
    headers = {"Accept": accept, "User-Agent": f"{config.APP_ID}/{__version__}", "X-GitHub-Api-Version": "2022-11-28"}
    token = (cfg.get("github_token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        # открывалка каждый раз новая: так она читает текущий прокси из настроек (osdeps.apply_proxy)
        opener = urllib.request.build_opener(_DropAuthOnRedirect)
        return opener.open(urllib.request.Request(url, headers=headers), timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 404 and not token:
            raise UpdateError("GitHub не нашёл релизы программы. Если репозиторий закрыт, укажите токен "
                              "в «Настройках» → «Общие» → «Обновления».") from e
        if e.code == 404:
            raise UpdateError("Токен GitHub не даёт доступа к репозиторию программы (или релизов пока нет).") from e
        if e.code == 401:
            raise UpdateError("Токен GitHub не подошёл — проверьте его в настройках.") from e
        if e.code in (403, 429):
            raise UpdateError("GitHub временно ограничил запросы. Попробуйте позже.") from e
        raise UpdateError(f"GitHub ответил ошибкой {e.code}.") from e
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"Нет соединения с GitHub: {getattr(e, 'reason', e)}") from e


def latest(cfg: dict, api: str = API) -> Release:
    with _request(f"{api}/releases/latest", cfg) as r:
        try:
            data = json.load(r)
        except ValueError as e:
            raise UpdateError("GitHub ответил непонятно.") from e
    assets = {}
    for a in data.get("assets") or []:
        digest = str(a.get("digest") or "")
        assets[a["name"]] = Asset(a["name"], a["url"], int(a.get("size") or 0),
                                  digest.split(":", 1)[1] if digest.startswith("sha256:") else "")
    return Release(str(data.get("tag_name", "")).lstrip("v"), data.get("body") or "", data.get("html_url") or PAGE, assets)


def asset_name(kind: str, version: str) -> str | None:
    """Какой файл релиза подходит этой установке (None — поставить самим не получится)."""
    if kind == "setup":
        return f"CallSummarizer-{version}-windows-x64-setup.exe"
    if kind == "deb":
        return f"{config.APP_ID}_{version}_amd64.deb" if shutil.which("pkexec") else None
    if kind == "user":
        return f"{config.APP_ID}-{version}-linux-x86_64.tar.gz"
    return None


def why_manual(kind: str) -> str:
    return {"portable": "Версия без установки обновляется вручную: скачайте новый архив.",
            "script": "Установка из install.cmd обновляется вручную. Удобнее перейти на установщик — "
                      "он обновляется сам.",
            "source": "Программа запущена из исходников — обновите их (git pull).",
            "deb": "Чтобы ставить пакет, нужна программа pkexec (пакет pkexec или policykit-1)."}.get(kind, "")


def download(cfg: dict, asset: Asset, on_progress: Callable[[float], None],
             cancelled: Callable[[], bool] = lambda: False) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    for old in CACHE.iterdir():  # прошлые обновления больше не нужны
        if old.name != asset.name:
            shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink(missing_ok=True)
    dst = CACHE / asset.name
    part = dst.with_name(dst.name + ".part")
    h = hashlib.sha256()
    done = 0
    with _request(asset.url, cfg, accept="application/octet-stream") as r, open(part, "wb") as f:
        while chunk := r.read(1 << 17):
            if cancelled():
                raise UpdateError("Отменено.")
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if asset.size:
                on_progress(done / asset.size)
    if asset.size and done != asset.size:
        raise UpdateError("Файл скачался не полностью — попробуйте ещё раз.")
    if asset.sha256 and h.hexdigest() != asset.sha256:
        part.unlink(missing_ok=True)
        raise UpdateError("Контрольная сумма скачанного файла не совпала — файл повреждён.")
    part.replace(dst)
    return dst


def install(kind: str, path: Path, on_status: Callable[[str], None]) -> str | None:
    """Ставит скачанное. Возвращает команду запуска новой версии (Linux) или None, если новая
    версия запустится сама (установщик Windows)."""
    if kind == "setup":
        # тихая установка; программа закроется сейчас, установщик запустит новую версию сам
        subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                         creationflags=0x00000008 | 0x00000200, close_fds=True)
        return None
    if kind == "deb":
        on_status("Установка пакета — система спросит пароль…")
        r = osdeps.run(["pkexec", "apt-get", "install", "-y", "--no-install-recommends", str(path)],
                       capture_output=True, text=True)
        if r.returncode in (126, 127):
            raise UpdateError("Установка отменена: пароль не введён.")
        if r.returncode:
            raise UpdateError("Пакет не установился:\n" + (r.stderr or r.stdout).strip()[-800:])
        return f"/usr/bin/{config.APP_ID}"
    if kind == "user":
        on_status("Распаковка…")
        target = CACHE / "unpacked"
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        with tarfile.open(path) as tar:
            try:
                tar.extractall(target, filter="data")  # без путей наружу и чужих прав
            except TypeError:  # Python до 3.10.12 не знает filter
                tar.extractall(target)
        script = next(target.glob("*/install.sh"), None)
        if script is None:
            raise UpdateError("В архиве нет install.sh.")
        on_status("Установка…")
        r = osdeps.run(["bash", str(script)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        shutil.rmtree(target, ignore_errors=True)
        if r.returncode:
            raise UpdateError("Установка не удалась:\n" + (r.stderr or r.stdout).strip()[-800:])
        return str(installs.USER_BIN)
    raise UpdateError(why_manual(kind) or "Эту установку программа обновить не может.")


def restart(command: str) -> None:
    """Запустить новую версию, как только эта закроется (иначе новая отдаст команду старой и выйдет)."""
    script = f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done; exec {shlex.quote(command)}"
    subprocess.Popen(["sh", "-c", script], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def gh_token() -> str:
    """Токен из GitHub CLI (gh), если человек в нём уже вошёл — по кнопке в настройках."""
    if not shutil.which("gh"):
        return ""
    r = osdeps.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else ""

