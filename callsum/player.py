"""Плеер записи созвона: воспроизведение, перемотка, скорость, переход к таймкоду."""

import threading
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QToolTip, QWidget

from . import audio, theme
from .widgets import icon_button

SPEEDS = (1.0, 1.25, 1.5, 2.0)


def _fmt(ms: int) -> str:
    s = max(0, ms // 1000)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class Timeline(QWidget):
    """Полоса записи: цветом — кто когда говорил, ромбиками — пункты саммари, флажками — отметки
    «важный момент», чёрточками — начала глав, кружок — текущее место."""

    seek = Signal(float)      # секунды, по отпусканию мыши
    scrub = Signal(float)     # секунды, пока тянут
    bookmark_menu = Signal(int, object)  # номер отметки, глобальная точка — правый клик по флажку

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(34)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.duration = 0.0
        self.position = 0.0
        self.segments: list[tuple[float, float, str, str]] = []  # начало, конец, цвет, имя
        self.markers: list[tuple[float, str]] = []
        self.bookmarks: list[tuple[float, str]] = []
        self.chapters: list[tuple[float, str]] = []
        self._drag = False
        self.setMinimumHeight(40)

    def set_data(self, duration: float, segments, markers, bookmarks=(), chapters=()):
        self.duration = max(0.0, duration)
        self.segments = segments
        self.markers = markers
        self.bookmarks = list(bookmarks)
        self.chapters = list(chapters)
        self.update()

    def set_duration(self, duration: float):
        if duration > 0:
            self.duration = duration
            self.update()

    def set_position(self, seconds: float):
        if not self._drag:
            self.position = seconds
            self.update()

    # геометрия
    def _track(self) -> QRectF:
        return QRectF(7, self.height() / 2 + 2, self.width() - 14, 8)

    def _x(self, t: float) -> float:
        tr = self._track()
        return tr.left() + (t / self.duration * tr.width() if self.duration else 0)

    def _t(self, x: float) -> float:
        tr = self._track()
        return max(0.0, min(self.duration, (x - tr.left()) / max(1.0, tr.width()) * self.duration))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        tr = self._track()
        clip = QPainterPath()
        clip.addRoundedRect(tr, 4, 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.C["hover"]))
        p.drawPath(clip)
        played_x = self._x(self.position)
        p.save()
        p.setClipPath(clip)
        if self.segments:
            for start, end, color, _name in self.segments:
                x1, x2 = self._x(start), max(self._x(end), self._x(start) + 1.5)
                for part, alpha in ((QRectF(x1, tr.top(), min(x2, played_x) - x1, tr.height()), 1.0),
                                    (QRectF(max(x1, played_x), tr.top(), x2 - max(x1, played_x), tr.height()), 0.38)):
                    if part.width() > 0:
                        c = QColor(color)
                        c.setAlphaF(alpha)
                        p.fillRect(part, c)
        else:
            p.fillRect(QRectF(tr.left(), tr.top(), played_x - tr.left(), tr.height()), QColor(theme.C["accent"]))
        p.restore()
        # начала глав — светлые чёрточки поперёк полосы
        p.setPen(QColor(theme.C["bg"]))
        for t, _title in self.chapters:
            if t > 0:
                x = self._x(t)
                p.drawLine(QPointF(x, tr.top() - 1), QPointF(x, tr.bottom() + 1))
        p.setPen(Qt.PenStyle.NoPen)
        # отметки «важный момент» — флажки под полосой
        p.setBrush(QColor(theme.C["red"]))
        for t, _note in self.bookmarks:
            x = self._x(t)
            y = tr.bottom() + 3
            p.drawPolygon([QPointF(x, y), QPointF(x + 4.5, y + 6), QPointF(x - 4.5, y + 6)])
        # пункты саммари
        p.setBrush(QColor(theme.C["amber"]))
        for t, _text in self.markers:
            x = self._x(t)
            y = tr.top() - 9
            p.drawPolygon([QPointF(x, y - 4), QPointF(x + 4, y), QPointF(x, y + 4), QPointF(x - 4, y)])
        # текущее место
        if self.duration:
            p.setBrush(QColor(theme.C["text"]))
            p.drawEllipse(QPointF(played_x, tr.center().y()), 6.5, 6.5)

    def _bookmark_at(self, x: float, y: float) -> int:
        if y < self._track().bottom():
            return -1
        return next((i for i, (t, _n) in enumerate(self.bookmarks) if abs(self._x(t) - x) < 7), -1)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            i = self._bookmark_at(e.position().x(), e.position().y())
            if i >= 0:
                self.bookmark_menu.emit(i, e.globalPosition().toPoint())
            return
        if self.duration and e.button() == Qt.MouseButton.LeftButton:
            self._drag = True
            self.position = self._t(e.position().x())
            self.scrub.emit(self.position)
            self.update()

    def mouseMoveEvent(self, e):
        t = self._t(e.position().x())
        if self._drag:
            self.position = t
            self.scrub.emit(t)
            self.update()
        # подсказка: пункт саммари или отметка рядом, иначе — глава и кто говорит в этот момент
        tip = ""
        x, y = e.position().x(), e.position().y()
        for mt, text in self.markers:
            if abs(self._x(mt) - x) < 6 and y < self._track().top():
                tip = f"{_fmt(int(mt * 1000))} — {text}"
                break
        i = self._bookmark_at(x, y)
        if not tip and i >= 0:
            bt, note = self.bookmarks[i]
            tip = f"⚑ {_fmt(int(bt * 1000))}" + (f" — {note}" if note else " — отмеченный момент") + \
                  "\nПравая кнопка — подписать или удалить"
        if not tip and self.duration:
            who = next((name for s, en, _c, name in self.segments if s <= t <= en), "")
            chapter = next((title for ct, title in reversed(self.chapters) if ct <= t), "")
            tip = f"{_fmt(int(t * 1000))}" + (f" · {chapter}" if chapter else "") + (f" · {who}" if who else "")
        if tip:
            QToolTip.showText(e.globalPosition().toPoint(), tip, self)

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.position = self._t(e.position().x())
            self.seek.emit(self.position)
            self.update()


