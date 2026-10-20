"""Окно «Задачи»: задачи из саммари — в YouTrack, Todoist, напоминания в календарь или списком в буфер."""

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout,
)

from . import export, trackers
from .storage import Call
from .widgets import fix_row_heights, set_icon


class _Sender(QObject):
    done = Signal(str)
    failed = Signal(str)

    def run(self, work):
        def target():
            try:
                self.done.emit(work())
            except trackers.TrackerError as e:
                self.failed.emit(str(e))
            except Exception as e:  # noqa: BLE001
                self.failed.emit(str(e) or e.__class__.__name__)
        threading.Thread(target=target, daemon=True).start()


class TasksDialog(QDialog):
    def __init__(self, parent, call: Call, cfg: dict, open_settings):
        super().__init__(parent)
        self.setWindowTitle("Задачи из саммари")
        self.setMinimumWidth(780)
        self.call, self.cfg, self._open_settings = call, cfg, open_settings
        self.tasks = call.tasks()
        self.sender_ = _Sender(self)
        self.sender_.done.connect(self._on_done)
        self.sender_.failed.connect(self._on_failed)

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(10)
        head = QLabel(f"«{call.title}»")
        head.setObjectName("H2")
        head.setWordWrap(True)
        v.addWidget(head)
        hint = QLabel("Отметьте задачи, которые нужно отправить. Исполнитель, срок и момент записи попадут в описание.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.list = QListWidget()
        self.list.setObjectName("Tasks")
        for t in self.tasks:
            due = f"  ·  срок: {t.deadline}" if t.deadline else ""
            who = f"{t.assignee} — " if t.assignee else ""
            item = QListWidgetItem(f"{who}{t.what or t.text}{due}" + ("  ✓ выполнено" if t.done else ""))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if t.done else Qt.CheckState.Checked)
            item.setToolTip(t.text)
            self.list.addItem(item)
        fix_row_heights(self.list, 34)
        self.list.setMinimumHeight(min(320, 16 + 35 * len(self.tasks)))
        v.addWidget(self.list, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.buttons = []
        for text, icon, slot in (("В YouTrack", "send", self.to_youtrack), ("В Todoist", "send", self.to_todoist),
                                 ("Напоминания…", "calendar", self.to_ics), ("Скопировать", "copy", self.copy)):
            b = QPushButton(text)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            set_icon(b, icon)
            b.clicked.connect(slot)
            row.addWidget(b)
            self.buttons.append(b)
        self.buttons[0].setToolTip("Создать задачи в проекте YouTrack (адрес, токен и проект — в «Настройках» → «Интеграции»)")
        self.buttons[1].setToolTip("Добавить во «Входящие» Todoist (токен — в «Настройках» → «Интеграции»)")
        self.buttons[2].setToolTip("Файл .ics: событие в день срока с напоминанием — откройте его в календаре")
        row.addStretch()
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        v.addLayout(row)

    def selected(self):
        return [t for i, t in enumerate(self.tasks) if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def _need(self, ready: bool, what: str) -> bool:
        if ready:
            return True
        box = QMessageBox(QMessageBox.Icon.Information, "Задачи",
                          f"Чтобы отправлять задачи в {what}, укажите данные для входа в «Настройках» → «Интеграции».",
                          parent=self)
        settings = box.addButton("Открыть настройки", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is settings:
            self.accept()
            self._open_settings()
        return False

    def _start(self, text: str, work):
        items = self.selected()
        if not items:
            self.status.setText("Отметьте хотя бы одну задачу.")
            return
        for b in self.buttons:
            b.setEnabled(False)
        self.status.setText(text)
        self.sender_.run(lambda: work(items))

    def to_youtrack(self):
        if self._need(trackers.youtrack_ready(self.cfg), "YouTrack"):
            self._start("Отправляю в YouTrack…", lambda items: "Создано в YouTrack: " + ", ".join(
                trackers.send_youtrack(self.cfg, self.call, items)))

    def to_todoist(self):
        if self._need(trackers.todoist_ready(self.cfg), "Todoist"):
            self._start("Отправляю в Todoist…", lambda items: f"Добавлено в Todoist: {trackers.send_todoist(self.cfg, self.call, items)}")

    def to_ics(self):
        items = self.selected()
        if not items:
            self.status.setText("Отметьте хотя бы одну задачу.")
            return
        name = f"Задачи — {export.safe_name(self.call.title)}.ics"
        path, _ = QFileDialog.getSaveFileName(self, "Напоминания", str(Path.home() / name), "Календарь (*.ics)")
        if not path:
            return
        Path(path).write_text(trackers.reminders_ics(self.call, items), encoding="utf-8")
        undated = sum(1 for t in items if not t.due_date)
        self.status.setText(f"Сохранено: {Path(path).name}. Откройте файл — календарь предложит добавить события."
                            + (f" Задачи без срока ({undated}) — на следующий рабочий день." if undated else ""))
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def copy(self):
        items = self.selected() or self.tasks
        QApplication.clipboard().setText("\n".join(f"{'☑' if t.done else '☐'} {t.text}" for t in items))
        self.status.setText("Список задач скопирован.")

    def _on_done(self, text: str):
        self.status.setText(text)
        for b in self.buttons:
            b.setEnabled(True)

    def _on_failed(self, text: str):
        self.status.setText(f"Не получилось: {text}")
        for b in self.buttons:
            b.setEnabled(True)
