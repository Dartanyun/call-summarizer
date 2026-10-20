"""Окно «Доступна новая версия»: что нового, скачивание и установка."""

import threading
import traceback

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QTextBrowser, QVBoxLayout,
)

from . import __version__, installs, render, updates
from .widgets import set_icon


class Checker(QObject):
    """Проверка новой версии в фоне."""
    found = Signal(object)   # updates.Release (новее текущей)
    none = Signal()          # установлена последняя версия
    failed = Signal(str)

    def run(self, cfg: dict):
        def work():
            try:
                rel = updates.latest(cfg)
            except updates.UpdateError as e:
                self.failed.emit(str(e))
                return
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.failed.emit(str(e) or e.__class__.__name__)
                return
            (self.found.emit(rel) if updates.is_newer(rel.version) else self.none.emit())
        threading.Thread(target=work, daemon=True).start()


class _Installer(QObject):
    progress = Signal(float)
    status = Signal(str)
    done = Signal(object)   # команда запуска новой версии или None
    failed = Signal(str)


class UpdateDialog(QDialog):
    skipped = Signal(str)  # версия, которую попросили пропустить

    def __init__(self, parent, release: updates.Release, cfg: dict, busy: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Обновление")
        self.setMinimumSize(640, 480)
        self.release, self.cfg = release, cfg
        self.kind = installs.current_kind()
        self.asset = None
        name = updates.asset_name(self.kind, release.version)
        if name:
            self.asset = release.assets.get(name)
        self.restart_command = None
        self.job = _Installer(self)
        self.job.progress.connect(lambda f: self.bar.setValue(int(f * 1000)))
        self.job.status.connect(self._status)
        self.job.done.connect(self._installed)
        self.job.failed.connect(self._failed)

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(10)
        head = QLabel(f"Доступна версия {release.version}")
        head.setObjectName("H2")
        v.addWidget(head)
        sub = QLabel(f"Сейчас установлена {__version__} ({installs.KIND_LABELS[self.kind]}). "
                     "Записи, настройки и голоса при обновлении сохранятся.")
        sub.setObjectName("Hint")
        sub.setWordWrap(True)
        v.addWidget(sub)
        notes = QTextBrowser()
        notes.setObjectName("Doc")
        notes.setOpenExternalLinks(True)
        render.show_summary(notes, release.notes or "*Описание изменений не указано.*")
        v.addWidget(notes, 1)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.hide()
        v.addWidget(self.bar)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.info)

        row = QHBoxLayout()
        self.btn_skip = QPushButton("Пропустить эту версию")
        self.btn_skip.setObjectName("Ghost")
        self.btn_skip.clicked.connect(self._skip)
        row.addWidget(self.btn_skip)
        row.addStretch()
        self.btn_page = QPushButton("Открыть на GitHub")
        self.btn_page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(release.page)))
        row.addWidget(self.btn_page)
        self.btn_later = QPushButton("Позже")
        self.btn_later.clicked.connect(self.reject)
        row.addWidget(self.btn_later)
        self.btn_go = QPushButton("Обновить")
        self.btn_go.setObjectName("Primary")
        set_icon(self.btn_go, "download", "on_accent")
        self.btn_go.clicked.connect(self._start)
        row.addWidget(self.btn_go)
        v.addLayout(row)

        if not self.asset:
            self.btn_go.hide()
            self.info.setText(updates.why_manual(self.kind) or "Для этой установки нет готового файла — "
                              "скачайте новую версию со страницы релиза.")
        elif busy:
            self.btn_go.setEnabled(False)
            self.info.setText(busy)
        else:
            mb = self.asset.size / 2**20
            how = {"deb": "Пакет поставится через системный запрос пароля.",
                   "setup": "Установщик поставит новую версию без прав администратора, программа перезапустится.",
                   "user": "Программа обновится в домашней папке и перезапустится."}.get(self.kind, "")
            self.info.setText(f"Скачается {mb:.0f} МБ. {how}")

    def _skip(self):
        self.skipped.emit(self.release.version)
        self.reject()

    def _status(self, text: str):
        self.info.setText(text)

    def _start(self):
        for b in (self.btn_go, self.btn_skip, self.btn_later, self.btn_page):
            b.setEnabled(False)
        self.bar.show()
        self.info.setText("Скачиваю…")
        cfg, asset, kind, job = dict(self.cfg), self.asset, self.kind, self.job

        def work():
            try:
                path = updates.download(cfg, asset, job.progress.emit)
                job.status.emit("Устанавливаю…")
                job.done.emit(updates.install(kind, path, job.status.emit))
            except updates.UpdateError as e:
                job.failed.emit(str(e))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                job.failed.emit(str(e) or e.__class__.__name__)
        threading.Thread(target=work, daemon=True).start()

    def _installed(self, command):
        self.restart_command = command
        self.info.setText("Готово — перезапускаю программу…")
        self.accept()

    def _failed(self, text: str):
        self.bar.hide()
        self.info.setText(f"Не получилось обновить: {text}")
        for b in (self.btn_go, self.btn_skip, self.btn_later, self.btn_page):
            b.setEnabled(True)
        self.btn_go.setText("Попробовать ещё раз")