class PlayerBar(QFrame):
    position = Signal(float)          # текущая позиция, секунды
    _prepared = Signal(str, str)      # call_id, путь к файлу (из фонового потока)
    _failed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Player")
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.player.setAudioOutput(self.output)
        self._call = None
        self._loaded = False
        self._preparing = False
        self._pending: float | None = None
        self._speed = 0

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 14, 8)
        lay.setSpacing(6)
        self.btn_back = icon_button("back", "Назад на 10 секунд", lambda: self.skip(-10))
        self.btn_play = QToolButton()
        self.btn_play.setObjectName("PlayButton")
        self.btn_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_play.setIconSize(QSize(18, 18))
        self.btn_play.setToolTip("Слушать запись")
        self.btn_play.clicked.connect(self.toggle)
        self.btn_fwd = icon_button("forward", "Вперёд на 10 секунд", lambda: self.skip(10))
        self.time = QLabel("0:00")
        self.time.setProperty("muted", True)
        self.time.setMinimumWidth(110)
        self.timeline = Timeline()
        self.timeline.scrub.connect(lambda t: self._show_time(int(t * 1000)))
        self.timeline.seek.connect(self._on_seek)
        self.speed = QToolButton()
        self.speed.setObjectName("Speed")
        self.speed.setCursor(Qt.CursorShape.PointingHandCursor)
        self.speed.setToolTip("Скорость воспроизведения")
        self.speed.clicked.connect(self._next_speed)
        self.status = QLabel()
        self.status.setObjectName("Faint")
        for w in (self.btn_back, self.btn_play, self.btn_fwd, self.time):
            lay.addWidget(w)
        lay.addWidget(self.timeline, 1)
        lay.addWidget(self.status)
        lay.addWidget(self.speed)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(lambda d: self.timeline.set_duration(d / 1000))
        self.player.playbackStateChanged.connect(lambda _s: self._update_icon())
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(lambda _e, msg: self.status.setText(f"Ошибка: {msg}"))
        self._prepared.connect(self._on_prepared)
        self._failed.connect(lambda _cid, msg: (self.status.setText(msg), setattr(self, "_preparing", False)))
        self._update_icon()
        self._update_speed()

    # ---------- публичное ----------

    def set_call(self, call) -> bool:
        """Возвращает True, если созвон сменился (и таймлайн нужно заполнить заново)."""
        if self._call is not None and call is not None and call.id == self._call.id:
            return False
        self.player.stop()
        self.player.setSource(QUrl())
        self._call, self._loaded, self._pending = call, False, None
        self.status.setText("")
        self.timeline.set_data(call.duration if call else 0.0, [], [])
        self.timeline.set_position(0)
        self.time.setText(f"0:00 / {_fmt(int((call.duration if call else 0) * 1000))}")
        self.setVisible(bool(call and call.tracks()))
        return True

    def play_at(self, seconds: float):
        self._pending = max(0.0, seconds)
        if self._loaded and self.player.mediaStatus() in (
            QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
            QMediaPlayer.MediaStatus.EndOfMedia,
        ):
            self._apply_pending()
        else:
            self._ensure_loaded()

    def toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        elif self._loaded:
            self.player.play()
        else:
            self.play_at(0.0)

    def skip(self, seconds: float):
        if self._loaded:
            self.player.setPosition(max(0, self.player.position() + int(seconds * 1000)))

    def stop(self):
        self.player.pause()

    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def refresh_theme(self):
        self._update_icon()

    # ---------- внутреннее ----------

    def _ensure_loaded(self):
        call = self._call
        if call is None:
            return
        path = call.playback_path()
        if path is not None:
            self._load(path)
            return
        if self._preparing:
            return
        # старые записи с двумя дорожками: сводим их один раз в фоне
        self._preparing = True
        self.status.setText("Готовлю запись…")
        tracks, dst, cid = call.tracks(), call.path / "playback.ogg", call.id

        def work():
            try:
                audio.make_playback(tracks, dst)
                self._prepared.emit(cid, str(dst))
            except audio.AudioError as e:
                self._failed.emit(cid, str(e).splitlines()[0])

        threading.Thread(target=work, daemon=True).start()

    def _on_prepared(self, call_id: str, path: str):
        self._preparing = False
        self.status.setText("")
        if self._call is not None and self._call.id == call_id:
            self._load(Path(path))

    def _load(self, path: Path):
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self._loaded = True

    def _on_status(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self._apply_pending()

    def _apply_pending(self):
        if self._pending is not None:
            self.player.setPosition(int(self._pending * 1000))
            self._pending = None
            self.player.play()

    def set_timeline(self, segments, markers, bookmarks=(), chapters=()):
        self.timeline.set_data(self.timeline.duration, segments, markers, bookmarks, chapters)

    def current_position(self) -> float:
        return self.timeline.position

    def _on_position(self, ms: int):
        self.timeline.set_position(ms / 1000)
        if not self.timeline._drag:
            self._show_time(ms)
        self.position.emit(ms / 1000)

    def _show_time(self, ms: int):
        self.time.setText(f"{_fmt(ms)} / {_fmt(int(self.timeline.duration * 1000))}")

    def _on_seek(self, seconds: float):
        if self._loaded:
            self.player.setPosition(int(seconds * 1000))
        else:
            self.play_at(seconds)

    def _next_speed(self):
        self._speed = (self._speed + 1) % len(SPEEDS)
        self._update_speed()

    def _update_speed(self):
        rate = SPEEDS[self._speed]
        self.player.setPlaybackRate(rate)
        self.speed.setText(f"{rate:g}×")

    def _update_icon(self):
        playing = self.is_playing()
        self.btn_play.setIcon(theme.icon("pause" if playing else "play", theme.C["on_accent"]))
        self.btn_play.setToolTip("Пауза" if playing else "Слушать запись")
