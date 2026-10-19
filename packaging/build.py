"""Готовые сборки программы: python packaging/build.py

Собирает программу вместе с Python и всеми библиотеками (PyInstaller), так что на компьютере
пользователя ничего, кроме неё, ставить не нужно. Собирать нужно на той системе, для которой
сборка: Linux-версию — в Linux, Windows-версию — в Windows (в GitHub Actions это делает build.yml).

Нужны: программа и PyInstaller в текущем окружении Python (pip install . pyinstaller),
в Linux ещё dpkg-deb, в Windows — Inno Setup 6 (для установщика).

Результат — в dist/:
    Linux:   call-summarizer-X.Y.Z-linux-x86_64.tar.gz   распаковать и запустить ./install.sh
             call-summarizer_X.Y.Z_amd64.deb             sudo apt install ./call-summarizer_X.Y.Z_amd64.deb
    Windows: CallSummarizer-X.Y.Z-windows-x64-setup.exe  установщик, права администратора не нужны
             CallSummarizer-X.Y.Z-windows-x64-portable.zip  без установки
"""

import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build" / "packaging"
sys.path.insert(0, str(ROOT))

from callsum import __version__ as VERSION  # noqa: E402
from callsum import config  # noqa: E402

IS_WIN = sys.platform == "win32"
NAME = "CallSummarizer" if IS_WIN else "call-summarizer"
REPO = "https://github.com/Dartanyun/call-summarizer"
ICON_SIZES = (48, 64, 128, 256)
TOGGLE_NAME = "Саммари созвонов: начать или остановить запись"


def step(text: str) -> None:
    print(f"\n→ {text}", flush=True)


def render_icons() -> None:
    """.ico для Windows и PNG разных размеров для меню Linux — из callsum/icon.svg."""
    from PySide6.QtGui import QGuiApplication

    from callsum import make_icon

    # рисуем без окна и без экрана — так работает и на сервере сборки
    _app = QGuiApplication.instance() or QGuiApplication([sys.argv[0], "-platform", "offscreen"])  # noqa: F841
    if IS_WIN:
        make_icon.render(BUILD / "call-summarizer.ico", 256)
    else:
        for size in ICON_SIZES:
            make_icon.render(BUILD / "icons" / f"{size}.png", size)


def pyinstaller() -> Path:
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller"),
                    str(ROOT / "packaging" / "call-summarizer.spec")], check=True)
    return DIST / NAME


def check_linux_libs(app_dir: Path) -> None:
    """Ни одна библиотека Qt не должна браться из системы: в KDE своя версия Qt, и смесь двух версий
    ломает программу. Остальные системные библиотеки (GLib, X11, PulseAudio…) — наоборот, системные."""
    internal = app_dir / "_internal"
    env = dict(os.environ, LD_LIBRARY_PATH=str(internal))
    bad = set()
    for f in app_dir.rglob("*.so*"):
        if not f.is_file() or f.is_symlink():
            continue
        r = subprocess.run(["ldd", str(f)], capture_output=True, text=True, env=env)
        for line in r.stdout.splitlines():
            name, _, where = line.strip().partition(" => ")
            if name.startswith(("libQt6", "libpyside6", "libshiboken6")) and not where.startswith(str(app_dir)):
                bad.add(f"{f.relative_to(app_dir)}: {name} => {where}")
    if bad:
        raise SystemExit("Библиотеки Qt ищутся вне сборки:\n  " + "\n  ".join(sorted(bad)))
    print("Все библиотеки Qt — из сборки")


def self_check(app_dir: Path) -> None:
    """Собранная программа должна запуститься и найти внутри себя все библиотеки."""
    exe = app_dir / (f"{NAME}.exe" if IS_WIN else NAME)
    report = BUILD / "self-check.txt"
    env = dict(os.environ)
    if not IS_WIN and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        env.setdefault("QT_QPA_PLATFORM", "offscreen")  # сервер сборки без экрана
    r = subprocess.run([str(exe), "--self-check", "--report", str(report)], env=env)
    if IS_WIN:  # без консоли программа ничего не печатает — показываем отчёт
        print(report.read_text(encoding="utf-8") if report.exists() else "(отчёта нет)")
    if r.returncode:
        raise SystemExit("Самопроверка собранной программы не прошла")


