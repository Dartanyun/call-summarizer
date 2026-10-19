"""Оформление: светлая и тёмная палитры, таблица стилей и собственные иконки."""

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

DARK = {
    "bg": "#16181b",
    "sidebar": "#1c1f23",
    "card": "#202328",
    "surface": "#262a30",
    "hover": "#2d3239",
    "border": "#33383f",
    "text": "#e9ebee",
    "muted": "#9199a4",
    "faint": "#6b7380",
    "accent": "#7b8cff",
    "accent_hover": "#909eff",
    "accent_soft": "#272d4a",
    "on_accent": "#ffffff",
    "red": "#f05252",
    "red_hover": "#f46b6b",
    "red_soft": "#3a2226",
    "green": "#34c77b",
    "amber": "#f0a92e",
    "amber_soft": "#3a3020",
    "selection": "#2a3050",
    # цвета собеседников
    "spk0": "#f0a92e", "spk1": "#34c77b", "spk2": "#f472b6", "spk3": "#38bdf8",
    "spk4": "#a78bfa", "spk5": "#fb923c", "spk6": "#2dd4bf", "spk7": "#f87171",
}

LIGHT = {
    "bg": "#f5f6f8",
    "sidebar": "#eceef2",
    "card": "#ffffff",
    "surface": "#ffffff",
    "hover": "#e3e6eb",
    "border": "#dde1e7",
    "text": "#1d2127",
    "muted": "#646c78",
    "faint": "#9aa1ab",
    "accent": "#4f63ec",
    "accent_hover": "#3f53dc",
    "accent_soft": "#e7eafd",
    "on_accent": "#ffffff",
    "red": "#e03b3b",
    "red_hover": "#c92f2f",
    "red_soft": "#fde8e8",
    "green": "#1f9d5c",
    "amber": "#c27c0e",
    "amber_soft": "#fdf1dc",
    "selection": "#dfe4fc",
    "spk0": "#b8740b", "spk1": "#1f9d5c", "spk2": "#d6338a", "spk3": "#0284c7",
    "spk4": "#7c3aed", "spk5": "#ea580c", "spk6": "#0d9488", "spk7": "#dc2626",
}

# Текущие цвета; обновляются в apply().
C: dict[str, str] = dict(DARK)
is_dark = True

# Иконки 24×24, обводкой; цвет подставляется вместо currentColor.
_ICONS = {
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0"/><path d="M12 17.5V21"/><path d="M8.5 21h7"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2.5" fill="currentColor" stroke="none"/>',
    "record": '<circle cx="12" cy="12" r="6.5" fill="currentColor" stroke="none"/>',
    "upload": '<path d="M12 15V4"/><path d="M7.5 8.5 12 4l4.5 4.5"/><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
    "settings": '<path d="M4 6h9"/><path d="M17 6h3"/><circle cx="15" cy="6" r="2"/><path d="M4 12h3"/><path d="M11 12h9"/><circle cx="9" cy="12" r="2"/><path d="M4 18h11"/><path d="M19 18h1"/><circle cx="17" cy="18" r="2"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="2.5"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    "download": '<path d="M12 4v11"/><path d="M7.5 10.5 12 15l4.5-4.5"/><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
    "folder": '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2.2h7.4a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    "refresh": '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
    "sparkles": '<path d="M10 3.5c.6 3.6 2.4 5.4 6 6-3.6.6-5.4 2.4-6 6-.6-3.6-2.4-5.4-6-6 3.6-.6 5.4-2.4 6-6z"/><path d="M18 14.5c.3 1.7 1.1 2.5 2.8 2.8-1.7.3-2.5 1.1-2.8 2.8-.3-1.7-1.1-2.5-2.8-2.8 1.7-.3 2.5-1.1 2.8-2.8z"/>',
    "trash": '<path d="M4.5 7h15"/><path d="M9.5 7V5a1.5 1.5 0 0 1 1.5-1.5h2A1.5 1.5 0 0 1 14.5 5v2"/><path d="M6.5 7l.8 11.2a2 2 0 0 0 2 1.8h5.4a2 2 0 0 0 2-1.8L17.5 7"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
    "more": '<circle cx="5.5" cy="12" r="1.4" fill="currentColor"/><circle cx="12" cy="12" r="1.4" fill="currentColor"/><circle cx="18.5" cy="12" r="1.4" fill="currentColor"/>',
    "pencil": '<path d="M15.5 4.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4z"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 10h17"/><path d="M8 3v4"/><path d="M16 3v4"/>',
    "file": '<path d="M13.5 3.5H7a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z"/><path d="M13.5 3.5V9H19"/>',
    "speaker": '<path d="M4 9.5h3l4.5-4v13L7 14.5H4z"/><path d="M15.5 9a4.2 4.2 0 0 1 0 6"/><path d="M18.3 6.5a7.8 7.8 0 0 1 0 11"/>',
    "x": '<path d="M6.5 6.5l11 11"/><path d="M17.5 6.5l-11 11"/>',
    "alert": '<path d="M12 4 21 19.5H3z"/><path d="M12 10v4"/><circle cx="12" cy="16.8" r=".6" fill="currentColor"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="m11 12 8.5-8.5"/><path d="m16.5 6.5 2.5 2.5"/><path d="m14 9 2 2"/>',
    "text": '<path d="M5 6h14"/><path d="M5 11h14"/><path d="M5 16h9"/>',
    "users": '<circle cx="9" cy="8.5" r="3.3"/><path d="M3 19.5a6 6 0 0 1 12 0"/><path d="M15.5 5.4a3.3 3.3 0 0 1 0 6.2"/><path d="M17.5 14.2a6 6 0 0 1 3.5 5.3"/>',
    "dot": '<circle cx="12" cy="12" r="5" fill="currentColor" stroke="none"/>',
    "play": '<path d="M8.5 5.8v12.4a.8.8 0 0 0 1.2.7l9.6-6.2a.8.8 0 0 0 0-1.4L9.7 5.1a.8.8 0 0 0-1.2.7z" fill="currentColor" stroke="none"/>',
    "pause": '<rect x="6.8" y="5.5" width="3.8" height="13" rx="1.2" fill="currentColor" stroke="none"/><rect x="13.4" y="5.5" width="3.8" height="13" rx="1.2" fill="currentColor" stroke="none"/>',
    "back": '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3"/><path d="M4.5 4.5v4h4"/>',
    "forward": '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
}


