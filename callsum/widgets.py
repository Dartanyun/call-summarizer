"""Собственные виджеты: карточки созвонов, индикаторы уровня, пульсирующая точка, всплывающие подсказки."""

from PySide6.QtCore import (
    QEasingCurve, QModelIndex, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QSize, Qt,
    QTimer, QVariantAnimation,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout, QProgressBar,
    QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QToolButton,
    QVBoxLayout, QWidget,
)

from . import theme

# роли данных в списке созвонов
ROLE_ID = Qt.ItemDataRole.UserRole
ROLE_KIND = Qt.ItemDataRole.UserRole + 1     # "header" | "call"
ROLE_TITLE = Qt.ItemDataRole.UserRole + 2
ROLE_SUB = Qt.ItemDataRole.UserRole + 3
ROLE_CHIP = Qt.ItemDataRole.UserRole + 4     # (текст, тон) или None
ROLE_PROGRESS = Qt.ItemDataRole.UserRole + 5  # 0..1 или -1


def _qc(name: str, alpha: float | None = None) -> QColor:
    c = QColor(theme.C.get(name, name))
    if alpha is not None:
        c.setAlphaF(alpha)
    return c


class CallDelegate(QStyledItemDelegate):
    """Рисует созвон в списке карточкой: название, время и цветной статус."""

    def sizeHint(self, option, index):
        if index.data(ROLE_KIND) == "header":
            return QSize(option.rect.width(), 34)
        return QSize(option.rect.width(), 62)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect
        base = QFont(option.font)

        if index.data(ROLE_KIND) == "header":
            f = QFont(base)
            f.setPointSizeF(base.pointSizeF() * 0.82)
            f.setBold(True)
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)
            p.setFont(f)
            p.setPen(_qc("faint"))
            p.drawText(r.adjusted(18, 0, -12, -6), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                       index.data(Qt.ItemDataRole.DisplayRole).upper())
            p.restore()
            return

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        card = QRectF(r.adjusted(8, 2, -8, -2))
        if selected:
            p.setBrush(_qc("selection"))
        elif hover:
            p.setBrush(_qc("hover"))
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(card, 10, 10)
        if selected:
            p.setBrush(_qc("accent"))
            p.drawRoundedRect(QRectF(card.left() + 3, card.top() + 14, 3, card.height() - 28), 1.5, 1.5)

        inner = card.adjusted(16, 10, -12, -10)

        # статус-«таблетка» справа
        chip = index.data(ROLE_CHIP)
        chip_w = 0
        sf = QFont(base)
        sf.setPointSizeF(base.pointSizeF() * 0.82)
        sf.setBold(True)
        if chip:
            text, tone = chip
            fm = QFontMetrics(sf)
            chip_w = fm.horizontalAdvance(text) + 16
            chip_rect = QRectF(inner.right() - chip_w, inner.top() + 1, chip_w, fm.height() + 4)
            fg = _qc({"accent": "accent", "red": "red", "amber": "amber", "green": "green"}.get(tone, "muted"))
            bg = QColor(fg)
            bg.setAlphaF(0.16)
            p.setBrush(bg)
            p.drawRoundedRect(chip_rect, chip_rect.height() / 2, chip_rect.height() / 2)
            p.setFont(sf)
            p.setPen(fg)
            p.drawText(chip_rect, Qt.AlignmentFlag.AlignCenter, text)

        # заголовок
        tf = QFont(base)
        tf.setWeight(QFont.Weight.DemiBold)
        p.setFont(tf)
        p.setPen(_qc("text"))
        tfm = QFontMetrics(tf)
        title_w = int(inner.width() - (chip_w + 8 if chip_w else 0))
        title = tfm.elidedText(index.data(ROLE_TITLE) or "", Qt.TextElideMode.ElideRight, title_w)
        p.drawText(QRectF(inner.left(), inner.top(), title_w, tfm.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)

        # подпись
        mf = QFont(base)
        mf.setPointSizeF(base.pointSizeF() * 0.9)
        p.setFont(mf)
        p.setPen(_qc("muted"))
        mfm = QFontMetrics(mf)
        sub = mfm.elidedText(index.data(ROLE_SUB) or "", Qt.TextElideMode.ElideRight, int(inner.width()))
        p.drawText(QRectF(inner.left(), inner.bottom() - mfm.height(), inner.width(), mfm.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sub)

        # тонкая полоска прогресса по низу карточки
        prog = index.data(ROLE_PROGRESS)
        if prog is not None and prog >= 0:
            track = QRectF(card.left() + 16, card.bottom() - 5, card.width() - 32, 2.5)
            p.setBrush(_qc("border"))
            p.drawRoundedRect(track, 1.25, 1.25)
            p.setBrush(_qc("accent"))
            p.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * prog, track.height()), 1.25, 1.25)
        p.restore()