# ---------------- Linux ----------------

# Библиотеки, которые сборка берёт у системы (см. call-summarizer.spec). На рабочем столе они
# почти всегда уже стоят; в Ubuntu 24.04+ и Debian 13+ часть пакетов называется с суффиксом t64.
DEB_DEPENDS = ", ".join([
    "ffmpeg", "pulseaudio-utils", "libc6 (>= 2.35)", "libstdc++6", "libglib2.0-0t64 | libglib2.0-0",
    "libssl3t64 | libssl3", "libffi8", "libpulse0", "libdbus-1-3", "libfontconfig1", "libfreetype6",
    "libegl1", "libgl1", "libdrm2", "libx11-6", "libx11-xcb1", "libxext6", "libxrender1", "libxcb1",
    "libxcb-glx0", "libxcb-randr0", "libxcb-render0", "libxcb-shape0", "libxcb-shm0", "libxcb-sync1",
    "libxcb-xfixes0", "libxcb-xkb1", "libwayland-client0", "libwayland-cursor0", "libwayland-egl1",
])


def normalize_modes(root: Path) -> None:
    """Права как в обычных пакетах: папки и программы 755, остальное 644 (не зависят от umask)."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        if path.is_dir() or path.stat().st_mode & 0o111:
            path.chmod(0o755)
        else:
            path.chmod(0o644)

def hicolor(dst: Path) -> None:
    """Иконки в теме hicolor: значок программы (SVG и PNG) и значки состояния для трея."""
    apps = dst / "scalable" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "callsum" / "icon.svg", apps / f"{config.ICON_NAME}.svg")
    for size in ICON_SIZES:
        png = dst / f"{size}x{size}" / "apps"
        png.mkdir(parents=True, exist_ok=True)
        shutil.copy(BUILD / "icons" / f"{size}.png", png / f"{config.ICON_NAME}.png")
    status = dst / "scalable" / "status"
    status.mkdir(parents=True, exist_ok=True)
    for svg in (ROOT / "callsum" / "icons" / "tray").glob("*.svg"):
        shutil.copy(svg, status / svg.name)


def desktop_entries(exe: str) -> dict[str, str]:
    """Ярлыки для .deb (для установки из архива их создаёт install.sh)."""
    app_id = config.APP_ID
    return {
        f"{app_id}.desktop": f"""[Desktop Entry]
Type=Application
Name=Саммари созвонов
Name[en]=Call Summarizer
GenericName=Запись и саммари созвонов
Comment=Записывает созвоны, распознаёт речь и делает краткое саммари
Comment[en]=Record calls, transcribe them locally and summarize with Claude
Exec={exe}
Icon={config.ICON_NAME}
Terminal=false
Categories=AudioVideo;Audio;Recorder;
Keywords=созвон;встреча;саммари;транскрипт;запись;meeting;call;summary;transcript;
StartupWMClass={app_id}
""",
        f"{app_id}-toggle.desktop": f"""[Desktop Entry]
