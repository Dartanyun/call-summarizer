"""Главное окно."""

import bisect
import os
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import QEvent, QFile, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPushButton, QScrollArea, QStackedWidget, QSystemTrayIcon, QTextBrowser, QTextEdit, QToolButton,
    QVBoxLayout, QWidget,
)

from . import (
    audio, callwatch, config, digest, export, llm, render, sections, speakers, stats, storage, templates, theme, voices,
)
from .ask_view import AskPage, DigestPage
from .pipeline import Pipeline
from .notify import Notifier
from .player import PlayerBar
from .settings_dialog import SettingsDialog
from .speaker_dialog import SpeakerDialog
from .widgets import (
    ROLE_CHIP, ROLE_ID, ROLE_KIND, ROLE_PROGRESS, ROLE_SUB, ROLE_TITLE, Banner, CallDelegate,
    FlowLayout, LevelMeter, PulseDot, Segmented, Toast, icon_button, refresh_icons, set_icon,
)

MEDIA_FILTER = (
    "Аудио и видео (*.mp3 *.m4a *.ogg *.oga *.opus *.wav *.flac *.aac *.wma *.webm "
    "*.mp4 *.mkv *.mov *.avi);;Все файлы (*)"
)

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

BUSY_CHIPS = {"queued": "в очереди", "importing": "импорт", "transcribing": "распознаётся",
              "diarizing": "говорящие…", "summarizing": "саммари…"}

# отметку «важный момент» ставим чуть раньше нажатия: обычно жмут, уже услышав важное
BOOKMARK_LEAD = 3.0


def fmt_duration(sec: float) -> str:
    s = int(sec)
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def day_label(d: date) -> str:
    today = date.today()
    if d == today:
        return "Сегодня"
    if d == today - timedelta(days=1):
        return "Вчера"
    return f"{d.day} {MONTHS[d.month - 1]}" + (f" {d.year}" if d.year != today.year else "")


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def tabular(font: QFont) -> QFont:
    """Цифры одинаковой ширины, чтобы таймер не «прыгал»."""
    try:
        font.setFeature(QFont.Tag("tnum"), 1)
    except (AttributeError, TypeError):
        pass
    return font