def icon(name: str, color: str | None = None, size: int = 24) -> QIcon:
    """Иконка из набора, перекрашенная в нужный цвет (по умолчанию — цвет текста)."""
    color = color or C["text"]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        + _ICONS[name].replace("currentColor", color)
        + "</svg>"
    )
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    result = QIcon()
    for scale in (1, 2, 3):
        pm = QPixmap(size * scale, size * scale)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        renderer.render(p, QRectF(0, 0, size * scale, size * scale))
        p.end()
        pm.setDevicePixelRatio(scale)
        result.addPixmap(pm)
    return result


def svg_icon(svg: str, sizes=(16, 22, 24, 32, 48, 64)) -> QIcon:
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    result = QIcon()
    for size in sizes:
        for scale in (1, 2):
            pm = QPixmap(size * scale, size * scale)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            renderer.render(p, QRectF(0, 0, size * scale, size * scale))
            p.end()
            pm.setDevicePixelRatio(scale)
            result.addPixmap(pm)
    return result


# Значки для системного трея: одноцветный глиф «звук → текст» под цвет панели
# и цветная метка состояния.
TRAY_BADGES = {
    "idle": None,
    "recording": ("#f04848", ""),
    "paused": ("#f0a92e", '<rect x="16.3" y="16" width="1.6" height="5" rx=".6" fill="#fff"/>'
                          '<rect x="19.1" y="16" width="1.6" height="5" rx=".6" fill="#fff"/>'),
    "processing": ("#3daee9", '<circle cx="16.4" cy="18.5" r=".95" fill="#fff"/><circle cx="18.5" cy="18.5" r=".95" fill="#fff"/>'
                              '<circle cx="20.6" cy="18.5" r=".95" fill="#fff"/>'),
}


def tray_icon(state: str) -> QIcon:
    # Установленные в тему значки (~/.local/share/icons/hicolor/scalable/status) передаются
    # в трей по имени: так KDE надёжнее их показывает и сам перекрашивает под цвет панели.
    name = f"call-summarizer-{state}"
    if QIcon.hasThemeIcon(name):
        return QIcon.fromTheme(name)
    dark_panel = system_is_dark()
    fg = "#fcfcfc" if dark_panel else "#232629"
    edge = "#202326" if dark_panel else "#eff0f1"
    badge = TRAY_BADGES.get(state)
    lines = (
        f'<rect x="13" y="6.2" width="8.5" height="2.3" rx="1.15" fill="{fg}"/>'
        f'<rect x="13" y="10.2" width="6.5" height="2.3" rx="1.15" fill="{fg}"/>'
    )
    if not badge:
        lines += f'<rect x="13" y="14.2" width="4.5" height="2.3" rx="1.15" fill="{fg}"/>'
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        f'<rect x="2.5" y="9.5" width="2.4" height="5" rx="1.2" fill="{fg}"/>'
        f'<rect x="6.2" y="5.5" width="2.4" height="13" rx="1.2" fill="{fg}"/>'
        f'<rect x="9.9" y="8" width="2.4" height="8" rx="1.2" fill="{fg}"/>'
        + lines
    )
    if badge:
        color, inner = badge
        svg += f'<circle cx="18.5" cy="18.5" r="5.2" fill="{color}" stroke="{edge}" stroke-width="1.4"/>{inner}'
    return svg_icon(svg + "</svg>", sizes=(16, 22, 24, 32, 48))


