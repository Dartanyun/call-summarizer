"""Диалог «Кто это?» — переименование участника созвона."""

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from . import theme


class SpeakerDialog(QDialog):
    RESET = 2  # код возврата «вернуть стандартное имя»

    def __init__(self, parent, label: str, current: str, color: str, share: float,
                 quotes: list[str], suggestion: str = "", others: list[str] = ()):
        super().__init__(parent)
        self.setWindowTitle("Кто это?")
        self.setMinimumWidth(500)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 20, 22, 16)
        v.setSpacing(12)

        head = QHBoxLayout()
        dot = QLabel()
        dot.setPixmap(theme.pixmap("dot", color, 18))
        head.addWidget(dot)
        title = QLabel(html.escape(label))
        title.setObjectName("H2")
        head.addWidget(title)
        head.addStretch()
        share_l = QLabel(f"говорит {round(share * 100)}% времени")
        share_l.setProperty("muted", True)
        head.addWidget(share_l)
        v.addLayout(head)

        if quotes:
            card = QFrame()
            card.setObjectName("Card")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 10, 14, 10)
            cl.setSpacing(6)
            for q in quotes:
                ql = QLabel(f"«{q}»")
                ql.setWordWrap(True)
                ql.setProperty("muted", True)
                cl.addWidget(ql)
            v.addWidget(card)

        self.edit = QLineEdit(current)
        self.edit.setPlaceholderText("Имя, например «Ира» или «Денис Петрович»")
        self.edit.selectAll()
        v.addWidget(self.edit)

        if suggestion and suggestion != current:
            row = QHBoxLayout()
            hint = QLabel("Claude предполагает:")
            hint.setProperty("muted", True)
            row.addWidget(hint)
            sug = QPushButton(suggestion)
            sug.setObjectName("Chip")
            sug.setCursor(Qt.CursorShape.PointingHandCursor)
            sug.clicked.connect(lambda: self.edit.setText(suggestion))
            row.addWidget(sug)
            row.addStretch()
            v.addLayout(row)

        tip = "Если один человек разбит на двух спикеров — дайте им одинаковое имя, и они объединятся."
        if others:
            tip += " Уже есть: " + ", ".join(others) + "."
        tip_l = QLabel(tip)
        tip_l.setObjectName("Hint")
        tip_l.setWordWrap(True)
        v.addWidget(tip_l)

        buttons = QDialogButtonBox()
        save = buttons.addButton("Сохранить", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("Primary")
        save.setDefault(True)
        buttons.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        reset = buttons.addButton("Сбросить имя", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(lambda: self.done(self.RESET))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self.edit.setFocus()

    def name(self) -> str:
        return self.edit.text().strip()
