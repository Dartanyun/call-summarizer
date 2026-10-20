#!/usr/bin/env bash
# Установка «Саммари созвонов» для текущего пользователя (без sudo).
# Работает и из папки с исходниками (ставит Python-библиотеки), и из готовой сборки с GitHub
# (архив call-summarizer-X.Y.Z-linux-x86_64.tar.gz: программа там уже собрана со всеми библиотеками).
set -euo pipefail

APP_ID=call-summarizer
# имя иконки в теме; при смене рисунка меняем и имя — Plasma кэширует иконки по имени
ICON_NAME=call-summarizer-wave
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
PREFIX="$DATA/$APP_ID"
BIN="$HOME/.local/bin"
BUNDLE=0
[ -x "$SRC/$APP_ID" ] && [ -d "$SRC/_internal" ] && BUNDLE=1

missing=()
command -v ffmpeg  >/dev/null || missing+=(ffmpeg)
command -v pactl   >/dev/null || missing+=(pulseaudio-utils)
if ((!BUNDLE)); then
    command -v python3 >/dev/null || missing+=(python3)
    if ! command -v uv >/dev/null && ! python3 -c "import ensurepip" 2>/dev/null; then
        missing+=(python3-venv)
    fi
fi
# пакет .deb уже стоит: копия в домашней папке перекроет его ярлык в меню
if [ -x /opt/call-summarizer/call-summarizer ]; then
    echo "Внимание: в системе уже установлен пакет call-summarizer (.deb)."
    echo "Копия в домашней папке будет запускаться из меню вместо него."
    if [ -t 0 ]; then
        read -r -p "Всё равно поставить копию в домашнюю папку? [y/N] " answer
        [[ "$answer" =~ ^[yYдД] ]] || { echo "Отменено. Обновить пакет: sudo apt install ./call-summarizer_*.deb"; exit 0; }
    fi
fi