Type=Application
Name={TOGGLE_NAME}
Name[en]=Call Summarizer: start or stop recording
Exec={exe} --toggle
Icon={config.ICON_NAME}
NoDisplay=true
X-KDE-Shortcuts=Meta+Shift+Z
""",
    }


def linux_tarball(app_dir: Path) -> Path:
    """Архив: программа, иконки и install.sh, который ставит её в домашнюю папку (без sudo)."""
    top = f"{config.APP_ID}-{VERSION}-linux-x86_64"
    stage = BUILD / top
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(app_dir, stage, symlinks=True)
    hicolor(stage / "share" / "icons" / "hicolor")
    for f in ("install.sh", "uninstall.sh", "README.md", "CHANGELOG.md", "LICENSE"):
        shutil.copy2(ROOT / f, stage / f)
    normalize_modes(stage)
    out = DIST / f"{top}.tar.gz"
    with tarfile.open(out, "w:gz", compresslevel=6) as tar:
        tar.add(stage, arcname=top)
    return out


def linux_deb(app_dir: Path) -> Path:
    """Пакет для Debian и Ubuntu: программа в /opt, команда, ярлыки и иконки — в /usr."""
    app_id = config.APP_ID
    root = BUILD / "deb"
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(app_dir, root / "opt" / app_id, symlinks=True)
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / app_id).symlink_to(f"/opt/{app_id}/{NAME}")
    apps = root / "usr" / "share" / "applications"
    apps.mkdir(parents=True)
    for name, text in desktop_entries(app_id).items():
        (apps / name).write_text(text, encoding="utf-8")
    hicolor(root / "usr" / "share" / "icons" / "hicolor")
    doc = root / "usr" / "share" / "doc" / app_id
    doc.mkdir(parents=True)
    license_text = "\n".join(f" {line}" if line.strip() else " ." for line in
                             (ROOT / "LICENSE").read_text(encoding="utf-8").strip().splitlines())
    (doc / "copyright").write_text(
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        f"Upstream-Name: {app_id}\nSource: {REPO}\n\nFiles: *\nLicense: MIT\n{license_text}\n", encoding="utf-8")

    size_kb = sum(f.stat().st_size for f in root.rglob("*") if f.is_file() and not f.is_symlink()) // 1024
    control = root / "DEBIAN" / "control"
    control.parent.mkdir()
    control.write_text(f"""Package: {app_id}
Version: {VERSION}
Section: sound
Priority: optional
Architecture: amd64
Maintainer: Dartanyun <118448717+Dartanyun@users.noreply.github.com>
Installed-Size: {size_kb}
Depends: {DEB_DEPENDS}
Recommends: pipewire-bin, libglib2.0-bin
Homepage: {REPO}
Description: запись созвонов, распознавание речи и саммари через Claude
 Записывает созвон — микрофон и звук собеседников на отдельных дорожках,
 локально распознаёт речь и говорящих (faster-whisper, sherpa-onnx)
 и делает структурированное саммари через Claude API.
""", encoding="utf-8")
    normalize_modes(root)
    out = DIST / f"{app_id}_{VERSION}_amd64.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(root), str(out)], check=True)
    return out


# ---------------- Windows ----------------

def windows_zip(app_dir: Path) -> Path:
    out = DIST / f"{NAME}-{VERSION}-windows-x64-portable.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(app_dir.rglob("*")):
            if f.is_file():
                z.write(f, Path(NAME) / f.relative_to(app_dir))
    return out


def windows_setup(app_dir: Path) -> Path:
    iscc = shutil.which("iscc") or next(
        (str(p) for p in (Path(os.environ.get(v, "")) / "Inno Setup 6" / "ISCC.exe"
                          for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")) if p.is_file()), None)
    if not iscc:
        raise SystemExit("Не найден Inno Setup 6 (ISCC.exe): https://jrsoftware.org/isdl.php")
    subprocess.run([iscc, "/Qp", f"/DAppVersion={VERSION}", f"/DSourceDir={app_dir}", f"/DOutputDir={DIST}",
                    f"/DIconFile={BUILD / 'call-summarizer.ico'}", str(ROOT / "packaging" / "windows" / "installer.iss")],
                   check=True)
    return DIST / f"{NAME}-{VERSION}-windows-x64-setup.exe"


def main() -> None:
    shutil.rmtree(DIST, ignore_errors=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    step(f"Версия {VERSION}: иконки")
    render_icons()
    step("Сборка программы (PyInstaller)")
    app_dir = pyinstaller()
    step("Самопроверка собранной программы")
    if not IS_WIN:
        check_linux_libs(app_dir)
    self_check(app_dir)
    if IS_WIN:
        step("Архив без установки и установщик")
        outputs = [windows_zip(app_dir), windows_setup(app_dir)]
    else:
        step("Архив и пакет .deb")
        outputs = [linux_tarball(app_dir), linux_deb(app_dir)]
    print("\nГотово:")
    for out in outputs:
        print(f"  {out.relative_to(ROOT)}  ({out.stat().st_size / 2**20:.0f} МБ)")


if __name__ == "__main__":
    main()