def pixmap(name: str, color: str | None = None, size: int = 16, dpr: float = 2.0) -> QPixmap:
    return icon(name, color, size).pixmap(QSize(size, size), dpr)


def speaker_colors(names: dict[str, str]) -> dict[str, str]:
    """Цвет каждого говорящего; у спикеров с одинаковым именем (объединённых вручную) — один цвет."""
    out, by_name = {}, {}
    for sid, name in names.items():
        if sid in ("me", "Я") or name == "Я":
            out[sid] = C["accent"]
            continue
        if name not in by_name:
            by_name[name] = C[f"spk{len(by_name) % 8}"]
        out[sid] = by_name[name]
    return out


def system_is_dark() -> bool:
    return QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def ui_font() -> QFont:
    families = QFontDatabase.families()
    for name in ("Inter", "Noto Sans", "Cantarell", "Ubuntu"):
        if name in families:
            f = QFont(name)
            break
    else:
        f = QFont(QApplication.font())
    f.setPointSizeF(10)
    return f


def mono_family() -> str:
    families = QFontDatabase.families()
    for name in ("JetBrains Mono", "Noto Sans Mono", "Hack", "DejaVu Sans Mono"):
        if name in families:
            return name
    return "monospace"


def _palette(c: dict) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: c["bg"],
        QPalette.ColorRole.WindowText: c["text"],
        QPalette.ColorRole.Base: c["card"],
        QPalette.ColorRole.AlternateBase: c["surface"],
        QPalette.ColorRole.Text: c["text"],
        QPalette.ColorRole.Button: c["surface"],
        QPalette.ColorRole.ButtonText: c["text"],
        QPalette.ColorRole.Highlight: c["accent"],
        QPalette.ColorRole.HighlightedText: c["on_accent"],
        QPalette.ColorRole.ToolTipBase: c["surface"],
        QPalette.ColorRole.ToolTipText: c["text"],
        QPalette.ColorRole.PlaceholderText: c["faint"],
        QPalette.ColorRole.Link: c["accent"],
        QPalette.ColorRole.BrightText: c["red"],
        QPalette.ColorRole.Light: c["hover"],
        QPalette.ColorRole.Midlight: c["hover"],
        QPalette.ColorRole.Mid: c["border"],
        QPalette.ColorRole.Dark: c["border"],
        QPalette.ColorRole.Shadow: "#000000",
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["faint"]))
    return p