if ((${#missing[@]})); then
    echo "Не хватает системных пакетов. Установите их и запустите скрипт снова:"
    echo "    sudo apt install ${missing[*]}"
    exit 1
fi

if ((BUNDLE)); then
    echo "→ Копирую программу в $PREFIX/program"
    # прежняя установка из исходников больше не нужна (~600 МБ)
    rm -rf "$PREFIX/program" "$PREFIX/app" "$PREFIX/venv"
    mkdir -p "$PREFIX/program"
    cp -a "$SRC/$APP_ID" "$SRC/_internal" "$PREFIX/program/"
else
echo "→ Копирую приложение в $PREFIX/app"
rm -rf "$PREFIX/program"  # прежняя установка из готовой сборки
mkdir -p "$PREFIX/app"
rm -rf "$PREFIX/app/callsum"
cp -r "$SRC/callsum" "$PREFIX/app/"
find "$PREFIX/app" -name __pycache__ -prune -exec rm -rf {} +

echo "→ Ставлю зависимости в $PREFIX/venv (PySide6, faster-whisper, anthropic; ~600 МБ)"
if command -v uv >/dev/null; then
    [ -x "$PREFIX/venv/bin/python" ] || uv venv -q "$PREFIX/venv" --python python3
    uv pip install -q --python "$PREFIX/venv/bin/python" -r "$SRC/requirements.txt"
else
    [ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
    "$PREFIX/venv/bin/pip" install -q --upgrade pip
    "$PREFIX/venv/bin/pip" install -q -r "$SRC/requirements.txt"
fi
fi

echo "→ Создаю команду $BIN/$APP_ID"
mkdir -p "$BIN"
if ((BUNDLE)); then
    printf '#!/bin/sh\nexec "%s" "$@"\n' "$PREFIX/program/$APP_ID" > "$BIN/$APP_ID"
else
cat > "$BIN/$APP_ID" <<EOF
#!/bin/sh
PYTHONPATH="$PREFIX/app" exec "$PREFIX/venv/bin/python" -m callsum "\$@"
EOF
fi
chmod +x "$BIN/$APP_ID"

echo "→ Добавляю ярлык в меню приложений"
rm -f "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"  # иконка под старым именем
if ((BUNDLE)); then
    # в готовой сборке иконки уже нарисованы во всех размерах
    mkdir -p "$DATA/icons/hicolor"
    cp -r "$SRC/share/icons/hicolor/." "$DATA/icons/hicolor/"
else
install -Dm644 "$SRC/callsum/icon.svg" "$DATA/icons/hicolor/scalable/apps/$ICON_NAME.svg"
# PNG-копии: Plasma составляет список папок с иконками при запуске, и папки, созданные
# позже (например, scalable/), до повторного входа в систему не видит
QT_QPA_PLATFORM=offscreen "$PREFIX/venv/bin/python" - "$SRC/callsum/icon.svg" "$DATA/icons/hicolor" "$ICON_NAME" <<'PY' || true
import sys
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from pathlib import Path
app = QGuiApplication(sys.argv[:1])
src, root, name = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
renderer = QSvgRenderer(src)
for size in (48, 64, 128, 256):
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    out = root / f"{size}x{size}" / "apps" / f"{name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out))
PY
# значки состояния для трея: ожидание, запись, пауза, обработка
for f in "$SRC"/callsum/icons/tray/*.svg; do
    install -Dm644 "$f" "$DATA/icons/hicolor/scalable/status/$(basename "$f")"
done
fi
install -Dm644 /dev/stdin "$DATA/applications/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Саммари созвонов
Name[en]=Call Summarizer
GenericName=Запись и саммари созвонов
Comment=Записывает созвоны, распознаёт речь и делает краткое саммари
Comment[en]=Record calls, transcribe them locally and summarize with Claude
Exec=$BIN/$APP_ID
Icon=$ICON_NAME
Terminal=false
Categories=AudioVideo;Audio;Recorder;
Keywords=созвон;встреча;саммари;транскрипт;запись;meeting;call;summary;transcript;
StartupWMClass=$APP_ID
EOF
# горячие клавиши: отдельные скрытые ярлыки «начать/остановить запись» и «отметить момент»
TOGGLE_NAME="Саммари созвонов: начать или остановить запись"
install -Dm644 /dev/stdin "$DATA/applications/$APP_ID-toggle.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$TOGGLE_NAME
Name[en]=Call Summarizer: start or stop recording
Exec=$BIN/$APP_ID --toggle
Icon=$ICON_NAME
NoDisplay=true
X-KDE-Shortcuts=Meta+Shift+Z
DESKTOP
BOOKMARK_NAME="Саммари созвонов: отметить важный момент"
install -Dm644 /dev/stdin "$DATA/applications/$APP_ID-bookmark.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$BOOKMARK_NAME
Name[en]=Call Summarizer: mark an important moment
Exec=$BIN/$APP_ID --bookmark
Icon=$ICON_NAME
NoDisplay=true
X-KDE-Shortcuts=Meta+Shift+B
DESKTOP
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
command -v gtk-update-icon-cache  >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

# В KDE сочетание из ярлыка регистрируется, но само не включается — включаем,
# если оно свободно и вы не назначили своё.
assign_kde_shortcut() {  # ярлык, название действия, код сочетания, подпись
    local comp="$1" name="$2" key="$3" label="$4" obj="/component/${1//[-.]/_}"
    for _ in 1 2 3 4 5; do
        qdbus6 org.kde.kglobalaccel | grep -qx "$obj" && break
        sleep 1
    done
    info=$(qdbus6 --literal org.kde.kglobalaccel "$obj" org.kde.kglobalaccel.Component.allShortcutInfos 2>/dev/null || true)
    if [[ "$info" == *"[Argument: ai {0}], [Argument: ai"* ]] &&
       [ "$(qdbus6 org.kde.kglobalaccel /kglobalaccel org.kde.KGlobalAccel.isGlobalShortcutAvailable "$key" "$comp")" = "true" ]; then
        dbus-send --session --dest=org.kde.kglobalaccel /kglobalaccel org.kde.KGlobalAccel.setForeignShortcut \
            array:string:"$comp","_launch","$name","$name" array:int32:"$key" &&
            echo "→ Горячая клавиша $label"
    fi
}
if command -v qdbus6 >/dev/null && command -v dbus-send >/dev/null && qdbus6 org.kde.kglobalaccel >/dev/null 2>&1; then
    assign_kde_shortcut "$APP_ID-toggle.desktop" "$TOGGLE_NAME" 301989978 "Meta+Shift+Z: начать/остановить запись"
    assign_kde_shortcut "$APP_ID-bookmark.desktop" "$BOOKMARK_NAME" 301989954 "Meta+Shift+B: отметить важный момент"
fi

if ((BUNDLE)); then
    VERSION=$("$PREFIX/program/$APP_ID" --version | cut -d' ' -f2)
else
    VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$SRC/callsum/__init__.py")
fi
echo
echo "Готово, установлена версия $VERSION. Запуск: меню приложений → «Саммари созвонов» или команда $APP_ID"
echo "Записи хранятся в $PREFIX/calls"
