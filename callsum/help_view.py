"""Справка внутри программы: темы из callsum/help/*.md, поиск по ним и самопроверка."""

import re
import tempfile
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QTextBrowser, QTextEdit,
    QVBoxLayout,
)

from . import osdeps, render, theme
from .widgets import set_icon

HELP_DIR = Path(__file__).parent / "help"


def topics() -> list[tuple[str, str, str]]:
    """[(ключ, заголовок, текст)] по порядку файлов."""
    out = []
    for f in sorted(HELP_DIR.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        m = re.match(r"\s*#\s+(.+)", text)
        out.append((f.stem, m.group(1).strip() if m else f.stem, text))
    return out


class HelpWindow(QDialog):
    def __init__(self, parent=None, open_settings=None):
        super().__init__(parent)
        self.setWindowTitle("Справка — Саммари созвонов")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1000, 720)
        self._open_settings = open_settings
        self.topics = topics()
        self.current = ""
        self.proc: QProcess | None = None

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 12)
        v.setSpacing(10)
        body = QHBoxLayout()
        body.setSpacing(0)
        side = QVBoxLayout()
        side.setContentsMargins(14, 14, 10, 0)
        side.setSpacing(8)
        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("Поиск по справке")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        side.addWidget(self.search)
        self.list = QListWidget()
        self.list.setObjectName("SideNav")
        self.list.setFixedWidth(250)
        self.list.currentItemChanged.connect(lambda item, _p: item and self.show_topic(item.data(Qt.ItemDataRole.UserRole)))
        side.addWidget(self.list, 1)
        body.addLayout(side)
        self.view = QTextBrowser()
        self.view.setObjectName("Doc")
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._on_link)
        body.addWidget(self.view, 1)
        v.addLayout(body, 1)

        row = QHBoxLayout()
        row.setContentsMargins(14, 0, 14, 0)
        self.btn_check = QPushButton("Проверить программу")
        set_icon(self.btn_check, "check")
        self.btn_check.setToolTip("Самопроверка: на месте ли ffmpeg, Qt, распознавание речи и говорящих")
        self.btn_check.clicked.connect(self.self_check)
        row.addWidget(self.btn_check)
        self.status = QLabel()
        self.status.setObjectName("Faint")
        row.addWidget(self.status, 1)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.close)
        row.addWidget(close)
        v.addLayout(row)
        self._filter("")

    # ---------- темы ----------

    def _filter(self, query: str):
        q = query.strip().lower()
        self.list.blockSignals(True)
        self.list.clear()
        for key, title, text in self.topics:
            if not q or q in text.lower():
                item = QListWidgetItem(title)
                item.setData(Qt.ItemDataRole.UserRole, key)
                self.list.addItem(item)
                if key == self.current:
                    self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        if self.list.count() and (self.list.currentItem() is None):
            self.list.setCurrentRow(0)
        elif self.current:
            self._highlight()
        if not self.list.count():
            render.show_placeholder(self.view, "Ничего не найдено", "Попробуйте другое слово.")

    def show_topic(self, key: str):
        text = next((t for k, _title, t in self.topics if k == key), None)
        if text is None:
            return
        self.current = key
        render.show_summary(self.view, text, keep_title=True)
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == key and self.list.currentRow() != i:
                self.list.blockSignals(True)
                self.list.setCurrentRow(i)
                self.list.blockSignals(False)
        self._highlight()

    def _highlight(self):
        q = self.search.text().strip()
        selections = []
        if q:
            color = QColor(theme.C["amber"])
            color.setAlphaF(0.35)
            cursor = self.view.document().find(q, 0)
            while not cursor.isNull() and len(selections) < 300:
                sel = QTextEdit.ExtraSelection()
                sel.cursor = cursor
                sel.format.setBackground(color)
                selections.append(sel)
                cursor = self.view.document().find(q, cursor)
        self.view.setExtraSelections(selections)
        if selections:
            c = QTextCursor(selections[0].cursor)
            c.clearSelection()
            self.view.setTextCursor(c)
            self.view.ensureCursorVisible()

    def _on_link(self, url: QUrl):
        if url.scheme() == "help":
            self.search.clear()
            self.show_topic(url.path())
        elif url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)

    # ---------- самопроверка ----------

    def self_check(self):
        """Запускаем программу заново с --self-check: проверяется ровно то, что запустится у пользователя."""
        if self.proc:
            return
        report = Path(tempfile.gettempdir()) / f"call-summarizer-check-{osdeps.user_id()}.txt"
        report.unlink(missing_ok=True)
        cmd = osdeps.app_command()
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.finished.connect(lambda code, _s: self._check_done(code, report))
        self.proc.start(cmd[0], [*cmd[1:], "--self-check", "--report", str(report)])
        self.btn_check.setEnabled(False)
        self.status.setText("Проверяю… это займёт несколько секунд.")

    def _check_done(self, code: int, report: Path):
        self.proc = None
        self.btn_check.setEnabled(True)
        try:
            text = report.read_text(encoding="utf-8")
        except OSError:
            text = "Отчёт не получен: программа не запустилась для проверки."
        self.status.setText("Всё в порядке." if code == 0 else "Есть проблемы — подробности в отчёте.")
        self.current = ""
        self.list.clearSelection()
        lines = "\n".join(f"    {line}" for line in text.splitlines())
        render.show_summary(self.view, f"# Самопроверка\n\n{lines}\n\nСтроки **FAIL** — то, что мешает работе, "
                                       "**WARN** — необязательное. Решения частых проблем — в разделе "
                                       "[Решение проблем](help:14-troubleshooting).", keep_title=True)
