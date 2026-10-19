# PyInstaller: программа в одну папку вместе с Python и всеми библиотеками.
# Обычно запускается из packaging/build.py — он же готовит иконки и собирает установщики.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH).resolve().parent
BUILD = ROOT / "build" / "packaging"
sys.path.insert(0, str(ROOT))
from callsum import __version__  # noqa: E402

IS_WIN = sys.platform == "win32"
NAME = "CallSummarizer" if IS_WIN else "call-summarizer"

datas = (
    collect_data_files("callsum")  # иконки
    + collect_data_files("faster_whisper")  # модель поиска речи Silero VAD
)
# своих правил для PyInstaller у этих библиотек нет: забираем их .so/.dll явно
binaries = collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("sherpa_onnx")
hiddenimports = ["callsum.selfcheck", "callsum.kde_hotkey", "callsum.help_view", "callsum.tasks_dialog"]
if IS_WIN:
    datas += collect_data_files("soundcard")  # описание WASAPI для cffi (mediafoundation.py.h)
    hiddenimports += ["callsum.audio_win", "callsum.callwatch_win", "callsum.hotkey_win"]
else:
    hiddenimports += ["callsum.audio_pulse", "callsum.notify"]

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "torch", "IPython", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtWebEngineCore", "PySide6.QtPdf", "PySide6.Qt3DCore"],
    noarchive=False,
)
# Лишнее из Qt: экранная клавиатура, PDF, тема GTK (тянет за собой весь GTK машины сборки),
# экзотические графические режимы. Qt Quick не трогаем: он нужен плееру (модулю ffmpeg в Qt Multimedia).
# Перевод Qt оставляем только русский и английский.
DROP = ("libqtvirtualkeyboard", "qtvirtualkeyboard", "Qt6VirtualKeyboard", "Qt6Pdf", "libqpdf", "qpdf.dll",
        "libqgtk3", "EglFS", "EglFs", "egldeviceintegrations", "libqeglfs", "libqlinuxfb", "libqminimalegl",
        "libqvnc", "libqvkkhrdisplay", "qdirect2d")


def needed(entry) -> bool:
    name = entry[0].replace("\\", "/")
    if any(part in name for part in DROP):
        return False
    if "/translations/" in name and name.endswith(".qm"):
        return name.endswith(("_ru.qm", "_en.qm"))
    return True


a.binaries = [b for b in a.binaries if needed(b)]
a.datas = [d for d in a.datas if needed(d)]
if not IS_WIN:
    # Системные библиотеки (GLib, fontconfig, X11, PulseAudio, OpenSSL, libstdc++…) берём у системы
    # пользователя, а не у машины сборки: свои копии из другого дистрибутива ломают видеодрайверы,
    # шрифты и сертификаты. Оставляем только мелкие библиотеки xcb, которых часто нет в системе,
    # а без них Qt не запустится в X11.
    KEEP = ("libpython", "libxcb-cursor", "libxcb-icccm", "libxcb-image", "libxcb-keysyms", "libxcb-render-util",
            "libxcb-util", "libxkbcommon")
    # (модули самого Python — тип EXTENSION — не трогаем, даже если Python системный)
    a.binaries = [b for b in a.binaries
                  if not (b[2] == "BINARY" and b[1].startswith(("/usr/lib", "/lib"))
                          and not Path(b[1]).name.startswith(KEEP))]

pyz = PYZ(a.pure)

version_file = None
if IS_WIN:
    # «Свойства → Подробно» у CallSummarizer.exe: название и версия
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
    )

    numbers = tuple(int(x) for x in __version__.split(".")) + (0,)
    strings = [("FileDescription", "Саммари созвонов"), ("ProductName", "Саммари созвонов"),
               ("FileVersion", __version__), ("ProductVersion", __version__),
               ("InternalName", NAME), ("OriginalFilename", f"{NAME}.exe"), ("LegalCopyright", "MIT License")]
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[StringFileInfo([StringTable("041904B0", [StringStruct(k, v) for k, v in strings])]),
              VarFileInfo([VarStruct("Translation", [0x0419, 1200])])],
    )
    BUILD.mkdir(parents=True, exist_ok=True)
    version_file = BUILD / "version.txt"
    version_file.write_text(str(info), encoding="utf-8")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    console=not IS_WIN,  # в Windows — без чёрного окна консоли
    icon=str(BUILD / "call-summarizer.ico") if IS_WIN else None,
    version=str(version_file) if version_file else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)