class MainWindow(QMainWindow):
    def __init__(self, app_icon: QIcon):
        super().__init__()
        self.app_icon = app_icon
        self.cfg = config.load()
        self.calls: dict[str, storage.Call] = {}
        self.recorder: audio.Recorder | None = None
        self.recording_call: storage.Call | None = None
        self.live_summary: dict[str, str] = {}
        self.status_text: dict[str, tuple[str, float]] = {}
        self._rendered: dict[str, tuple] = {}
        self._themed_pixmaps: list[tuple[QLabel, str, str, int]] = []
        self._auto_recording = False   # запись начата автоматически — и остановится сама
        self._offered_app: str | None = None
        self._tray_click: tuple[str, str] = ("show", "")
        self._tray_state = None
        self._quitting = False
        self.hotkey = None
        self.hotkey_label: str | None = None if sys.platform == "win32" else "Meta+Shift+Z"
        self.bookmark_hotkey = None
        self.bookmark_hotkey_label: str | None = None if sys.platform == "win32" else "Meta+Shift+B"
        self.view_mode = "calls"  # calls | ask | digest — что показано справа
        self.help_window = None
        self._timeline_key = None
        self._search_hits: dict[str, bool] = {}

        self.pipeline = Pipeline(lambda: dict(self.cfg))
        self.pipeline.status.connect(self.on_status)
        self.pipeline.summary_chunk.connect(self.on_summary_chunk)
        self.pipeline.stage_done.connect(self.on_stage_done)
        self.pipeline.failed.connect(self.on_failed)
        self.pipeline.idle.connect(self.update_tray)

        self.setWindowTitle(config.APP_NAME)
        self.setWindowIcon(app_icon)
        avail = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1240, int(avail.width() * 0.92)), min(800, int(avail.height() * 0.92)))
        self.setMinimumSize(860, 540)
        self._build()
        self._build_tray()
        self._build_shortcuts()

        self.rec_timer = QTimer(self, interval=100, timeout=self.on_rec_tick)
        self.render_timer = QTimer(self, interval=120, singleShot=True, timeout=self.render_live_summary)
        self.search_timer = QTimer(self, interval=250, singleShot=True, timeout=lambda: self.reload_list())
        self.cleanup_timer = QTimer(self, interval=6 * 3600 * 1000, timeout=self.run_cleanup)
        self.follow_timer = QTimer(self, interval=3000, timeout=self._follow_devices)
        self._follow_misses = 0
        self.cleanup_timer.start()
        # итоги недели по расписанию: проверяем раз в 10 минут (и вскоре после запуска)
        self.digest_timer = QTimer(self, interval=10 * 60 * 1000, timeout=self.check_digest)
        self.digest_timer.start()
        QTimer.singleShot(90 * 1000, self.check_digest)
        QApplication.styleHints().colorSchemeChanged.connect(self.on_scheme_changed)

        self.notifier = Notifier(self) if Notifier.available() else None
        if self.notifier:
            self.notifier.action.connect(self.on_notification_action)

        self.watcher = callwatch.CallWatcher([], self)
        self.watcher.call_started.connect(self.on_call_started)
        self.watcher.call_ended.connect(self.on_call_ended)

        self._install_conflict = None
        QTimer.singleShot(1500, self.check_installs)
        from .update_view import Checker

        self.release = None          # найденная новая версия
        self._update_notified = ""
        self.update_checker = Checker(self)
        self.update_checker.found.connect(self.on_update_found)
        self.update_timer = QTimer(self, interval=6 * 3600 * 1000, timeout=self.check_updates)
        self.update_timer.start()
        QTimer.singleShot(60 * 1000, self.check_updates)
        self.recover_after_crash()
        self.reload_list()
        self.apply_silent_settings()
        self.run_cleanup()
        self.update_tray()

    # ================= построение интерфейса =================

    def _themed_pixmap(self, label: QLabel, name: str, color: str, size: int):
        """Картинка-иконка, которая перекрашивается при смене темы."""
        self._themed_pixmaps.append((label, name, color, size))
        label.setPixmap(theme.pixmap(name, theme.C[color], size))
        label.setFixedSize(size, size)

    def _build(self):
        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self._build_sidebar())
        self.stack = QStackedWidget()
        self.page_welcome = self._build_welcome()
        self.page_welcome.installEventFilter(self)
        self.page_call = self._build_call_page()
        self.page_rec = self._build_recording_page()
        get_calls = lambda: list(self.calls.values())  # noqa: E731
        get_cfg = lambda: dict(self.cfg)  # noqa: E731
        self.page_ask = AskPage(get_calls, get_cfg, self.open_call_at, lambda: self.open_settings("Саммари"))
        self.page_digest = DigestPage(get_calls, get_cfg, self.open_call_at, lambda: self.open_settings("Саммари"))
        self.page_digest.ready.connect(self.on_digest_ready)
        for page in (self.page_welcome, self.page_call, self.page_rec, self.page_ask, self.page_digest):
            self.stack.addWidget(page)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.toast = Toast(root)

    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(316)
        v = QVBoxLayout(side)
        v.setContentsMargins(14, 16, 14, 12)
        v.setSpacing(10)

        top = QHBoxLayout()
        top.setContentsMargins(4, 0, 0, 0)
        logo = QLabel()
        logo.setPixmap(self.app_icon.pixmap(QSize(26, 26), 2.0))
        logo.setFixedSize(26, 26)
        title = QLabel(config.APP_NAME)
        title.setObjectName("AppTitle")
        top.addWidget(logo)
        top.addSpacing(6)
        top.addWidget(title)
        top.addStretch()
        top.addWidget(icon_button("help", "Справка (F1)", lambda: self.show_help()))
        top.addWidget(icon_button("settings", "Настройки (Ctrl+,)", lambda: self.open_settings()))
        v.addLayout(top)
        v.addSpacing(6)

        self.btn_record = QPushButton("Начать запись")
        self.btn_record.setObjectName("Record")
        self.btn_record.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_record.setToolTip("Ctrl+R")
        set_icon(self.btn_record, "record", "on_accent")
        self.btn_record.clicked.connect(self.toggle_recording)
        v.addWidget(self.btn_record)

        self.btn_import = QPushButton("Открыть запись…")
        self.btn_import.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_import.setToolTip("Аудио или видео со звонком (Ctrl+O)")
        set_icon(self.btn_import, "upload")
        self.btn_import.clicked.connect(self.import_file)
        v.addWidget(self.btn_import)
        nav = QHBoxLayout()
        nav.setSpacing(8)
        self.btn_ask = QPushButton("Спросить")
        self.btn_ask.setToolTip("Вопрос по всем созвонам: что решили, кто что обещал (Ctrl+K)")
        set_icon(self.btn_ask, "chat")
        self.btn_ask.clicked.connect(self.show_ask)
        self.btn_digest = QPushButton("Итоги недели")
        self.btn_digest.setToolTip("Главное, решения и задачи за неделю по всем созвонам")
        set_icon(self.btn_digest, "digest")
        self.btn_digest.clicked.connect(lambda: self.show_digest())
        for b in (self.btn_ask, self.btn_digest):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setCheckable(True)
            b.setObjectName("NavButton")
            nav.addWidget(b, 1)
        v.addLayout(nav)
        v.addSpacing(6)

        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("Поиск по созвонам")
        self.search.setClearButtonEnabled(True)
        self.search_icon = self.search.addAction(theme.icon("search", theme.C["faint"]),
                                                 QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        v.addWidget(self.search)

        self.list = QListWidget()
        self.list.setObjectName("CallList")
        self.list.setItemDelegate(CallDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setUniformItemSizes(False)
        self.list.currentItemChanged.connect(self._on_list_changed)
        self.list.itemPressed.connect(lambda _item: self._set_mode("calls"))
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.list_menu)
        v.addWidget(self.list, 1)

        self.footer = QLabel()
        self.footer.setObjectName("Faint")
        self.footer.setContentsMargins(6, 0, 0, 0)
        foot = QHBoxLayout()
        foot.addWidget(self.footer)
        foot.addStretch()
        self.btn_update = QPushButton()
        self.btn_update.setObjectName("Link")
        self.btn_update.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_update.clicked.connect(lambda: self.open_update())
        self.btn_update.hide()
        foot.addWidget(self.btn_update)
        v.addLayout(foot)
        return side

    def _build_welcome(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(2)
        # колонка фиксированной ширины: так Qt правильно считает высоту текста с переносами
        column = QWidget()
        column.setFixedWidth(744)
        self.welcome_column = column
        col = QVBoxLayout(column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)

        logo = QLabel()
        logo.setPixmap(self.app_icon.pixmap(QSize(68, 68), 2.0))
        col.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)
        hero = QLabel("Записывайте созвоны —\nсаммари сделается само")
        hero.setObjectName("Hero")
        hero.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(hero)
        sub = QLabel("Приложение записывает ваш микрофон и голоса собеседников из любого мессенджера, "
                     "распознаёт речь прямо на компьютере, а Claude или локальная модель выделяют главное.")
        sub.setProperty("muted", True)
        sub.setWordWrap(True)
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setContentsMargins(60, 0, 60, 0)
        col.addWidget(sub)
        col.addSpacing(14)

        steps = QHBoxLayout()
        steps.setSpacing(14)
        for icon_name, head, text in (
            ("mic", "Запишите", "Zoom, Telegram, Meet, Discord — звук берётся прямо из системы."),
            ("text", "Распознайте", "Локально, через Whisper. Аудио не покидает компьютер."),
            ("sparkles", "Получите саммари", "Темы, решения, задачи с исполнителями и открытые вопросы."),
        ):
            card = QFrame()
            card.setObjectName("Card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(18, 18, 18, 18)
            cl.setSpacing(8)
            bubble = QFrame()
            bubble.setObjectName("StepIcon")
            bubble.setFixedSize(44, 44)
            bl = QVBoxLayout(bubble)
            bl.setContentsMargins(0, 0, 0, 0)
            ic = QLabel()
            self._themed_pixmap(ic, icon_name, "accent", 22)
            bl.addWidget(ic, 0, Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(bubble)
            hl = QLabel(head)
            hl.setStyleSheet("font-weight: 700; font-size: 11pt;")
            cl.addWidget(hl)
            tl = QLabel(text)
            tl.setProperty("muted", True)
            tl.setWordWrap(True)
            cl.addWidget(tl)
            cl.addStretch()
            steps.addWidget(card, 1)
        col.addLayout(steps)
        col.addSpacing(18)

        cta = QHBoxLayout()
        cta.addStretch()
        b = QPushButton("Начать запись")
        b.setObjectName("Record")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumWidth(200)
        set_icon(b, "record", "on_accent")
        b.clicked.connect(self.toggle_recording)
        cta.addWidget(b)
        cta.addSpacing(8)
        b2 = QPushButton("Открыть запись…")
        b2.setCursor(Qt.CursorShape.PointingHandCursor)
        b2.setMinimumHeight(b.sizeHint().height())
        set_icon(b2, "upload")
        b2.clicked.connect(self.import_file)
        cta.addWidget(b2)
        cta.addStretch()
        col.addLayout(cta)

        how = QPushButton("Как пользоваться программой")
        how.setObjectName("Link")
        how.setCursor(Qt.CursorShape.PointingHandCursor)
        how.clicked.connect(lambda: self.show_help())
        col.addWidget(how, 0, Qt.AlignmentFlag.AlignHCenter)

        self.welcome_banner = Banner()
        col.addSpacing(10)
        col.addWidget(self.welcome_banner)

        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(column)
        row.addStretch()
        outer.addLayout(row)
        outer.addStretch(3)
        # в невысоком окне приветствие прокручивается, а не сминается
        scroll = QScrollArea()
        scroll.setObjectName("Plain")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _meta_item(self, icon_name: str) -> tuple[QWidget, QLabel]:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        ic = QLabel()
        self._themed_pixmap(ic, icon_name, "faint", 15)
        text = QLabel()
        text.setProperty("muted", True)
        lay.addWidget(ic)
        lay.addWidget(text)
        return w, text

    def _build_call_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(34, 26, 34, 24)
        v.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(6)
        tcol = QVBoxLayout()
        tcol.setSpacing(8)
        self.title_label = QLabel()
        self.title_label.setObjectName("H1")
        self.title_label.setWordWrap(True)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        tcol.addWidget(self.title_label)
        meta = QHBoxLayout()
        meta.setSpacing(18)
        self.meta_date_w, self.meta_date = self._meta_item("calendar")
        self.meta_dur_w, self.meta_dur = self._meta_item("clock")
        self.meta_src_w, self.meta_src = self._meta_item("file")
        self.meta_people_w, self.meta_people = self._meta_item("users")
        self.meta_audio_w, self.meta_audio = self._meta_item("trash")
        self.meta_audio.setText("аудио удалено")
        for w in (self.meta_date_w, self.meta_dur_w, self.meta_people_w, self.meta_src_w, self.meta_audio_w):
            meta.addWidget(w)
        meta.addStretch()
        tcol.addLayout(meta)
        header.addLayout(tcol, 1)

        self.btn_primary = QPushButton()
        self.btn_primary.setObjectName("Primary")
        self.btn_primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_primary.clicked.connect(self.on_primary)
        header.addWidget(self.btn_primary, 0, Qt.AlignmentFlag.AlignTop)
        header.addSpacing(6)
        self.btn_copy = icon_button("copy", "Скопировать саммари (Ctrl+Shift+C)", self.copy_current)
        self.btn_export = icon_button("download", "Экспорт: PDF, Word, Markdown, Telegram, Obsidian")
        self.btn_export.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.export_menu = QMenu(self)
        self.export_menu.aboutToShow.connect(self._fill_export_menu)
        self.btn_export.setMenu(self.export_menu)
        self.btn_tasks = icon_button("check", "Задачи — в YouTrack, Todoist или напоминания в календарь", self.open_tasks)
        self.btn_folder = icon_button("folder", "Открыть папку с записью", self.open_folder)
        self.btn_more = icon_button("more", "Ещё")
        self.btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QMenu(self)
        self.more_menu.aboutToShow.connect(self._fill_more_menu)
        self.btn_more.setMenu(self.more_menu)
        for b in (self.btn_copy, self.btn_export, self.btn_tasks, self.btn_folder, self.btn_more):
            header.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(header)

        self.banner = Banner()
        v.addWidget(self.banner)

        self.people = QWidget()
        pl = QHBoxLayout(self.people)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(12)
        people_label = QLabel("Участники")
        people_label.setObjectName("Faint")
        people_label.setFixedHeight(28)
        pl.addWidget(people_label, 0, Qt.AlignmentFlag.AlignTop)
        chips_box = QWidget()
        self.chips = FlowLayout(chips_box, spacing=8)
        pl.addWidget(chips_box, 1)
        self._chips_key = None
        v.addWidget(self.people)

        segrow = QHBoxLayout()
        self.seg = Segmented(["Саммари", "Транскрипт", "Статистика"], self.on_tab)
        segrow.addWidget(self.seg)
        segrow.addStretch()
        self.doc_info = QLabel()
        self.doc_info.setObjectName("Faint")
        segrow.addWidget(self.doc_info)
        v.addLayout(segrow)

        self.docs = QStackedWidget()
        self.summary_view = QTextBrowser()
        self.transcript_view = QTextBrowser()
        self.stats_view = QTextBrowser()
        for view in (self.summary_view, self.transcript_view, self.stats_view):
            view.setObjectName("Doc")
            view.setOpenLinks(False)  # таймкоды обрабатываем сами
            view.anchorClicked.connect(self.on_anchor)
            view.viewport().setAutoFillBackground(False)
            self.docs.addWidget(view)
        self.transcript_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.transcript_view.customContextMenuRequested.connect(self.transcript_menu)
        v.addWidget(self.docs, 1)

        self.player = PlayerBar()
        self.player.position.connect(self.on_play_position)
        self.player.timeline.bookmark_menu.connect(self.bookmark_menu)
        self._active_para = -1
        self._para_starts: list[float] = []
        v.addWidget(self.player)
        return page

    def _build_recording_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addStretch(2)
        col = QVBoxLayout()
        col.setSpacing(10)
        col.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.pulse = PulseDot(92)
        col.addWidget(self.pulse, 0, Qt.AlignmentFlag.AlignHCenter)
        self.rec_head = QLabel("Идёт запись")
        self.rec_head.setObjectName("H2")
        col.addWidget(self.rec_head, 0, Qt.AlignmentFlag.AlignHCenter)
        self.timer_label = QLabel("0:00")
        self.timer_label.setObjectName("Timer")
        self.timer_label.setFont(tabular(self.timer_label.font()))
        col.addWidget(self.timer_label, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addSpacing(12)

        card = QFrame()
        card.setObjectName("Card")
        card.setFixedWidth(440)
        grid = QGridLayout(card)
        grid.setContentsMargins(22, 18, 22, 18)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(16)
        self.meters: dict[str, tuple[QWidget, ...]] = {}
        for row, (key, icon_name, label) in enumerate(
            (("mic", "mic", "Микрофон"), ("system", "speaker", "Собеседники"))
        ):
            ic = QLabel()
            self._themed_pixmap(ic, icon_name, "muted", 18)
            lb = QLabel(label)
            lb.setMinimumWidth(96)
            meter = LevelMeter()
            grid.addWidget(ic, row, 0)
            grid.addWidget(lb, row, 1)
            grid.addWidget(meter, row, 2)
            self.meters[key] = (ic, lb, meter)
        col.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)

        hint = QLabel("Окно можно свернуть — запись продолжится.\nОстановить её можно и из значка в системном трее.\n"
                      "Прозвучало важное? «Отметить момент» — Ctrl+B"
                      + (f" или {self.bookmark_hotkey_label} из любого окна." if self.bookmark_hotkey_label else "."))
        self.rec_hint = hint
        hint.setWordWrap(True)
        hint.setMaximumWidth(520)
        hint.setProperty("muted", True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addSpacing(6)
        col.addWidget(hint, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addSpacing(10)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.btn_pause = QPushButton("Пауза")
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pause.setMinimumHeight(44)
        self.btn_pause.setMinimumWidth(120)
        self.btn_pause.setToolTip("Ctrl+P")
        set_icon(self.btn_pause, "pause")
        self.btn_pause.clicked.connect(self.toggle_pause)
        stop = QPushButton("Остановить запись")
        stop.setObjectName("Record")
        stop.setCursor(Qt.CursorShape.PointingHandCursor)
        stop.setMinimumWidth(200)
        set_icon(stop, "stop", "on_accent")
        stop.clicked.connect(lambda: self.stop_recording())
        self.btn_mark = QPushButton("Отметить момент")
        self.btn_mark.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mark.setMinimumHeight(44)
        self.btn_mark.setToolTip("Ctrl+B — важный момент попадёт в саммари и на шкалу плеера")
        set_icon(self.btn_mark, "flag")
        self.btn_mark.clicked.connect(self.add_bookmark)
        buttons.addStretch()
        buttons.addWidget(self.btn_mark)
        buttons.addWidget(self.btn_pause)
        buttons.addWidget(stop)
        buttons.addStretch()
        col.addLayout(buttons)
        self.marks_label = QLabel()
        self.marks_label.setObjectName("Faint")
        col.addSpacing(4)
        col.addWidget(self.marks_label, 0, Qt.AlignmentFlag.AlignHCenter)

        outer.addLayout(col)
        outer.addStretch(3)
        return page

    def _build_tray(self):
        self.tray = QSystemTrayIcon(theme.tray_icon("idle"), self)
        menu = QMenu()
        self.tray_offer = menu.addAction("Записать звонок", self._accept_offer)
        self.tray_offer.setVisible(False)
        self.tray_rec = menu.addAction("Начать запись", self.toggle_recording)
        self.tray_pause = menu.addAction("Пауза", self.toggle_pause)
        self.tray_pause.setVisible(False)
        self.tray_mark = menu.addAction("Отметить важный момент", self.add_bookmark)
        self.tray_mark.setVisible(False)
        menu.addAction("Показать окно", self.show_window)
        menu.addSeparator()
        menu.addAction("Выход", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip(config.APP_NAME)
        self.tray.activated.connect(
            lambda r: self.toggle_window() if r == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.messageClicked.connect(self.on_tray_message_clicked)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def setup_hotkey(self, app):
        """Общесистемная горячая клавиша. В Linux её назначает KDE по ярлыку (см. install.sh),
        в Windows регистрируем сами."""
        if sys.platform != "win32":
            return
        try:
            from .hotkey_win import GlobalHotkey

            from .hotkey_win import BOOKMARK_CANDIDATES, BOOKMARK_ID

            self.hotkey = GlobalHotkey(app, int(self.winId()), self.toggle_recording)
            self.hotkey_label = self.hotkey.label  # None — не удалось; причина в self.hotkey.error
            self.bookmark_hotkey = GlobalHotkey(app, int(self.winId()), self.add_bookmark,
                                                BOOKMARK_CANDIDATES, BOOKMARK_ID)
            self.bookmark_hotkey_label = self.bookmark_hotkey.label
        except (OSError, AttributeError, ImportError):
            self.hotkey, self.hotkey_label = None, None
        if self.hotkey_label:
            self.btn_record.setToolTip(f"Ctrl+R или {self.hotkey_label} из любого окна")
        self.rec_hint.setText(self.rec_hint.text().split("\nПрозвучало")[0] + "\nПрозвучало важное? «Отметить момент» — "
                              "Ctrl+B" + (f" или {self.bookmark_hotkey_label} из любого окна." if self.bookmark_hotkey_label
                                          else "."))

    def tray_available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable() and self.tray.isVisible()

    def update_tray(self, force: bool = False):
        """Значок в трее показывает, чем занято приложение: ожидание, запись, пауза, обработка."""
        if self.recorder:
            state = "paused" if self.recorder.paused else "recording"
            word = "Запись на паузе" if self.recorder.paused else "Идёт запись"
            tip = f"{word} · {fmt_duration(self.recorder.elapsed)}"
        elif self.pipeline.is_busy():
            state = "processing"
            cid = self.pipeline.current
            call = self.calls.get(cid) if cid else None
            text, frac = self.status_text.get(cid, ("Обработка…", -1.0)) if cid else ("В очереди…", -1.0)
            pct = f" {int(frac * 100)}%" if frac >= 0 else ""
            tip = f"{text}{pct}" + (f"\n{call.title}" if call else "")
        else:
            state, tip = "idle", "Ожидание"
        if state != self._tray_state or force:
            self._tray_state = state
            self.tray.setIcon(theme.tray_icon(state))
        self.tray.setToolTip(f"{config.APP_NAME}\n{tip}")

    def handle_command(self, command: str):
        """Команды от второго запуска программы: show — показать окно, toggle — запись,
        bookmark — отметить важный момент (горячие клавиши KDE)."""
        if command == "toggle":
            self.toggle_recording()
        elif command == "bookmark":
            self.add_bookmark()
        else:
            self.show_window()

    def toggle_window(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_window()

    def _build_shortcuts(self):
        for keys, slot in (
            ("Ctrl+R", self.toggle_recording),
            ("Ctrl+P", self.toggle_pause),
            ("Ctrl+O", self.import_file),
            ("Ctrl+F", lambda: (self.search.setFocus(), self.search.selectAll())),
            ("Ctrl+,", lambda: self.open_settings()),
            ("Ctrl+Shift+C", self.copy_current),
            ("Ctrl+B", self.add_bookmark),
            ("Ctrl+K", self.show_ask),
            ("F1", lambda: self.show_help()),
            ("Delete", self.delete_current),
        ):
            QShortcut(QKeySequence(keys), self, activated=slot)

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ================= тема =================

    def on_scheme_changed(self, *_):
        if self.cfg.get("theme", "system") == "system":
            self.apply_theme()

    def apply_theme(self):
        theme.apply(QApplication.instance(), self.cfg.get("theme", "system"))
        refresh_icons(self)
        for label, name, color, size in self._themed_pixmaps:
            label.setPixmap(theme.pixmap(name, theme.C[color], size))
        self.search_icon.setIcon(theme.icon("search", theme.C["faint"]))
        self.banner.refresh_theme()
        self.player.refresh_theme()
        self._timeline_key = None
        self.update_tray(force=True)
        self.welcome_banner.refresh_theme()
        for page in (self.page_ask, self.page_digest):
            page.banner.refresh_theme()
        self._rendered.clear()
        self.list.viewport().update()
        self.show_current()

    # ================= список созвонов =================

    def recover_after_crash(self):
        # если приложение закрылось посреди работы, возвращаем созвонам понятный статус
        for call in storage.list_calls():
            if call.status in ("recording", "queued", "importing", "transcribing", "summarizing"):
                if call.summary():
                    call.save(status="done")
                elif call.has_transcript():
                    call.save(status="transcribed")
                elif call.tracks():
                    call.save(status="recorded")
                else:
                    call.save(status="error", error="Обработка была прервана.")

    def reload_list(self, select: str | None = None):
        select = select or self.current_id()
        fresh = storage.list_calls()
        # сохраняем те же объекты Call, с которыми работает фоновая очередь
        self.calls = {c.id: self.calls.get(c.id, c) for c in fresh}
        query = self.search.text().strip().lower()

        self.list.blockSignals(True)
        self.list.clear()
        group = None
        shown = 0
        self._search_hits: dict[str, bool] = {}
        for call in self.calls.values():
            snippet = None
            if query and query not in call.title.lower() and query not in call.summary().lower():
                snippet = self._transcript_snippet(call, query)
                if snippet is None:
                    continue
                self._search_hits[call.id] = True
            day = day_label(call.created.date())
            if day != group:
                header = QListWidgetItem(day)
                header.setData(ROLE_KIND, "header")
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                self.list.addItem(header)
                group = day
            item = QListWidgetItem()
            item.setData(ROLE_KIND, "call")
            item.setData(ROLE_ID, call.id)
            self._fill_item(item, call)
            if snippet:
                item.setData(ROLE_SUB, snippet)  # вместо времени — где нашлось
            self.list.addItem(item)
            shown += 1
            if call.id == select:
                self.list.setCurrentItem(item)
        if query and not shown:
            empty = QListWidgetItem("Ничего не найдено")
            empty.setData(ROLE_KIND, "header")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(empty)
        if self.list.currentItem() is None:
            for i in range(self.list.count()):
                if self.list.item(i).data(ROLE_KIND) == "call":
                    self.list.setCurrentRow(i)
                    break
        self.list.blockSignals(False)

        n = len(self.calls)
        self.footer.setText(f"{n} {plural(n, 'созвон', 'созвона', 'созвонов')}" if n else "")
        self.show_current()

    @staticmethod
    def _transcript_snippet(call: storage.Call, query: str) -> str | None:
        """Фрагмент транскрипта с найденным словом: «00:12:34 · …оплате картой…»."""
        for p in call.paragraphs():
            text = p["text"]
            i = text.lower().find(query)
            if i >= 0:
                start = max(0, i - 25)
                frag = ("…" if start else "") + text[start:i + len(query) + 45].strip() + "…"
                s = int(p["start"])
                return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d} · {frag}"
        return None

    def _apply_search_highlight(self, scroll: bool):
        """Подсвечивает найденное в транскрипте; при переходе к созвону — прокручивает к первому месту."""
        view = self.transcript_view
        query = self.search.text().strip()
        selections = []
        if query:
            doc = view.document()
            color = QColor(theme.C["amber"])
            color.setAlphaF(0.35)
            cursor = doc.find(query, 0)
            while not cursor.isNull() and len(selections) < 500:
                sel = QTextEdit.ExtraSelection()
                sel.cursor = cursor
                sel.format.setBackground(color)
                selections.append(sel)
                cursor = doc.find(query, cursor)
        view.setExtraSelections(selections)
        if scroll and selections:
            c = QTextCursor(selections[0].cursor)
            c.clearSelection()
            view.setTextCursor(c)
            # найденное место — на трети высоты, а не у самого края
            sb = view.verticalScrollBar()
            sb.setValue(max(0, sb.value() + view.cursorRect(c).top() - view.viewport().height() // 3))

    def _chip(self, call: storage.Call) -> tuple[str, str] | None:
        if call is self.recording_call:
            return ("● запись", "red")
        if self.pipeline.is_busy(call.id):
            _, frac = self.status_text.get(call.id, ("", -1.0))
            if call.status == "transcribing" and frac >= 0:
                return (f"{int(frac * 100)}%", "accent")
            return (BUSY_CHIPS.get(call.status, "обработка"), "accent")
        if call.status == "error":
            return ("ошибка", "red")
        if not call.has_transcript():
            return ("не распознан", "amber") if call.tracks() else None
        if not call.summary():
            return ("без саммари", "muted")
        return None

    def _fill_item(self, item: QListWidgetItem, call: storage.Call):
        sub = [call.created.strftime("%H:%M")]
        if call.duration:
            sub.append(fmt_duration(call.duration))
        if call.meta.get("source") == "import":
            sub.append("из файла")
        item.setData(ROLE_TITLE, call.title)
        item.setData(ROLE_SUB, " · ".join(sub))
        item.setData(ROLE_CHIP, self._chip(call))
        _, frac = self.status_text.get(call.id, ("", -1.0))
        item.setData(ROLE_PROGRESS, frac if self.pipeline.is_busy(call.id) and frac >= 0 else None)
        item.setToolTip(call.meta.get("error") or "")

    def refresh_item(self, call_id: str):
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(ROLE_ID) == call_id and call_id in self.calls:
                self._fill_item(item, self.calls[call_id])

    def _on_list_changed(self, *_):
        # список сам по себе меняется только с выключенными сигналами (reload_list), так что
        # сюда попадаем, когда созвон выбрал человек — мышью или стрелками
        if self.view_mode != "calls":
            self._set_mode("calls")
            return
        self.show_current()

    def _set_mode(self, mode: str):
        self.view_mode = mode
        self.btn_ask.setChecked(mode == "ask")
        self.btn_digest.setChecked(mode == "digest")
        self.show_current()

    def show_ask(self):
        self.show_window()
        self._set_mode("ask")
        self.page_ask.refresh()

    def show_digest(self, monday=None):
        self.show_window()
        if monday is not None:
            self.page_digest.show_week(monday)
        self._set_mode("digest")
        self.page_digest.refresh()

    def open_call_at(self, call_id: str, seconds: float | None = None):
        """Открыть созвон (из ответа на вопрос или итогов недели) и, если указано, перейти к моменту."""
        if call_id not in self.calls:
            return
        if self.search.text():
            self.search.clear()
        self.view_mode = "calls"
        self.btn_ask.setChecked(False)
        self.btn_digest.setChecked(False)
        self.reload_list(select=call_id)
        call = self.calls[call_id]
        if seconds is None:
            return
        if call.tracks():
            self.player.play_at(seconds)
        else:
            self.seg.set_index(1)
            self.on_tab(1)
            starts = [p["start"] for p in call.paragraphs()]
            if starts:
                self.transcript_view.scrollToAnchor(f"p{max(0, bisect.bisect_right(starts, seconds + 0.05) - 1)}")

    def show_help(self, topic: str | None = None):
        from .help_view import HelpWindow

        if self.help_window is None:
            self.help_window = HelpWindow(self, open_settings=lambda: self.open_settings())
        if topic:
            self.help_window.show_topic(topic)
        self.help_window.show()
        self.help_window.raise_()
        self.help_window.activateWindow()

    def current_id(self) -> str | None:
        item = self.list.currentItem()
        return item.data(ROLE_ID) if item and item.data(ROLE_KIND) == "call" else None

    def current_call(self) -> storage.Call | None:
        cid = self.current_id()
        return self.calls.get(cid) if cid else None

    # ================= правая часть =================

    def show_current(self):
        if self.view_mode == "ask":
            self.stack.setCurrentWidget(self.page_ask)
            self.page_ask.set_scope_call(self.current_call())
            return
        if self.view_mode == "digest":
            self.stack.setCurrentWidget(self.page_digest)
            return
        call = self.current_call()
        if call is None or call is self.recording_call:
            self.player.set_call(None)
            self.toast.bottom_margin = 28
        if call is None:
            self.stack.setCurrentWidget(self.page_welcome)
            if llm.ready(self.cfg):
                self.welcome_banner.hide()
            else:
                self.welcome_banner.set(
                    "warn", "key", f"Чтобы получать саммари, {llm.not_ready_text(self.cfg)}",
                    "Записывать и распознавать речь можно и без этого. Подойдёт и локальная модель — "
                    "тогда текст не уходит в интернет.",
                    button=("Открыть настройки", lambda: self.open_settings("Саммари")),
                )
            return
        if call is self.recording_call:
            self.stack.setCurrentWidget(self.page_rec)
            return
        self.stack.setCurrentWidget(self.page_call)
        if getattr(self, "_shown_call_id", None) != call.id:
            self._shown_call_id = call.id
            self._active_para = -1
        if self.player.set_call(call):
            self._timeline_key = None
        # всплывающие сообщения — над плеером, а не поверх него
        self.toast.bottom_margin = 28 + (self.player.sizeHint().height() + 24 if not self.player.isHidden() else 0)

        self.title_label.setText(call.title)
        self.meta_date.setText(call.created.strftime("%d.%m.%Y, %H:%M"))
        from . import calendar_ics

        self.meta_date_w.setToolTip(calendar_ics.note(call.meta.get("calendar") or {}))
        self.meta_dur_w.setVisible(bool(call.duration))
        self.meta_dur.setText(fmt_duration(call.duration))
        src = call.meta.get("original_file")
        self.meta_src_w.setVisible(bool(src))
        self.meta_src.setText(Path(src).name if src else "")
        people = len(set(call.speaker_names().values()))
        self.meta_people_w.setVisible(people > 1)
        self.meta_people.setText(f"{people} {plural(people, 'участник', 'участника', 'участников')}")
        self.update_people(call)
        self.meta_audio_w.setVisible(bool(call.meta.get("audio_deleted")) and not call.tracks())
        self._update_timeline(call)
        if getattr(self, "_search_call", None) != call.id:
            self._search_call = call.id
            if self._search_hits.get(call.id):
                # нашлось только в транскрипте — показываем его и прокручиваем к месту
                self.seg.set_index(1)
                self.on_tab(1)
                self._apply_search_highlight(scroll=True)

        busy = self.pipeline.is_busy(call.id)
        summary = call.summary()
        has_tr = call.has_transcript()
        if busy or call.status == "error":
            self.btn_primary.hide()
        elif not has_tr and call.tracks():
            self.btn_primary.setText("Распознать")
            set_icon(self.btn_primary, "text", "on_accent")
            self.btn_primary.show()
        elif has_tr and not summary:
            self.btn_primary.setText("Сделать саммари")
            set_icon(self.btn_primary, "sparkles", "on_accent")
            self.btn_primary.show()
        else:
            self.btn_primary.hide()
        self.btn_copy.setEnabled(bool(summary))
        self.btn_export.setEnabled(bool(summary) or has_tr)
        self.btn_tasks.setEnabled(bool(summary) and not busy and bool(call.tasks()))

        self.update_banner()
        self.update_docs()

    def update_banner(self):
        call = self.current_call()
        if not call or call is self.recording_call:
            return
        if self.pipeline.is_busy(call.id):
            text, frac = self.status_text.get(call.id, ("В очереди…", -1.0))
            pct = f"{int(frac * 100)}%" if frac >= 0 else ""
            icon_name = {"summarizing": "sparkles", "diarizing": "users"}.get(call.status, "text")
            self.banner.set("info", icon_name,
                            text, pct, progress=frac, button=("Отменить", self.cancel_current))
        elif call.status == "error":
            self.banner.set("error", "alert", "Не получилось обработать запись",
                            call.meta.get("error", ""), button=("Повторить", self.retry_current))
        elif call.has_transcript() and not call.summary() and not llm.ready(self.cfg):
            self.banner.set("warn", "key", f"Чтобы получить саммари, {llm.not_ready_text(self.cfg)}",
                            button=("Настройки", lambda: self.open_settings("Саммари")))
        else:
            self.banner.hide()

    def update_docs(self):
        call = self.current_call()
        if not call:
            return
        busy = self.pipeline.is_busy(call.id)
        # саммари
        if call.id in self.live_summary:
            self.render_live_summary()
        else:
            summary = call.summary()
            key = (call.id, summary, busy, theme.is_dark)
            if self._rendered.get("summary") != key:
                self._rendered["summary"] = key
                if summary:
                    render.show_summary(self.summary_view, summary)
                elif busy:
                    render.show_placeholder(self.summary_view, "Саммари появится здесь",
                                            f"Сначала запись распознается, затем {llm.who(self.cfg)} напишет саммари.")
                elif call.has_transcript():
                    render.show_placeholder(self.summary_view, "Саммари пока нет",
                                            "Нажмите «Сделать саммари» вверху справа.")
                else:
                    render.show_placeholder(self.summary_view, "Саммари пока нет",
                                            "Сначала нужно распознать запись.")
        # транскрипт
        transcript = call.transcript()
        self._render_transcript(call, transcript)
        if self.docs.currentIndex() == 2:
            self._render_stats(call)
        self._update_doc_info(call, transcript)

    def _render_stats(self, call: storage.Call):
        names = call.speaker_names()
        narrow = self.stats_view.viewport().width() < 820
        key = (call.id, len(call.paragraphs()), tuple(names.items()), theme.is_dark, narrow)
        if self._rendered.get("stats") == key:
            return
        self._rendered["stats"] = key
        st = stats.compute(call.paragraphs(), names, call.duration)
        if st is None:
            render.show_placeholder(self.stats_view, "Статистики пока нет",
                                    "Она появится, когда запись распознается и будут определены говорящие.")
            return
        by_sid = theme.speaker_colors(names)
        colors = {}
        for sid, name in names.items():
            colors.setdefault(name, by_sid[sid])
        render.show_stats(self.stats_view, st, colors, narrow)

    def _render_transcript(self, call: storage.Call, transcript: str):
        active = self._active_para if self.player.is_playing() or self._active_para >= 0 else -1
        key = (call.id, transcript, theme.is_dark, active)
        if self._rendered.get("transcript") != key:
            same_call = (self._rendered.get("transcript") or (None,))[0] == call.id
            self._rendered["transcript"] = key
            paragraphs = call.paragraphs()
            self._para_starts = [p["start"] for p in paragraphs]
            if transcript:
                sb = self.transcript_view.verticalScrollBar()
                pos = sb.value()
                render.show_transcript(self.transcript_view, paragraphs, call.speaker_names(), active)
                if same_call:
                    sb.setValue(pos)  # подсветка не должна сбивать прокрутку
                self._apply_search_highlight(scroll=False)
            else:
                render.show_placeholder(self.transcript_view, "Транскрипта пока нет",
                                        "Он появится после распознавания речи.")

    def _update_doc_info(self, call: storage.Call, transcript: str):
        if self.docs.currentIndex() == 1 and transcript:
            words = len(transcript.split())
            self.doc_info.setText(f"{words} {plural(words, 'слово', 'слова', 'слов')}")
        elif self.docs.currentIndex() == 0 and call.summary():
            model = call.meta.get("summary_label") or dict(config.CLAUDE_MODELS).get(
                call.meta.get("summary_model", ""), "").split(" — ")[0]
            kind = templates.NAMES.get(call.meta.get("template", ""), "")
            self.doc_info.setText(" · ".join(x for x in (kind, model) if x))
        else:
            self.doc_info.setText("")

    def on_tab(self, index: int):
        self.docs.setCurrentIndex(index)
        call = self.current_call()
        if call:
            if index == 1:
                self._render_transcript(call, call.transcript())
            elif index == 2:
                self._render_stats(call)
            self._update_doc_info(call, call.transcript())

    # ---------- плеер и таймкоды ----------

    def _update_timeline(self, call: storage.Call):
        """Цветная полоса говорящих, ромбики пунктов саммари, главы и отметки в плеере."""
        paragraphs = call.paragraphs()
        names = call.speaker_names()
        summary = call.summary()
        marks = call.bookmarks()
        key = (call.id, len(paragraphs), tuple(names.items()), hash(summary), theme.is_dark,
               tuple((b["t"], b.get("note", "")) for b in marks))
        if key == self._timeline_key:
            return
        self._timeline_key = key
        colors = theme.speaker_colors(names)
        segments = [(p["start"], p["end"], colors.get(p.get("speaker"), theme.C["accent"]),
                     names.get(p.get("speaker"), "")) for p in paragraphs if p.get("speaker")]
        markers = []
        body = sections.without_section(render.strip_title(summary), sections.CHAPTER_HEADINGS)
        for line in body.splitlines():
            for m in render._TIMECODE.finditer(line):
                text = render._TIMECODE.sub("", line)
                text = text.lstrip("-*0123456789. ").removeprefix("[ ]").removeprefix("[x]").strip(" —-*_")
                markers.append((render._seconds(m.group(1)), text[:90]))
        self.player.set_timeline(segments, markers, [(b["t"], b.get("note", "")) for b in marks], call.chapters())

    def on_anchor(self, url: QUrl):
        if url.scheme() == "t":
            try:
                seconds = float(url.path())
            except ValueError:
                return
            self.player.play_at(seconds)
        elif url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)

    def on_play_position(self, seconds: float):
        if not self._para_starts:
            return
        idx = max(0, bisect.bisect_right(self._para_starts, seconds + 0.05) - 1)
        if idx == self._active_para:
            return
        self._active_para = idx
        call = self.current_call()
        if call and self.docs.currentIndex() == 1:
            self._render_transcript(call, call.transcript())
            # следим за звучащей репликой, только если она ушла за край экрана
            sb = self.transcript_view.verticalScrollBar()
            view_h = self.transcript_view.viewport().height()
            before = sb.value()
            self.transcript_view.scrollToAnchor(f"p{idx}")
            target = sb.value()
            if before <= target <= before + view_h * 0.7:
                sb.setValue(before)
            else:
                sb.setValue(max(0, target - view_h // 3))

    # ---------- исправления ----------

    def transcript_menu(self, pos):
        view = self.transcript_view
        menu = view.createStandardContextMenu(pos)
        selected = view.textCursor().selectedText().strip()
        call = self.current_call()
        if call and selected and len(selected) <= 60 and "\u2029" not in selected:
            menu.insertSeparator(menu.actions()[0] if menu.actions() else None)
            act = menu.addAction(theme.icon("pencil", theme.C["muted"]), f"Исправить «{selected}»…")
            menu.insertAction(menu.actions()[0], act)
            act.triggered.connect(lambda: self.correct_word(selected))
        menu.exec(view.viewport().mapToGlobal(pos))

    def correct_word(self, wrong: str):
        call = self.current_call()
        if not call or self.pipeline.is_busy(call.id):
            return
        right, ok = QInputDialog.getText(
            self, "Исправление",
            f"Как правильно пишется «{wrong}»?\nИсправится во всём транскрипте этого созвона, "
            "а слово попадёт в словарь — следующие записи будут распознаваться точнее.",
            text=wrong,
        )
        right = right.strip()
        if not ok or not right or right == wrong:
            return
        call.add_correction(wrong, right)
        if config.add_vocabulary_term(self.cfg, right):
            config.save(self.cfg)
        self._rendered.clear()
        self.show_current()
        self.toast.show_message(f"«{wrong}» → «{right}». Слово добавлено в словарь.")

    def on_primary(self):
        call = self.current_call()
        if not call:
            return
        if call.has_transcript():
            self.run_stages(["summarize"])
        else:
            self.run_stages(["transcribe", "speakers", "summarize"])

    def retry_current(self):
        call = self.current_call()
        if not call:
            return
        if call.has_transcript():
            self.run_stages(["summarize"])
        elif call.tracks():
            self.run_stages(["transcribe", "speakers", "summarize"])
        elif call.meta.get("original_file"):
            src = Path(call.meta["original_file"])
            if src.exists():
                self.pipeline.enqueue(call, self._with_summary(["import", "transcribe", "speakers"]), src=src)
                self.refresh_item(call.id)
                self.show_current()

    def _fill_more_menu(self):
        m = self.more_menu
        m.clear()
        call = self.current_call()
        if not call:
            return
        busy = self.pipeline.is_busy(call.id)
        a = m.addAction(theme.icon("refresh", theme.C["muted"]), "Распознать заново",
                        lambda: self.run_stages(["transcribe", "speakers", "summarize"]))
        a.setEnabled(bool(call.tracks()) and not busy)
        sub = m.addMenu(theme.icon("sparkles", theme.C["muted"]), "Сделать саммари заново")
        sub.setEnabled(call.has_transcript() and not busy)
        current = call.meta.get("template_choice") or ""
        for key, name in templates.CHOICES:
            label = name if key != templates.AUTO else f"{name} (сейчас — {templates.NAMES.get(call.meta.get('template', ''), 'общий').lower()})"
            act = sub.addAction(label, lambda k=key: self.resummarize(k))
            act.setCheckable(True)
            act.setChecked(key == (current or templates.AUTO))
            if key == templates.AUTO:
                sub.addSeparator()
        a = m.addAction(theme.icon("users", theme.C["muted"]), "Указать число участников…", self.recount_speakers)
        a.setEnabled(call.has_transcript() and bool(call.tracks()) and not busy and self.cfg.get("diarize", True))
        m.addSeparator()
        if call.tracks():
            keep = m.addAction(theme.icon("folder", theme.C["muted"]), "Не удалять аудио при автоочистке")
            keep.setCheckable(True)
            keep.setChecked(bool(call.meta.get("keep_audio")))
            keep.toggled.connect(lambda on, c=call: c.save(keep_audio=on))
        m.addAction(theme.icon("pencil", theme.C["muted"]), "Переименовать…", self.rename_current)
        m.addAction(theme.icon("folder", theme.C["muted"]), "Открыть папку", self.open_folder)
        m.addSeparator()
        m.addAction(theme.icon("trash", theme.C["red"]), "Удалить в корзину", self.delete_current)

    def resummarize(self, template: str):
        call = self.current_call()
        if not call or self.pipeline.is_busy(call.id):
            return
        call.save(template_choice="" if template == templates.AUTO else template)
        self.run_stages(["summarize"])

    # ---------- экспорт и задачи ----------

    def _fill_export_menu(self):
        m = self.export_menu
        m.clear()
        call = self.current_call()
        if not call:
            return
        has_summary = bool(call.summary())
        for label, kind in (("Markdown (.md)…", "md"), ("PDF…", "pdf"), ("Word (.docx)…", "docx")):
            m.addAction(theme.icon("download", theme.C["muted"]), label, lambda k=kind: self.export_current(k))
        m.addSeparator()
        a = m.addAction(theme.icon("send", theme.C["muted"]), "Скопировать для Telegram", self.copy_telegram)
        a.setEnabled(has_summary)
        if self.cfg.get("obsidian_dir"):
            a = m.addAction(theme.icon("folder", theme.C["muted"]), "Сохранить в Obsidian", self.save_obsidian)
        else:
            a = m.addAction(theme.icon("folder", theme.C["muted"]), "Сохранить в Obsidian… (выбрать папку)",
                            lambda: self.open_settings("Интеграции"))
        a.setEnabled(has_summary)

    def copy_telegram(self):
        call = self.current_call()
        if not call or not call.summary():
            return
        text = export.telegram_text(call)
        QApplication.clipboard().setText(text)
        tail = (f" Внимание: {len(text)} символов — длиннее одного сообщения Telegram ({export.TELEGRAM_LIMIT})."
                if len(text) > export.TELEGRAM_LIMIT else "")
        self.toast.show_message("Скопировано для Telegram — вставьте в чат." + tail, 6000 if tail else 3500)

    def save_obsidian(self):
        call = self.current_call()
        if not call:
            return
        try:
            path = export.save_obsidian(call, Path(self.cfg["obsidian_dir"]).expanduser())
        except OSError as e:
            self.toast.show_message(f"Не удалось сохранить в Obsidian: {e}", 6000, error=True)
            return
        self.toast.show_message(f"Заметка в Obsidian: {path.name}")

    def open_tasks(self):
        from .tasks_dialog import TasksDialog

        call = self.current_call()
        if not call or not call.tasks():
            self.toast.show_message("В саммари этого созвона нет задач.")
            return
        TasksDialog(self, call, dict(self.cfg), lambda: self.open_settings("Интеграции")).exec()

    # ---------- отметки «важный момент» ----------

    def add_bookmark(self):
        """Во время записи — отметка в идущем созвоне; при прослушивании — там, где сейчас плеер."""
        if self.recorder and self.recording_call:
            mark = self.recording_call.add_bookmark(self.recorder.elapsed - BOOKMARK_LEAD)
            self._update_marks_label()
            when = fmt_duration(mark["t"])
            if self.isActiveWindow():
                self.toast.show_message(f"Момент {when} отмечен — он попадёт в саммари.")
            else:
                self.notify("Момент отмечен", f"{when} — попадёт в саммари.")
            return
        call = self.current_call()
        if (self.view_mode == "calls" and call and self.stack.currentWidget() is self.page_call
                and self.player.isVisible() and call.tracks()):
            mark = call.add_bookmark(self.player.current_position())
            self._timeline_key = None
            self._update_timeline(call)
            hint = " Чтобы она попала в текст, сделайте саммари заново (меню «⋯»)." if call.summary() else ""
            self.toast.show_message(f"Отметка {fmt_duration(mark['t'])} добавлена.{hint}", 5000)
        elif self.isActiveWindow():
            self.toast.show_message("Отметки ставятся во время записи или прослушивания созвона.")

    def _update_marks_label(self):
        n = len(self.recording_call.bookmarks()) if self.recording_call else 0
        self.marks_label.setText(f"⚑ Отмечено моментов: {n}" if n else "")

    def bookmark_menu(self, index: int, pos):
        call = self.current_call()
        marks = call.bookmarks() if call else []
        if not (0 <= index < len(marks)):
            return
        mark = marks[index]
        menu = QMenu(self)
        menu.addAction(theme.icon("play", theme.C["muted"]), "Слушать отсюда", lambda: self.player.play_at(mark["t"]))
        menu.addAction(theme.icon("pencil", theme.C["muted"]), "Подписать…", lambda: self._note_bookmark(call, index))
        menu.addAction(theme.icon("trash", theme.C["red"]), "Удалить отметку", lambda: self._remove_bookmark(call, index))
        menu.exec(pos)

    def _note_bookmark(self, call: storage.Call, index: int):
        marks = call.bookmarks()
        note, ok = QInputDialog.getText(self, "Отметка", f"Подпись к моменту {fmt_duration(marks[index]['t'])}:",
                                        text=marks[index].get("note", ""))
        if ok:
            marks[index]["note"] = note.strip()
            call.set_bookmarks(marks)
            self._timeline_key = None
            self._update_timeline(call)

    def _remove_bookmark(self, call: storage.Call, index: int):
        marks = call.bookmarks()
        del marks[index]
        call.set_bookmarks(marks)
        self._timeline_key = None
        self._update_timeline(call)

    # ---------- участники ----------

    def _speaker_groups(self, call: storage.Call) -> list[tuple[str, list[str]]]:
        """Участники по именам: спикеры с одинаковым именем считаются одним человеком."""
        groups: dict[str, list[str]] = {}
        for sid, name in call.speaker_names().items():
            groups.setdefault(name, []).append(sid)
        return list(groups.items())

    def update_people(self, call: storage.Call):
        names = call.speaker_names()
        busy = self.pipeline.is_busy(call.id)
        key = (call.id, tuple(names.items()), tuple(call.speaker_is_auto(s) for s in names), busy, theme.is_dark)
        if key == self._chips_key:
            return
        self._chips_key = key
        self.chips.clear()
        self.people.setVisible(bool(names) and not busy)
        if not names or busy:
            return
        colors = theme.speaker_colors(names)
        talk = speakers.talk_time(call.paragraphs())
        total = sum(talk.values()) or 1.0
        for name, sids in self._speaker_groups(call):
            b = QPushButton(name)
            b.setObjectName("Chip")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setIcon(theme.icon("dot", colors[sids[0]], 12))
            b.setIconSize(QSize(10, 10))
            auto = any(call.speaker_is_auto(s) for s in sids)
            b.setProperty("auto", auto)
            share = sum(talk.get(s, 0.0) for s in sids) / total
            tip = f"Говорит {round(share * 100)}% времени. Нажмите, чтобы указать имя."
            if auto:
                by_voice = any(call.speaker_source(s) == "voice" for s in sids)
                tip += ("\nУзнан(а) по голосу — проверьте." if by_voice
                        else "\nИмя определено по разговору — проверьте.")
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, sids=sids: self.rename_speaker_dialog(sids))
            self.chips.addWidget(b)

    def rename_speaker_dialog(self, sids: list[str]):
        call = self.current_call()
        if not call:
            return
        if self.pipeline.is_busy(call.id):
            self.toast.show_message("Подождите, пока закончится обработка.")
            return
        names = call.speaker_names()
        defaults = speakers.default_names(list(names))
        sid = sids[0]
        paras = [p for p in call.paragraphs() if p.get("speaker") in sids]
        quotes = [p["text"] for p in sorted(paras, key=lambda p: -len(p["text"]))[:2]]
        quotes = [q if len(q) <= 160 else q[:157].rsplit(" ", 1)[0] + "…" for q in quotes]
        talk = speakers.talk_time(call.paragraphs())
        share = sum(talk.get(s, 0.0) for s in sids) / (sum(talk.values()) or 1.0)
        current = names[sid] if names[sid] != defaults.get(sid) else ""
        suggestion = next((call.meta.get("speaker_suggestions", {}).get(s, "") for s in sids
                           if call.meta.get("speaker_suggestions", {}).get(s)), "")
        others = sorted({n for s, n in names.items() if s not in sids and s != speakers.ME})
        dlg = SpeakerDialog(self, names[sid], current, theme.speaker_colors(names)[sid], share,
                            quotes, suggestion, others)
        result = dlg.exec()
        if result == QDialog.DialogCode.Accepted:
            new = dlg.name()
        elif result == SpeakerDialog.RESET:
            new = ""
        else:
            return
        for s in sids:
            call.rename_speaker(s, new)
        self._remember_voice(call, sids, new)
        self._rendered.clear()
        self.refresh_item(call.id)
        self.show_current()
        self.toast.show_message("Имя обновлено в транскрипте и саммари." if call.summary()
                                else "Имя обновлено в транскрипте.")

    def _remember_voice(self, call: storage.Call, sids: list[str], name: str):
        """Имя, которое дал пользователь, запоминаем вместе с голосом — в следующий раз узнаем сами."""
        if not name or not self.cfg.get("remember_voices", True) or speakers.ME in sids:
            return
        embs = call.voice_embeddings()
        vectors = [embs[s] for s in sids if s in embs]
        if not vectors:
            return
        import numpy as np

        voices.remember(name, np.mean(np.asarray(vectors, dtype=np.float32), axis=0))

    def recount_speakers(self):
        call = self.current_call()
        if not call or self.pipeline.is_busy(call.id):
            return
        two_tracks = {"mic", "system"} <= set(call.tracks())
        question = ("Сколько собеседников было на созвоне, не считая вас?" if two_tracks
                    else "Сколько всего человек говорит на записи?")
        items = ["Определить автоматически"] + [str(i) for i in range(1, 11)]
        current = int(call.meta.get("num_speakers") or 0)
        item, ok = QInputDialog.getItem(
            self, "Участники", f"{question}\nГоворящие будут определены заново, имена сбросятся.",
            items, min(current, 10), False,
        )
        if not ok:
            return
        call.save(num_speakers=0 if item == items[0] else int(item))
        stages = ["speakers"] + (["summarize"] if call.summary() and llm.ready(self.cfg) else [])
        self.pipeline.enqueue(call, stages)
        self.refresh_item(call.id)
        self.show_current()

    def list_menu(self, pos):
        item = self.list.itemAt(pos)
        if not item or item.data(ROLE_KIND) != "call":
            return
        self.list.setCurrentItem(item)
        self._fill_more_menu()
        self.more_menu.exec(self.list.viewport().mapToGlobal(pos))

    def rename_current(self):
        call = self.current_call()
        if not call:
            return
        title, ok = QInputDialog.getText(self, "Переименовать", "Название созвона:", text=call.title)
        if ok and title.strip():
            call.save(title=title.strip(), title_manual=True)
            self.refresh_item(call.id)
            self.show_current()

    def delete_current(self):
        call = self.current_call()
        if not call or self.search.hasFocus():
            return
        if call is self.recording_call or self.pipeline.is_busy(call.id):
            self.toast.show_message("Этот созвон сейчас записывается или обрабатывается.")
            return
        if QMessageBox.question(self, "Удаление", f"Переместить «{call.title}» в корзину?") != QMessageBox.StandardButton.Yes:
            return
        res = QFile.moveToTrash(str(call.path))
        ok = res[0] if isinstance(res, tuple) else res
        if not ok:
            QMessageBox.warning(self, "Удаление", f"Не удалось переместить в корзину:\n{call.path}")
            return
        self.calls.pop(call.id, None)
        self.reload_list()
        self.toast.show_message("Созвон перемещён в корзину.")

    def open_folder(self):
        call = self.current_call()
        if call:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(call.path)))

    def copy_current(self):
        call = self.current_call()
        if call and call.summary():
            QApplication.clipboard().setText(call.summary())
            self.toast.show_message("Саммари скопировано — можно вставить в чат или заметки.")

    def export_current(self, kind: str = "md"):
        call = self.current_call()
        if not call:
            return
        filters = {"md": "Markdown (*.md)", "pdf": "PDF (*.pdf)", "docx": "Документ Word (*.docx)"}
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт", str(Path.home() / f"{export.safe_name(call.title)}.{kind}"), filters[kind])
        if not path:
            return
        target = Path(path) if Path(path).suffix else Path(path).with_suffix(f".{kind}")
        try:
            {"md": export.save_markdown, "pdf": export.save_pdf, "docx": export.save_docx}[kind](call, target)
        except OSError as e:
            QMessageBox.warning(self, "Экспорт", f"Не удалось сохранить файл:\n{e}")
            return
        self.toast.show_message(f"Сохранено: {target.name}")

    # ================= запись =================

    def toggle_recording(self):
        if self.recorder:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self, auto_app: str | None = None):
        if self.recorder:
            return
        cfg = self.cfg
        now = date.today()
        call = storage.new_call("recording", title=f"Звонок в {auto_app} {now:%d.%m.%Y}" if auto_app else "")
        try:
            system = cfg["system_source"]
            if not system and cfg["record_system"]:
                # «по умолчанию» — значит туда, где сейчас звучит звонок (наушники, гарнитура…)
                system = callwatch.call_output_monitor(self._call_apps()) or ""
            self.recorder = audio.Recorder(
                call.path,
                mic=cfg["mic_source"] if cfg["record_mic"] else None,
                system=system if cfg["record_system"] else None,
            )
            self.recorder.start()
        except audio.AudioError as e:
            self.recorder = None
            shutil.rmtree(call.path, ignore_errors=True)  # там только что созданный meta.json
            QMessageBox.critical(self, "Запись", str(e))
            return
        call.save(status="recording", app=auto_app or "")
        self.recording_call = call
        self._auto_recording = bool(auto_app)
        self._set_offer(None)
        self.calls[call.id] = call
        for key, (ic, lb, meter) in self.meters.items():
            on = key in self.recorder.tracks
            for w in (ic, lb, meter):
                w.setVisible(on)
        self._set_record_button(True)
        self.tray_rec.setText("Остановить запись")
        self.tray_pause.setVisible(True)
        self.tray_mark.setVisible(True)
        self._update_marks_label()
        self._update_pause_ui()
        self.rec_timer.start()
        self._follow_misses = 0
        self.follow_timer.start()
        self.on_rec_tick()
        self.reload_list(select=call.id)
        self.update_tray()
        if auto_app:
            self.notify("Запись началась", f"Звонок в {auto_app}. Запись остановится сама, когда звонок закончится.")

    def toggle_pause(self):
        if not self.recorder:
            return
        if self.recorder.paused:
            self.recorder.resume()
        else:
            self.recorder.pause()
        self._update_pause_ui()
        self.on_rec_tick()
        self.update_tray()

    def _update_pause_ui(self):
        paused = bool(self.recorder and self.recorder.paused)
        self.rec_head.setText("Запись на паузе" if paused else "Идёт запись")
        self.btn_pause.setText("Продолжить" if paused else "Пауза")
        set_icon(self.btn_pause, "play" if paused else "pause")
        self.tray_pause.setText("Продолжить запись" if paused else "Пауза")
        if paused:
            self.pulse.stop()
            for _ic, _lb, meter in self.meters.values():
                meter.set_level(0)
        else:
            self.pulse.start()

    def _set_record_button(self, recording: bool):
        b = self.btn_record
        b.setProperty("recording", recording)
        b.style().unpolish(b)
        b.style().polish(b)
        if recording:
            set_icon(b, "stop", "red")
        else:
            b.setText("Начать запись")
            set_icon(b, "record", "on_accent")

    def on_rec_tick(self):
        if not self.recorder:
            return
        elapsed = fmt_duration(self.recorder.elapsed)
        if self.timer_label.text() != elapsed:
            self.update_tray()  # время в подсказке значка — раз в секунду
        self.timer_label.setText(elapsed)
        word = "На паузе" if self.recorder.paused else "Остановить"
        self.btn_record.setText(f"{word}  ·  {elapsed}")
        for key, track in self.recorder.tracks.items():
            self.meters[key][2].set_level(track.level)
        errors = self.recorder.errors()
        if errors:
            self.stop_recording(error="\n".join(errors))

    def stop_recording(self, error: str | None = None):
        if not self.recorder or not self.recording_call:
            return
        rec, call = self.recorder, self.recording_call
        self.follow_timer.stop()
        duration = rec.stop()
        self.rec_timer.stop()
        self.pulse.stop()
        self.recorder = None
        self.recording_call = None
        self._auto_recording = False
        self._set_record_button(False)
        self.tray_rec.setText("Начать запись")
        self.tray_pause.setVisible(False)
        self.tray_mark.setVisible(False)
        self.marks_label.setText("")

        silent = rec.silent_tracks()
        call.save(status="recorded", duration=round(duration, 1))
        if error:
            call.save(status="error", error=f"Запись прервалась: {error}")
            QMessageBox.warning(self, "Запись", f"Запись прервалась:\n{error}")
        elif silent:
            self.toast.show_message(f"Внимание: на дорожке «{', '.join(silent)}» почти не было звука.",
                                    8000, error=True)
        if not error and self.cfg["auto_process"] and call.tracks():
            self.pipeline.enqueue(call, self._with_summary(["transcribe", "speakers"]))
        self.reload_list(select=call.id)
        self.update_tray()

    def _call_apps(self) -> list[str]:
        return callwatch.parse_apps(self.cfg.get("call_apps") or callwatch.DEFAULT_APPS)

    def _follow_devices(self):
        """Во время записи следим, где звучит звонок и какой микрофон выбран в системе.

        Если устройства «по умолчанию», а звук ушёл в гарнитуру — переключаем запись туда.
        """
        rec = self.recorder
        if not rec or rec.paused:
            return
        try:
            if not self.cfg["system_source"] and rec.device("system"):
                want = callwatch.call_output_monitor(self._call_apps())
                if want:
                    self._follow_misses = 0
                else:
                    # звонок на секунду замолчал — не дёргаем запись, ждём несколько проверок
                    self._follow_misses += 1
                    want = audio.default_monitor() if self._follow_misses >= 3 else rec.device("system")
                if want != rec.device("system"):
                    rec.switch_device("system", want)
                    self.toast.show_message("Звук звонка переключился на другое устройство — записываю оттуда.")
            if not self.cfg["mic_source"] and rec.device("mic"):
                want = audio.default_mic()
                if want and want != rec.device("mic"):
                    rec.switch_device("mic", want)
                    self.toast.show_message("Системный микрофон сменился — записываю с нового.")
        except audio.AudioError:
            pass

    # ---------- автозапись звонков ----------

    def apply_silent_settings(self):
        mode = self.cfg.get("auto_record", "off")
        self.watcher.apps = self._call_apps()
        self.watcher.stop()
        if mode != "off":
            self.watcher.start()
        else:
            self._set_offer(None)
        try:
            config.set_autostart(bool(self.cfg.get("autostart")))
        except OSError:
            pass

    def _set_offer(self, app: str | None):
        self._offered_app = app
        self.tray_offer.setVisible(bool(app))
        if app:
            self.tray_offer.setText(f"Записать звонок ({app})")

    def on_call_started(self, app: str):
        if self.recorder:
            return
        if self.cfg.get("auto_record") == "auto":
            self.start_recording(auto_app=app)
        elif self.cfg.get("auto_record") == "ask":
            self._set_offer(app)
            # предложение показываем даже в режиме «без уведомлений», иначе оно бесполезно
            self._tray_click = ("offer", app)
            if self.isActiveWindow():
                self.toast.show_message(f"{app} использует микрофон. Записать звонок? — Ctrl+R", 8000)
            self._popup("offer", f"Начался звонок в {app}", "Записать созвон? Запись остановится сама, "
                        "когда звонок закончится.", [("default", "Записать"), ("record", "Записать"),
                                                     ("ignore", "Не записывать")], timeout_ms=30000, urgency=2)

    def on_call_ended(self, app: str):
        self._set_offer(None)
        if self.notifier:
            self.notifier.close("offer")
        if self.recorder and self._auto_recording:
            self.stop_recording()
            self.notify("Звонок закончился", "Запись остановлена — распознаю и делаю саммари.")

    def _accept_offer(self):
        if self._offered_app and not self.recorder:
            self.start_recording(auto_app=self._offered_app)

    def _popup(self, tag: str, title: str, text: str, actions=(), timeout_ms: int = 8000, urgency: int = 1,
               error: bool = False):
        """Системное уведомление с кнопками; если не получилось — обычное уведомление значка в трее."""
        if self.notifier and self.notifier.show(tag, title, text, list(actions), timeout_ms, urgency) is not None:
            return
        if self.tray.isVisible():
            kind, _, value = tag.partition(":")
            self._tray_click = (kind, value)
            icon = QSystemTrayIcon.MessageIcon.Warning if error else QSystemTrayIcon.MessageIcon.Information
            self.tray.showMessage(title, text, icon, timeout_ms)

    def on_notification_action(self, tag: str, key: str):
        if self.notifier and self.notifier.activation_token:
            # Wayland: окно выйдет на передний план только с токеном от сервера уведомлений
            os.environ["XDG_ACTIVATION_TOKEN"] = self.notifier.activation_token
        if tag == "offer":
            if key in ("default", "record"):
                self._accept_offer()
            elif key == "ignore":
                self._set_offer(None)
            return
        self.show_window()
        kind, _, value = tag.partition(":")
        if kind == "call" and value in self.calls:
            self._set_mode("calls")
            self.reload_list(select=value)
        elif kind == "digest":
            self.show_digest(date.fromisoformat(value) if value else None)
        elif kind == "update":
            self.open_update()

    def on_tray_message_clicked(self):
        kind, value = self._tray_click
        if kind == "offer" and self._offered_app:
            self._accept_offer()
            return
        self.show_window()
        if kind == "call" and value in self.calls:
            self._set_mode("calls")
            self.reload_list(select=value)
        elif kind == "digest":
            self.show_digest(date.fromisoformat(value) if value else None)
        elif kind == "update":
            self.open_update()

    # ================= итоги недели =================

    def check_digest(self):
        """По расписанию из настроек собираем итоги недели в фоне и сообщаем, когда готово."""
        if not llm.ready(self.cfg):
            return
        monday = digest.due(self.cfg, list(self.calls.values()))
        if monday is not None:
            self.page_digest.generate(monday, quiet=True)

    def on_digest_ready(self, monday):
        if self.view_mode == "digest":
            return
        self._popup(f"digest:{monday.isoformat()}", "Итоги недели готовы", digest.period(monday),
                    [("default", "Открыть"), ("open", "Открыть")])

    # ================= обработка =================

    def _with_summary(self, stages: list[str]) -> list[str]:
        return stages + (["summarize"] if llm.ready(self.cfg) else [])

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Открыть запись созвона", str(Path.home()), MEDIA_FILTER)
        if not path:
            return
        src = Path(path)
        call = storage.new_call("import", title=src.stem)
        self.calls[call.id] = call
        self.pipeline.enqueue(call, self._with_summary(["import", "transcribe", "speakers"]), src=src)
        self.reload_list(select=call.id)

    def run_cleanup(self):
        """Автоочистка: удаляем звук у старых созвонов, если это включено в настройках."""
        days = int(self.cfg.get("audio_retention_days") or 0)
        busy = {cid for cid in self.calls if self.pipeline.is_busy(cid)}
        if self.recording_call:
            busy.add(self.recording_call.id)
        cleaned = storage.cleanup_audio(list(self.calls.values()), days, busy)
        if cleaned:
            self.reload_list()
            if self.isVisible():
                n = len(cleaned)
                self.toast.show_message(f"Удалено аудио у {n} {plural(n, 'старого созвона', 'старых созвонов', 'старых созвонов')} "
                                        f"(старше {days} дн.). Транскрипты и саммари на месте.")

    def run_stages(self, stages: list[str]):
        call = self.current_call()
        if not call or self.pipeline.is_busy(call.id):
            return
        if "summarize" in stages and not llm.ready(self.cfg):
            if stages == ["summarize"]:
                self.toast.show_message(f"Сначала {llm.not_ready_text(self.cfg)}.")
                self.open_settings("Саммари")
                return
            stages = [s for s in stages if s != "summarize"]
        self.pipeline.enqueue(call, stages)
        self.refresh_item(call.id)
        self.show_current()

    def cancel_current(self):
        call = self.current_call()
        if call:
            self.pipeline.cancel(call.id)
            self.banner.title.setText("Отмена…")

    def on_status(self, call_id: str, text: str, frac: float):
        self.status_text[call_id] = (text, frac)
        self.refresh_item(call_id)
        self.update_tray()
        if call_id == self.current_id():
            if self.btn_primary.isVisible():
                self.show_current()
            else:
                self.update_banner()

    def on_summary_chunk(self, call_id: str, chunk: str):
        self.live_summary[call_id] = self.live_summary.get(call_id, "") + chunk
        if call_id == self.current_id() and not self.render_timer.isActive():
            self.render_timer.start()

    def render_live_summary(self):
        cid = self.current_id()
        if cid in self.live_summary:
            self._rendered.pop("summary", None)
            render.show_summary(self.summary_view, templates.hide_partial_marker(self.live_summary[cid]))
            sb = self.summary_view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def on_stage_done(self, call_id: str, stage: str):
        self.live_summary.pop(call_id, None)
        self.status_text.pop(call_id, None)
        call = self.calls.get(call_id)
        if stage == "summarize" and call:
            self.notify("Саммари готово", call.title, kind="summary", call_id=call_id)
        elif stage == "speakers" and call and not llm.ready(self.cfg):
            self.notify("Распознавание завершено", call.title, call_id=call_id)
        self.refresh_item(call_id)
        self.update_tray()
        if call_id == self.current_id():
            self.show_current()

    def on_failed(self, call_id: str, stage: str, message: str):
        self.live_summary.pop(call_id, None)
        self.status_text.pop(call_id, None)
        call = self.calls.get(call_id)
        if message != "Отменено." and call:
            self.notify("Ошибка обработки", f"{call.title}: {message}", kind="error", call_id=call_id)
        self.refresh_item(call_id)
        self.update_tray()
        if call_id == self.current_id():
            self.show_current()

    def notify(self, title: str, text: str, kind: str = "info", call_id: str = ""):
        """kind: info | summary | error. Всплывающие уведомления зависят от настройки «Уведомления»."""
        error = kind == "error"
        if self.isActiveWindow() or not self.tray.isVisible():
            self.toast.show_message(f"{title}: {text}", 5000, error=error)
            return
        mode = self.cfg.get("notifications", "all")
        if mode == "none" or (mode == "summary" and kind == "info"):
            return  # тихий режим: о состоянии говорит только значок в трее
        actions = [("default", "Открыть"), ("open", "Открыть")]
        self._popup(f"call:{call_id}" if call_id else "show:", title, text, actions,
                    urgency=2 if error else 1, error=error)

    # ================= настройки и выход =================

    def open_settings(self, page: str = ""):
        dlg = SettingsDialog(self.cfg, self, page)
        if dlg.exec():
            old_theme = self.cfg.get("theme", "system")
            old_days = self.cfg.get("audio_retention_days")
            self.cfg = dlg.result_config()
            config.save(self.cfg)
            if self.cfg.get("theme", "system") != old_theme:
                self.apply_theme()
            self.apply_silent_settings()
            if self.cfg.get("audio_retention_days") != old_days:
                self.run_cleanup()
            self.toast.show_message("Настройки сохранены.")
            self.show_current()
            if self.view_mode == "ask":
                self.page_ask.refresh()
            elif self.view_mode == "digest":
                self.page_digest.refresh()
            if dlg.update_release is not None:
                self.on_update_found(dlg.update_release)
                self.open_update(dlg.update_release)

    # ---------- обновления ----------

    def check_updates(self, force: bool = False):
        """Раз в сутки (или по кнопке) спрашиваем GitHub, нет ли новой версии."""
        from datetime import datetime

        if not force:
            if not self.cfg.get("update_check", True):
                return
            try:
                last = datetime.fromisoformat(self.cfg.get("update_last_check") or "")
                if (datetime.now() - last).total_seconds() < 24 * 3600:
                    return
            except ValueError:
                pass
        self.cfg["update_last_check"] = datetime.now().isoformat(timespec="seconds")
        config.save(self.cfg)
        self.update_checker.run(dict(self.cfg))

    def on_update_found(self, release):
        self.release = release
        skipped = self.cfg.get("update_skipped") == release.version
        self.btn_update.setText(f"⬆ Версия {release.version}")
        self.btn_update.setToolTip("Доступна новая версия — нажмите, чтобы посмотреть, что нового, и обновиться")
        self.btn_update.setVisible(not skipped)
        if not skipped and self._update_notified != release.version:
            self._update_notified = release.version
            self._popup("update:", f"Доступна версия {release.version}", "Нажмите, чтобы посмотреть, что нового, "
                        "и обновиться.", [("default", "Открыть"), ("open", "Обновить")])

    def open_update(self, release=None):
        from . import updates
        from .update_view import UpdateDialog

        release = release or self.release
        if release is None:
            return
        self.show_window()
        busy = ("Идёт запись — обновиться можно после неё." if self.recorder else
                "Идёт обработка записи — обновиться можно, когда она закончится." if self.pipeline.is_busy() else "")
        dlg = UpdateDialog(self, release, dict(self.cfg), busy)
        dlg.skipped.connect(self._skip_update)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.restart_command:
            updates.restart(dlg.restart_command)
        self.quit_app()  # установщик Windows запустит новую версию сам, в Linux — команда выше

    def _skip_update(self, version: str):
        self.cfg["update_skipped"] = version
        config.save(self.cfg)
        self.btn_update.hide()

    # ---------- две копии программы ----------

    def check_installs(self):
        """Старая копия из домашней папки перекрывает новую — предлагаем оставить одну."""
        from . import installs

        try:
            conflict = installs.find()
        except OSError:
            return
        if conflict is None or self.cfg.get("install_conflict_dismissed") == conflict.text:
            return
        self._install_conflict = conflict
        if self.isVisible():
            self._ask_install_conflict()

    def eventFilter(self, obj, event):
        # приветствие — не шире места справа от списка; Qt верно считает высоту текста
        # только у колонки фиксированной ширины, поэтому меняем саму ширину колонки
        if obj is self.page_welcome and event.type() == QEvent.Type.Resize:
            self.welcome_column.setFixedWidth(max(420, min(744, obj.width() - 48)))
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        if self._install_conflict is not None:
            QTimer.singleShot(300, self._ask_install_conflict)

    def _ask_install_conflict(self):
        from . import installs

        conflict, self._install_conflict = self._install_conflict, None
        if conflict is None:
            return
        if conflict.restart and (self.recorder or self.pipeline.is_busy()):
            self._install_conflict = conflict  # спросим после записи и обработки
            return
        box = QMessageBox(QMessageBox.Icon.Information, "Две копии программы", conflict.text, parent=self)
        box.setInformativeText("Записи, настройки и запомненные голоса общие — они останутся на месте.")
        go = box.addButton(conflict.action, QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Не сейчас", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not go:
            self.cfg["install_conflict_dismissed"] = conflict.text
            config.save(self.cfg)
            return
        try:
            installs.resolve(conflict)
        except OSError as e:
            QMessageBox.warning(self, "Две копии программы", f"Не получилось удалить старую копию:\n{e}")
            return
        if conflict.restart:
            self.quit_app()  # новая копия запустится сама через пару секунд
        else:
            self.toast.show_message("Старая копия удалена. Теперь из меню запускается эта версия.", 6000)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "welcome_column"):
            return
        if self.toast.isVisible():
            self.toast._reposition()
        # статистика в узком окне раскладывается по-другому
        if self.docs.currentIndex() == 2 and self.current_call() and self.stack.currentWidget() is self.page_call:
            self._render_stats(self.current_call())

    def quit_app(self):
        self._quitting = True
        self.close()
        self._quitting = False  # если выход отменили в диалоге

    def closeEvent(self, event):
        if not self._quitting and self.cfg.get("tray_on_close", True) and self.tray_available():
            # тихий режим: окно прячется, приложение продолжает работать в трее
            event.ignore()
            self.hide()
            if not self.cfg.get("tray_hint_shown"):
                self.cfg["tray_hint_shown"] = True
                config.save(self.cfg)
                self._popup("show:", config.APP_NAME, "Приложение продолжает работать в трее. "
                            "Выйти можно из меню значка.", [("default", "Открыть")], timeout_ms=6000)
            return
        if self.recorder:
            if QMessageBox.question(
                self, "Выход", "Идёт запись созвона. Остановить запись и выйти?"
            ) != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.stop_recording()
        if self.pipeline.is_busy():
            if QMessageBox.question(
                self, "Выход",
                "Идёт обработка записи. Если выйти, её можно будет запустить заново позже. Выйти?",
            ) != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.watcher.stop()
        if self.notifier:
            self.notifier.stop()
        if self.hotkey:
            self.hotkey.unregister()
        if self.bookmark_hotkey:
            self.bookmark_hotkey.unregister()
        self.tray.hide()
        event.accept()
        QApplication.quit()