class LevelMeter(QWidget):
    """Плавный индикатор уровня громкости."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = 0.0
        self._target = 0.0
        self.setMinimumSize(200, 8)
        self.setMaximumHeight(8)
        self._timer = QTimer(self, interval=33, timeout=self._tick)
        self._timer.start()

    def set_level(self, v: float):
        self._target = max(0.0, min(1.0, v))

    def _tick(self):
        # быстро вверх, плавно вниз
        k = 0.55 if self._target > self._level else 0.12
        new = self._level + (self._target - self._level) * k
        if abs(new - self._level) > 0.002:
            self._level = new
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        rad = r.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc("hover"))
        p.drawRoundedRect(r, rad, rad)
        if self._level > 0.01:
            fill = QRectF(r.left(), r.top(), max(r.height(), r.width() * self._level), r.height())
            g = QLinearGradient(r.topLeft(), r.topRight())
            g.setColorAt(0.0, _qc("green"))
            g.setColorAt(0.75, _qc("green"))
            g.setColorAt(0.9, _qc("amber"))
            g.setColorAt(1.0, _qc("red"))
            p.setBrush(g)
            p.drawRoundedRect(fill, rad, rad)


class PulseDot(QWidget):
    """Красная точка с расходящимся кольцом — индикатор идущей записи."""

    def __init__(self, size: int = 72, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._t = 0.0
        self._anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=1600, loopCount=-1)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_value)

    def start(self):
        self._anim.start()

    def stop(self):
        self._anim.stop()

    def _on_value(self, v):
        self._t = float(v)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        core = self.width() * 0.2
        halo = core + (self.width() / 2 - core) * self._t
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc("red", 0.35 * (1 - self._t)))
        p.drawEllipse(c, halo, halo)
        p.setBrush(_qc("red"))
        p.drawEllipse(c, core, core)


class Toast(QLabel):
    """Короткое сообщение внизу окна, исчезает само."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._fx = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._fx)
        self._anim = QPropertyAnimation(self._fx, b"opacity", self, duration=220)
        self._hiding = False
        self._anim.finished.connect(lambda: self._hiding and self.hide())
        self._hide_timer = QTimer(self, singleShot=True, timeout=self._fade_out)
        self.bottom_margin = 28  # окно поднимает сообщение над плеером
        self.hide()

    def show_message(self, text: str, ms: int = 3500, error: bool = False):
        border = theme.C["red"] if error else theme.C["border"]
        self.setStyleSheet(
            f"background: {theme.C['surface']}; color: {theme.C['text']}; border: 1px solid {border};"
            "border-radius: 12px; padding: 10px 18px; font-weight: 600;"
        )
        self.setText(text)
        self._reposition()
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self._fx.opacity() if self.isVisible() else 0.0)
        self._anim.setEndValue(1.0)
        self._hiding = False
        self._anim.start()
        self._hide_timer.start(ms)

    def _fade_out(self):
        self._anim.stop()
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._hiding = True
        self._anim.start()

    def _reposition(self):
        par = self.parentWidget()
        if not par:
            return
        w = min(520, par.width() - 40)
        self.setFixedWidth(w)
        self.adjustSize()
        self.move((par.width() - w) // 2, par.height() - self.height() - self.bottom_margin)


class Segmented(QFrame):
    """Переключатель вкладок в виде сегментов."""

    def __init__(self, labels: list[str], on_change, parent=None):
        super().__init__(parent)
        self.setObjectName("SegBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for i, text in enumerate(labels):
            b = QPushButton(text)
            b.setObjectName("Seg")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.group.addButton(b, i)
            lay.addWidget(b)
        self.group.button(0).setChecked(True)
        self.group.idClicked.connect(on_change)

    def set_index(self, i: int):
        self.group.button(i).setChecked(True)


def icon_button(name: str, tooltip: str, slot=None, size: int = 18) -> QToolButton:
    b = QToolButton()
    b.setProperty("icon_name", name)
    b.setIcon(theme.icon(name, theme.C["muted"]))
    b.setIconSize(QSize(size, size))
    b.setToolTip(tooltip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if slot:
        b.clicked.connect(slot)
    return b


def refresh_icons(root: QWidget):
    """Перекрашивает иконки кнопок после смены темы."""
    for b in root.findChildren(QToolButton):
        name = b.property("icon_name")
        if name:
            b.setIcon(theme.icon(name, theme.C["muted"]))
    for b in root.findChildren(QPushButton):
        name = b.property("icon_name")
        if name:
            b.setIcon(theme.icon(name, theme.C[b.property("icon_color") or "text"]))


def set_icon(btn, name: str, color: str = "text", size: int = 16):
    btn.setIconSize(QSize(size, size))
    btn.setProperty("icon_name", name)
    btn.setProperty("icon_color", color)
    btn.setIcon(theme.icon(name, theme.C[color]))


class Banner(QFrame):
    """Плашка над документом: прогресс обработки, ошибка или подсказка."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Banner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 12, 12, 12)
        lay.setSpacing(12)
        self.icon = QLabel()
        self.icon.setFixedSize(20, 20)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(6)
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-weight: 600;")
        self.sub = QLabel()
        self.sub.setWordWrap(True)
        self.sub.setProperty("muted", True)
        self.sub.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        col.addWidget(self.title)
        col.addWidget(self.sub)
        col.addWidget(self.progress)
        lay.addLayout(col, 1)
        self.button = QPushButton()
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self.button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._slot = None
        self.button.clicked.connect(lambda: self._slot and self._slot())
        self._icon_spec = ("sparkles", "accent")
        self.hide()

    def set(self, kind: str, icon_name: str, title: str, sub: str = "",
            progress: float | None = None, button: tuple[str, object] | None = None):
        self.setProperty("kind", kind)
        self.style().unpolish(self)
        self.style().polish(self)
        tone = {"error": "red", "warn": "amber"}.get(kind, "accent")
        self._icon_spec = (icon_name, tone)
        self.icon.setPixmap(theme.pixmap(icon_name, theme.C[tone], 20, self.devicePixelRatioF() or 2))
        self.title.setText(title)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))
        if progress is None:
            self.progress.hide()
        else:
            self.progress.show()
            if progress < 0:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 1000)
                self.progress.setValue(int(progress * 1000))
        if button:
            self.button.setText(button[0])
            self._slot = button[1]
            self.button.show()
        else:
            self._slot = None
            self.button.hide()
        self.show()

    def refresh_theme(self):
        name, tone = self._icon_spec
        self.icon.setPixmap(theme.pixmap(name, theme.C[tone], 20, self.devicePixelRatioF() or 2))



class FlowLayout(QLayout):
    """Раскладка «в строку с переносом» — для плашек участников."""

    def __init__(self, parent=None, spacing: int = 8):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect: QRect, test: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() and line_h > 0:
                x = rect.x()
                y += line_h + self._spacing
                line_h = 0
            if not test:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()

    def clear(self):
        while self._items:
            item = self._items.pop()
            w = item.widget()
            if w:
                # deleteLater срабатывает не сразу — прячем, чтобы старая плашка не мелькала под новой
                w.hide()
                w.setParent(None)
                w.deleteLater()
