#!/usr/bin/env bash
# Установка «Саммари созвонов» для текущего пользователя (без sudo).
set -euo pipefail

APP_ID=call-summarizer
# имя иконки в теме; при смене рисунка меняем и имя — Plasma кэширует иконки по имени
ICON_NAME=call-summarizer-wave
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
PREFIX="$DATA/$APP_ID"
BIN="$HOME/.local/bin"

missing=()
command -v ffmpeg  >/dev/null || missing+=(ffmpeg)
command -v pactl   >/dev/null || missing+=(pulseaudio-utils)
command -v python3 >/dev/null || missing+=(python3)
if ! command -v uv >/dev/null && ! python3 -c "import ensurepip" 2>/dev/null; then
    missing+=(python3-venv)
fi
if ((${#missing[@]})); then
    echo "Не хватает системных пакетов. Установите их и запустите скрипт снова:"
    echo "    sudo apt install ${missing[*]}"
    exit 1
fi

echo "→ Копирую приложение в $PREFIX/app"
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

echo "→ Создаю команду $BIN/$APP_ID"
mkdir -p "$BIN"
cat > "$BIN/$APP_ID" <<EOF
#!/bin/sh
PYTHONPATH="$PREFIX/app" exec "$PREFIX/venv/bin/python" -m callsum "\$@"
EOF
chmod +x "$BIN/$APP_ID"

echo "→ Добавляю ярлык в меню приложений"
install -Dm644 "$SRC/callsum/icon.svg" "$DATA/icons/hicolor/scalable/apps/$ICON_NAME.svg"
rm -f "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"  # иконка под старым именем
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
# горячая клавиша: отдельный скрытый ярлык «начать/остановить запись»
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
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
command -v gtk-update-icon-cache  >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

# В KDE сочетание из ярлыка регистрируется, но само не включается — включаем,
# если оно свободно и вы не назначили своё.
if command -v qdbus6 >/dev/null && command -v dbus-send >/dev/null && qdbus6 org.kde.kglobalaccel >/dev/null 2>&1; then
    COMP="$APP_ID-toggle.desktop"
    OBJ="/component/${APP_ID//-/_}_toggle_desktop"
    KEY=301989978  # Meta+Shift+Z
    for _ in 1 2 3 4 5; do
        qdbus6 org.kde.kglobalaccel | grep -qx "$OBJ" && break
        sleep 1
    done
    info=$(qdbus6 --literal org.kde.kglobalaccel "$OBJ" org.kde.kglobalaccel.Component.allShortcutInfos 2>/dev/null || true)
    if [[ "$info" == *"[Argument: ai {0}], [Argument: ai"* ]] &&
       [ "$(qdbus6 org.kde.kglobalaccel /kglobalaccel org.kde.KGlobalAccel.isGlobalShortcutAvailable $KEY "$COMP")" = "true" ]; then
        dbus-send --session --dest=org.kde.kglobalaccel /kglobalaccel org.kde.KGlobalAccel.setForeignShortcut \
            array:string:"$COMP","_launch","$TOGGLE_NAME","$TOGGLE_NAME" array:int32:$KEY &&
            echo "→ Горячая клавиша Meta+Shift+Z: начать/остановить запись"
    fi
fi

echo
echo "Готово. Запуск: меню приложений → «Саммари созвонов» или команда $APP_ID"
echo "Записи хранятся в $PREFIX/calls"
