#!/usr/bin/env bash
# Удаление «Саммари созвонов». Записи и настройки остаются, если не передан --purge.
set -euo pipefail

APP_ID=call-summarizer
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
PREFIX="$DATA/$APP_ID"

rm -rf "$PREFIX/app" "$PREFIX/venv" "$PREFIX/program"
rm -f "$HOME/.local/bin/$APP_ID" \
      "$DATA/applications/$APP_ID.desktop" \
      "$DATA/applications/$APP_ID-toggle.desktop" \
      "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP_ID.desktop" \
      "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg" \
      "$DATA/icons/hicolor/scalable/apps/$APP_ID-wave.svg" \
      "$DATA"/icons/hicolor/*/apps/$APP_ID-wave.png \
      "$DATA"/icons/hicolor/scalable/status/$APP_ID-*.svg
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
if command -v dbus-send >/dev/null; then
    dbus-send --session --dest=org.kde.kglobalaccel /kglobalaccel org.kde.KGlobalAccel.unregister \
        string:"$APP_ID-toggle.desktop" string:"_launch" >/dev/null 2>&1 || true
fi

if [[ "${1:-}" == "--purge" ]]; then
    rm -rf "$PREFIX" "${XDG_CONFIG_HOME:-$HOME/.config}/$APP_ID"
    echo "Приложение, записи и настройки удалены."
else
    echo "Приложение удалено. Записи остались в $PREFIX/calls"
    echo "(чтобы удалить и их, запустите: $0 --purge)"
fi