QSS = """
* {{ outline: 0; }}
QMainWindow, QDialog {{ background: {bg}; }}
QWidget {{ color: {text}; }}
QToolTip {{ background: {surface}; color: {text}; border: 1px solid {border}; border-radius: 6px; padding: 5px 8px; }}

#Sidebar {{ background: {sidebar}; border-right: 1px solid {border}; }}
#AppTitle {{ font-weight: 700; font-size: 11pt; }}
#Muted, QLabel[muted="true"] {{ color: {muted}; }}
#Faint {{ color: {faint}; }}
#H1 {{ font-size: 18pt; font-weight: 700; }}
#H2 {{ font-size: 13pt; font-weight: 700; }}
#Hero {{ font-size: 20pt; font-weight: 700; }}
#Timer {{ font-size: 40pt; font-weight: 300; }}

QPushButton {{
    background: {surface}; color: {text}; border: 1px solid {border};
    border-radius: 9px; padding: 7px 14px; font-weight: 600;
}}
QPushButton:hover {{ background: {hover}; }}
QPushButton:pressed {{ background: {border}; }}
QPushButton:disabled {{ color: {faint}; background: transparent; }}
QPushButton#Primary {{ background: {accent}; color: {on_accent}; border: none; }}
QPushButton#Primary:hover {{ background: {accent_hover}; }}
QPushButton#Record {{
    background: {red}; color: #ffffff; border: none; border-radius: 11px;
    padding: 11px 16px; font-size: 10.5pt; font-weight: 700;
}}
QPushButton#Record:hover {{ background: {red_hover}; }}
QPushButton#Record[recording="true"] {{ background: {red_soft}; color: {red}; border: 1px solid {red}; }}
QPushButton#Ghost {{ background: transparent; border: 1px solid transparent; color: {muted}; }}
QPushButton#Ghost:hover {{ background: {hover}; color: {text}; }}
QPushButton#Link {{ background: transparent; border: none; color: {accent}; padding: 2px 4px; }}
QPushButton#Link:hover {{ text-decoration: underline; }}

QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 6px; }}
QToolButton:hover {{ background: {hover}; }}
QToolButton:pressed {{ background: {border}; }}
QToolButton:disabled {{ background: transparent; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

QPushButton#Seg {{
    background: transparent; border: none; border-radius: 7px;
    padding: 6px 14px; color: {muted}; font-weight: 600;
}}
QPushButton#Seg:hover {{ color: {text}; }}
QPushButton#Seg:checked {{ background: {card}; color: {text}; border: 1px solid {border}; }}
#SegBar {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; }}

QLineEdit, QComboBox, QPlainTextEdit {{
    background: {card}; border: 1px solid {border}; border-radius: 8px;
    padding: 6px 9px; selection-background-color: {accent}; selection-color: {on_accent};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid {accent}; }}
QLineEdit#Search {{ background: {bg}; padding-left: 4px; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url({chevron}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {surface}; border: 1px solid {border}; border-radius: 8px; padding: 4px;
    selection-background-color: {selection}; selection-color: {text};
}}

QListView#CallList {{ background: transparent; border: none; }}
QTextBrowser#Doc {{ background: {card}; border: 1px solid {border}; border-radius: 14px; }}

#Card {{ background: {card}; border: 1px solid {border}; border-radius: 14px; }}
QPushButton#Chip {{
    background: {surface}; border: 1px solid {border}; border-radius: 13px;
    padding: 4px 12px 4px 9px; font-weight: 600;
}}
QPushButton#Chip:hover {{ background: {hover}; }}
QPushButton#Chip[auto="true"] {{ border: 1px dashed {faint}; }}
#Hint {{ color: {faint}; }}
#Player {{ background: {card}; border: 1px solid {border}; border-radius: 14px; }}
QToolButton#PlayButton {{ background: {accent}; border-radius: 17px; padding: 8px; }}
QToolButton#PlayButton:hover {{ background: {accent_hover}; }}
QToolButton#Speed {{ color: {muted}; font-weight: 700; padding: 4px 8px; }}
QSlider::groove:horizontal {{ height: 4px; background: {hover}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; background: {text};
}}
QSlider::handle:horizontal:hover {{ background: {accent}; }}
QScrollArea#Plain, QScrollArea#Plain > QWidget > QWidget {{ background: transparent; border: none; }}
#Banner {{ background: {accent_soft}; border-radius: 12px; }}
#Banner[kind="error"] {{ background: {red_soft}; }}
#Banner[kind="warn"] {{ background: {amber_soft}; }}
#StepIcon {{ background: {accent_soft}; border-radius: 22px; }}

QProgressBar {{ background: {hover}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 3px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 3px; min-height: 36px; }}
QScrollBar::handle:vertical:hover {{ background: {faint}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
QScrollBar::handle:horizontal {{ background: {border}; border-radius: 3px; min-width: 36px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMenu {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; padding: 5px; }}
QMenu::item {{ padding: 7px 18px 7px 10px; border-radius: 6px; }}
QMenu::item:selected {{ background: {hover}; }}
QMenu::item:disabled {{ color: {faint}; }}
QMenu::icon {{ padding-left: 8px; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 4px 6px; }}

QGroupBox {{
    background: {card}; border: 1px solid {border}; border-radius: 12px;
    margin-top: 26px; padding: 14px 12px 10px 12px; font-weight: 700;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 4px; top: 2px; color: {text}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {faint}; background: {card}; }}
QCheckBox::indicator:checked {{ background: {accent}; border: 1px solid {accent}; image: url({check}); }}
QSplitter::handle {{ background: transparent; }}
QMessageBox {{ background: {bg}; }}
"""

def _svg_file(name: str, path_d: str, color: str) -> str:
    """QSS принимает картинки только файлами, поэтому кладём маленький SVG во временный каталог."""
    import tempfile
    from pathlib import Path

    path = Path(tempfile.gettempdir()) / f"call-summarizer-{name}-{color.strip('#')}.svg"
    if not path.exists():
        path.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="{path_d}" '
            f'fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        )
    return path.as_posix()


def apply(app: QApplication, mode: str = "system") -> None:
    """mode: system | light | dark."""
    global is_dark
    is_dark = system_is_dark() if mode == "system" else mode == "dark"
    C.clear()
    C.update(DARK if is_dark else LIGHT)
    app.setStyle("Fusion")
    app.setPalette(_palette(C))
    app.setStyleSheet(QSS.format(
        **C,
        check=_svg_file("check", "M3.5 8.5l3 3 6-7", C["on_accent"]),
        chevron=_svg_file("chevron", "M4 6l4 4 4-4", C["muted"]),
    ))
