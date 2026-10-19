"""Окно настроек."""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from . import __version__, audio, callwatch, config, voices


def _combo(items: list[tuple[str, str]], current: str) -> QComboBox:
    box = QComboBox()
    for value, label in items:
        box.addItem(label, value)
    idx = box.findData(current)
    if idx < 0 and current:
        box.addItem(current, current)  # устройство сейчас не подключено — не теряем выбор
        idx = box.count() - 1
    box.setCurrentIndex(max(0, idx))
    return box


class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(640)
        avail = QApplication.primaryScreen().availableGeometry()
        self.resize(680, min(860, int(avail.height() * 0.86)))
        self.cfg = dict(cfg)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 14)
        # содержимое прокручивается, чтобы окно помещалось и на небольших экранах
        scroll = QScrollArea()
        scroll.setObjectName("Plain")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 8, 18, 8)
        layout.setSpacing(4)

        # Оформление
        g = QGroupBox("Оформление")
        f = QFormLayout(g)
        self.theme = _combo(config.THEMES, cfg.get("theme", "system"))
        f.addRow("Тема:", self.theme)
        layout.addWidget(g)

        # Claude
        g = QGroupBox("Саммари (Claude API)")
        f = QFormLayout(g)
        self.api_key = QLineEdit(cfg["api_key"])
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("sk-ant-… (или переменная окружения ANTHROPIC_API_KEY)")
        f.addRow("API-ключ:", self.api_key)
        hint = QLabel('Ключ можно получить на <a href="https://console.anthropic.com/settings/keys">console.anthropic.com</a>.')
        hint.setOpenExternalLinks(True)
        f.addRow("", hint)
        self.claude_model = _combo(config.CLAUDE_MODELS, cfg["claude_model"])
        f.addRow("Модель:", self.claude_model)
        self.effort = _combo(config.EFFORTS, cfg["effort"])
        f.addRow("Глубина анализа:", self.effort)
        layout.addWidget(g)

        # Whisper
        g = QGroupBox("Распознавание речи (локально, Whisper)")
        f = QFormLayout(g)
        self.whisper_model = _combo(config.WHISPER_MODELS, cfg["whisper_model"])
        f.addRow("Модель:", self.whisper_model)
        self.language = _combo(config.LANGUAGES, cfg["language"])
        f.addRow("Язык созвонов:", self.language)
        self.diarize = QCheckBox("Определять говорящих по голосу")
        self.diarize.setChecked(cfg.get("diarize", True))
        self.diarize.setToolTip(
            "Ваши реплики определяются по микрофону, собеседники различаются по голосу.\n"
            "Число участников можно уточнить у конкретного созвона в меню «⋯»."
        )
        f.addRow(self.diarize)
        self.auto = QCheckBox("Сразу распознавать и делать саммари после остановки записи")
        self.auto.setChecked(cfg["auto_process"])
        f.addRow(self.auto)
        layout.addWidget(g)

        # Словарь
        g = QGroupBox("Словарь")
        v = QVBoxLayout(g)
        hint = QLabel("Названия проектов и сервисов, термины, имена коллег — по одному на строку. "
                      "Whisper будет узнавать их точнее, а Claude — исправлять похожие ошибки. "
                      "Слова сюда добавляются и из транскрипта: выделите ошибку → правая кнопка → «Исправить».")
        hint.setWordWrap(True)
        hint.setObjectName("Hint")
        v.addWidget(hint)
        self.vocabulary = QPlainTextEdit(cfg.get("vocabulary", ""))
        self.vocabulary.setPlaceholderText("API\nKubernetes\nPostgreSQL\nИра Смирнова\nпроект «Звезда»")
        self.vocabulary.setMinimumHeight(110)
        v.addWidget(self.vocabulary)
        layout.addWidget(g)

        # Голоса
        g = QGroupBox("Запомненные голоса")
        v = QVBoxLayout(g)
        self.remember = QCheckBox("Запоминать голоса участников и узнавать их в следующих созвонах")
        self.remember.setChecked(cfg.get("remember_voices", True))
        v.addWidget(self.remember)
        hint = QLabel("Голос запоминается, когда вы называете участника по имени. «Отпечатки» голосов "
                      "хранятся только на этом компьютере.")
        hint.setWordWrap(True)
        hint.setObjectName("Hint")
        v.addWidget(hint)
        self.voices_list = QListWidget()
        self.voices_list.setMaximumHeight(130)
        v.addWidget(self.voices_list)
        row = QHBoxLayout()
        row.addStretch()
        self.btn_forget = QPushButton("Забыть выбранный")
        self.btn_forget.clicked.connect(self._forget_selected)
        self.btn_forget_all = QPushButton("Забыть все")
        self.btn_forget_all.clicked.connect(self._forget_all)
        row.addWidget(self.btn_forget)
        row.addWidget(self.btn_forget_all)
        v.addLayout(row)
        self._fill_voices()
        layout.addWidget(g)

        # Тихий режим
        g = QGroupBox("Тихий режим")
        f = QFormLayout(g)
        self.tray_on_close = QCheckBox("Сворачивать в трей при закрытии окна")
        self.tray_on_close.setChecked(cfg.get("tray_on_close", True))
        f.addRow(self.tray_on_close)
        self.autostart = QCheckBox("Запускать вместе с системой (сразу в трей)")
        self.autostart.setChecked(cfg.get("autostart", False))
        f.addRow(self.autostart)
        self.auto_record = _combo(config.AUTO_RECORD, cfg.get("auto_record", "off"))
        self.auto_record.setToolTip("Звонок замечается, когда одно из приложений ниже начинает брать звук с микрофона.\n"
                                    "Запись, начатая автоматически, сама остановится, когда звонок закончится.")
        f.addRow("Автозапись звонков:", self.auto_record)
        self.call_apps = QLineEdit(cfg.get("call_apps") or callwatch.DEFAULT_APPS)
        self.call_apps.setToolTip("Части названий приложений через запятую.")
        f.addRow("Приложения для звонков:", self.call_apps)
        self.notifications = _combo(config.NOTIFICATIONS, cfg.get("notifications", "all"))
        f.addRow("Уведомления:", self.notifications)
        hotkey = getattr(parent, "hotkey_label", None)
        if sys.platform == "win32":
            err = getattr(getattr(parent, "hotkey", None), "error", 0)
            how = (f"Начать или остановить запись из любого окна — <b>{hotkey}</b>." if hotkey
                   else "Общесистемная горячая клавиша недоступна: Win+Shift+Z и Ctrl+Alt+Z заняты другими программами."
                   if err in (0, 1409) else f"Общесистемную горячую клавишу не удалось включить (ошибка Windows {err}).")
        else:
            how = ("Начать или остановить запись из любого окна — <b>Meta+Shift+Z</b> "
                   "(сменить: Параметры системы → Комбинации клавиш → «Саммари созвонов»).")
        hint = QLabel("Значок в трее всегда показывает состояние: ожидание, запись, пауза, расшифровка. " + how)
        hint.setWordWrap(True)
        hint.setObjectName("Hint")
        f.addRow(hint)
        layout.addWidget(g)

        # Хранение
        g = QGroupBox("Хранение")
        f = QFormLayout(g)
        self.retention = _combo([(str(d), label) for d, label in config.RETENTION],
                                str(cfg.get("audio_retention_days", 0)))
        f.addRow("Удалять аудио старше:", self.retention)
        hint = QLabel("Удаляется только звук: транскрипты, саммари и запомненные голоса остаются. "
                      "Чтобы сохранить запись конкретного созвона, отметьте в его меню «⋯» "
                      "пункт «Не удалять аудио при автоочистке».")
        hint.setWordWrap(True)
        hint.setObjectName("Hint")
        f.addRow(hint)
        layout.addWidget(g)

        # Звук
        g = QGroupBox("Запись звука")
        f = QFormLayout(g)
        try:
            mics, monitors = audio.list_sources()
        except (audio.AudioError, ValueError):
            mics, monitors = [], []
        self.record_mic = QCheckBox("Записывать микрофон")
        self.record_mic.setChecked(cfg["record_mic"])
        f.addRow(self.record_mic)
        self.mic = _combo([("", "По умолчанию в системе")] + mics, cfg["mic_source"])
        f.addRow("Микрофон:", self.mic)
        self.record_system = QCheckBox("Записывать звук собеседников (то, что слышно в колонках/наушниках)")
        self.record_system.setChecked(cfg["record_system"])
        f.addRow(self.record_system)
        self.system = _combo([("", "Устройство вывода по умолчанию")] + monitors, cfg["system_source"])
        f.addRow("Откуда:", self.system)
        layout.addWidget(g)

        # Промпт
        g = QGroupBox("Что должно быть в саммари")
        v = QVBoxLayout(g)
        self.prompt = QPlainTextEdit(cfg["summary_prompt"])
        self.prompt.setMinimumHeight(160)
        v.addWidget(self.prompt)
        row = QHBoxLayout()
        row.addStretch()
        reset = QPushButton("Вернуть стандартный")
        reset.clicked.connect(lambda: self.prompt.setPlainText(config.DEFAULT_PROMPT))
        row.addWidget(reset)
        v.addLayout(row)
        layout.addWidget(g)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setText("Сохранить")
        save.setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(18, 0, 18, 0)
        version = QLabel(f"Версия {__version__}")
        version.setObjectName("Faint")
        version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bottom.addWidget(version)
        bottom.addStretch()
        bottom.addWidget(buttons)
        outer.addLayout(bottom)

    def _fill_voices(self):
        self.voices_list.clear()
        profiles = voices.load()
        for name in sorted(profiles, key=lambda n: (n != voices.OWN_VOICE, n.lower())):
            calls = int(profiles[name].get("calls", 1))
            label = "Я (ваш голос)" if name == voices.OWN_VOICE else name
            item = QListWidgetItem(f"{label}  ·  {calls} созв.")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.voices_list.addItem(item)
        if not profiles:
            item = QListWidgetItem("Пока никого — назовите участника в любом созвоне")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.voices_list.addItem(item)
        self.btn_forget.setEnabled(bool(profiles))
        self.btn_forget_all.setEnabled(bool(profiles))

    def _forget_selected(self):
        item = self.voices_list.currentItem()
        name = item.data(Qt.ItemDataRole.UserRole) if item else None
        if name:
            voices.forget(name)
            self._fill_voices()

    def _forget_all(self):
        if QMessageBox.question(self, "Голоса", "Забыть все запомненные голоса?") == QMessageBox.StandardButton.Yes:
            voices.forget_all()
            self._fill_voices()

    def result_config(self) -> dict:
        cfg = dict(self.cfg)
        cfg.update(
            theme=self.theme.currentData(),
            vocabulary=self.vocabulary.toPlainText().strip(),
            tray_on_close=self.tray_on_close.isChecked(),
            autostart=self.autostart.isChecked(),
            auto_record=self.auto_record.currentData(),
            call_apps="" if self.call_apps.text().strip() == callwatch.DEFAULT_APPS else self.call_apps.text().strip(),
            notifications=self.notifications.currentData(),
            audio_retention_days=int(self.retention.currentData()),
            remember_voices=self.remember.isChecked(),
            api_key=self.api_key.text().strip(),
            claude_model=self.claude_model.currentData(),
            effort=self.effort.currentData(),
            whisper_model=self.whisper_model.currentData(),
            language=self.language.currentData(),
            diarize=self.diarize.isChecked(),
            auto_process=self.auto.isChecked(),
            record_mic=self.record_mic.isChecked(),
            mic_source=self.mic.currentData(),
            record_system=self.record_system.isChecked(),
            system_source=self.system.currentData(),
            summary_prompt=self.prompt.toPlainText().strip() or config.DEFAULT_PROMPT,
        )
        return cfg
