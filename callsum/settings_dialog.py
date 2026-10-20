"""Окно настроек: разделы слева, их содержимое справа."""

import sys
import threading

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from . import __version__, audio, callwatch, config, llm, templates, theme, voices
from .widgets import fix_row_heights


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


def _hint(text: str, links: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("Hint")
    if links:
        label.setOpenExternalLinks(True)
    return label


def _secret(value: str, placeholder: str) -> QLineEdit:
    edit = QLineEdit(value)
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    edit.setPlaceholderText(placeholder)
    return edit


class _Background(QObject):
    """Проверка подключения в фоне: окно настроек не замирает, пока ждём сеть."""
    done = Signal(object, str)

    def run(self, label: QLabel, work):
        label.setText("Проверяю…")

        def target():
            try:
                text = work()
            except Exception as e:  # noqa: BLE001 — показываем любую ошибку подключения
                text = f"Не получилось: {e}"
            self.done.emit(label, text)
        threading.Thread(target=target, daemon=True).start()


PAGES = [("settings", "Общие"), ("sparkles", "Саммари"), ("text", "Распознавание"), ("mic", "Запись"),
         ("clock", "Тихий режим"), ("calendar", "Интеграции"), ("folder", "Хранение")]


class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent=None, page: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(780)
        avail = QApplication.primaryScreen().availableGeometry()
        self.resize(860, min(720, int(avail.height() * 0.86)))
        self.cfg = dict(cfg)
        self._parent = parent
        self.bg = _Background(self)
        self.bg.done.connect(lambda label, text: label.setText(text))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 14)
        body = QHBoxLayout()
        body.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("SideNav")
        self.nav.setFixedWidth(200)
        self.stack = QStackedWidget()
        for icon_name, title in PAGES:
            item = QListWidgetItem(theme.icon(icon_name, theme.C["muted"]), title)
            self.nav.addItem(item)
        self.nav.setSpacing(1)
        self.nav.setIconSize(QSize(18, 18))
        fix_row_heights(self.nav, 36)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        side = QVBoxLayout()
        side.setContentsMargins(12, 14, 6, 0)
        side.addWidget(self.nav)
        body.addLayout(side)
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)

        for build in (self._page_general, self._page_summary, self._page_recognition, self._page_record,
                      self._page_silent, self._page_integrations, self._page_storage):
            self._add_page(build)
        names = [t for _i, t in PAGES]
        self.nav.setCurrentRow(names.index(page) if page in names else 0)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setText("Сохранить")
        save.setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(18, 0, 18, 0)
        from . import installs

        version = QLabel(f"Версия {__version__} · {installs.KIND_LABELS[installs.current_kind()]}")
        version.setObjectName("Faint")
        version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bottom.addWidget(version)
        bottom.addStretch()
        bottom.addWidget(buttons)
        outer.addLayout(bottom)
        self._on_backend()
        self._on_template()

    def _add_page(self, build):
        scroll = QScrollArea()
        scroll.setObjectName("Plain")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 8, 18, 8)
        layout.setSpacing(4)
        build(layout)
        layout.addStretch()
        scroll.setWidget(content)
        self.stack.addWidget(scroll)

    # ---------------- разделы ----------------

    def _page_general(self, layout: QVBoxLayout):
        cfg = self.cfg
        g = QGroupBox("Оформление")
        f = QFormLayout(g)
        self.theme = _combo(config.THEMES, cfg.get("theme", "system"))
        f.addRow("Тема:", self.theme)
        layout.addWidget(g)

        from . import installs
        from .update_view import Checker

        g = QGroupBox("Обновления")
        f = QFormLayout(g)
        f.addRow("Установлена:", QLabel(f"версия {__version__} · {installs.KIND_LABELS[installs.current_kind()]}"))
        self.update_check = QCheckBox("Проверять раз в день, нет ли новой версии")
        self.update_check.setChecked(cfg.get("update_check", True))
        f.addRow(self.update_check)
        self.github_token = _secret(cfg.get("github_token", ""), "не нужен, пока репозиторий открыт")
        f.addRow("Токен GitHub:", self.github_token)
        f.addRow(_hint("Новые версии берутся со страницы релизов на GitHub. Токен понадобится, только если "
                       'репозиторий закроют: <a href="https://github.com/settings/personal-access-tokens/new">токен '
                       "с ограниченным доступом</a> к репозиторию call-summarizer, право Contents — Read-only.",
                       links=True))
        row = QHBoxLayout()
        self.update_status = _hint("")
        row.addWidget(self.update_status, 1)
        self.btn_update_now = QPushButton("Обновить…")
        self.btn_update_now.setObjectName("Primary")
        self.btn_update_now.hide()
        self.btn_update_now.clicked.connect(self.accept)
        row.addWidget(self.btn_update_now, 0, Qt.AlignmentFlag.AlignTop)
        check = QPushButton("Проверить сейчас")
        check.clicked.connect(self._check_updates)
        row.addWidget(check, 0, Qt.AlignmentFlag.AlignTop)
        f.addRow(row)
        self.update_release = None
        self.checker = Checker(self)
        self.checker.found.connect(self._update_found)
        self.checker.none.connect(lambda: self.update_status.setText("Установлена последняя версия."))
        self.checker.failed.connect(self.update_status.setText)
        layout.addWidget(g)

    def _page_summary(self, layout: QVBoxLayout):
        cfg = self.cfg
        g = QGroupBox("Кто пишет саммари")
        f = QFormLayout(g)
        self.backend = _combo(llm.BACKENDS, llm.backend(cfg))
        self.backend.currentIndexChanged.connect(self._on_backend)
        f.addRow("Модель:", self.backend)
        f.addRow(_hint("Эта же модель отвечает на вопросы по созвонам и собирает итоги недели. "
                       "Звук не отправляется никуда — только текст транскрипта, и только если выбран Claude."))
        layout.addWidget(g)

        self.claude_box = g = QGroupBox("Claude API")
        f = QFormLayout(g)
        self.api_key = _secret(cfg["api_key"], "sk-ant-… (или переменная окружения ANTHROPIC_API_KEY)")
        f.addRow("API-ключ:", self.api_key)
        f.addRow("", _hint('Ключ можно получить на <a href="https://console.anthropic.com/settings/keys">'
                           "console.anthropic.com</a>.", links=True))
        self.claude_model = _combo(config.CLAUDE_MODELS, cfg["claude_model"])
        f.addRow("Модель Claude:", self.claude_model)
        self.effort = _combo(config.EFFORTS, cfg["effort"])
        f.addRow("Глубина анализа:", self.effort)
        layout.addWidget(g)

        self.ollama_box = g = QGroupBox("Локальная модель (Ollama)")
        f = QFormLayout(g)
        self.ollama_url = QLineEdit(cfg.get("ollama_url", ""))
        self.ollama_url.setPlaceholderText(llm.OLLAMA_URL)
        f.addRow("Адрес Ollama:", self.ollama_url)
        row = QHBoxLayout()
        self.ollama_model = QComboBox()
        self.ollama_model.setEditable(True)
        self.ollama_model.setMinimumWidth(260)
        if cfg.get("ollama_model"):
            self.ollama_model.addItem(cfg["ollama_model"])
        self.ollama_model.setCurrentText(cfg.get("ollama_model", ""))
        row.addWidget(self.ollama_model, 1)
        refresh = QPushButton("Обновить список")
        refresh.clicked.connect(self._refresh_ollama)
        row.addWidget(refresh)
        f.addRow("Модель:", row)
        self.ollama_status = _hint("")
        f.addRow("", self.ollama_status)
        f.addRow(_hint('Установите <a href="https://ollama.com/download">Ollama</a> и скачайте модель, например '
                       "<code>ollama pull qwen2.5:14b</code> (нужно ~16 ГБ памяти) или <code>qwen2.5:7b</code> для "
                       "слабых компьютеров. Текст созвонов не покидает компьютер.", links=True))
        layout.addWidget(g)

        g = QGroupBox("Шаблон саммари")
        v = QVBoxLayout(g)
        f = QFormLayout()
        self.template = _combo(templates.CHOICES, cfg.get("summary_template", templates.AUTO))
        self.template.currentIndexChanged.connect(self._on_template)
        f.addRow("По умолчанию:", self.template)
        v.addLayout(f)
        v.addWidget(_hint("«Определять автоматически» — модель сама поймёт, планёрка это, собеседование или звонок "
                          "с клиентом. У отдельного созвона шаблон меняется в меню «⋯» → «Сделать саммари заново»."))
        self.prompt_box = QWidget()
        pv = QVBoxLayout(self.prompt_box)
        pv.setContentsMargins(0, 6, 0, 0)
        pv.addWidget(_hint("Свой шаблон: что должно быть в саммари. Задачи, хронология и таймкоды добавятся сами."))
        self.prompt = QPlainTextEdit(cfg["summary_prompt"])
        self.prompt.setMinimumHeight(180)
        pv.addWidget(self.prompt)
        row = QHBoxLayout()
        row.addStretch()
        reset = QPushButton("Вернуть стандартный")
        reset.clicked.connect(lambda: self.prompt.setPlainText(config.DEFAULT_PROMPT))
        row.addWidget(reset)
        pv.addLayout(row)
        v.addWidget(self.prompt_box)
        layout.addWidget(g)

        g = QGroupBox("Итоги недели")
        f = QFormLayout(g)
        self.digest = _combo(config.DIGEST_SCHEDULES, cfg.get("digest_schedule", "fri17"))
        f.addRow("Собирать сами:", self.digest)
        f.addRow(_hint("Главное, решения, задачи по людям и что зависло — по саммари всех созвонов недели. "
                       "Собираются, пока программа запущена (можно в трее); вручную — кнопка «Итоги недели»."))
        layout.addWidget(g)

    def _page_recognition(self, layout: QVBoxLayout):
        cfg = self.cfg
        g = QGroupBox("Распознавание речи (локально, Whisper)")
        f = QFormLayout(g)
        self.whisper_model = _combo(config.WHISPER_MODELS, cfg["whisper_model"])
        f.addRow("Модель:", self.whisper_model)
        self.language = _combo(config.LANGUAGES, cfg["language"])
        f.addRow("Язык созвонов:", self.language)
        self.device = _combo(config.WHISPER_DEVICES, cfg.get("whisper_device", "auto"))
        f.addRow("Устройство:", self.device)
        from . import transcribe

        f.addRow("", _hint("Видеокарта NVIDIA найдена — распознавание будет в разы быстрее." if transcribe.cuda_available()
                           else "Видеокарта NVIDIA не найдена — распознавание идёт на процессоре."))
        self.denoise = QCheckBox("Шумоподавление перед распознаванием")
        self.denoise.setChecked(bool(cfg.get("denoise")))
        self.denoise.setToolTip("Убирает гул и постоянный шум (вентилятор, улица). Запись при этом не меняется.")
        f.addRow(self.denoise)
        self.diarize = QCheckBox("Определять говорящих по голосу")
        self.diarize.setChecked(cfg.get("diarize", True))
        self.diarize.setToolTip("Ваши реплики определяются по микрофону, собеседники различаются по голосу.\n"
                                "Число участников можно уточнить у конкретного созвона в меню «⋯».")
        f.addRow(self.diarize)
        self.auto = QCheckBox("Сразу распознавать и делать саммари после остановки записи")
        self.auto.setChecked(cfg["auto_process"])
        f.addRow(self.auto)
        layout.addWidget(g)

        g = QGroupBox("Словарь")
        v = QVBoxLayout(g)
        v.addWidget(_hint("Названия проектов и сервисов, термины, имена коллег — по одному на строку. "
                          "Whisper будет узнавать их точнее, а модель — исправлять похожие ошибки. "
                          "Слова сюда добавляются и из транскрипта: выделите ошибку → правая кнопка → «Исправить»."))
        self.vocabulary = QPlainTextEdit(cfg.get("vocabulary", ""))
        self.vocabulary.setPlaceholderText("API\nKubernetes\nPostgreSQL\nИра Смирнова\nпроект «Звезда»")
        self.vocabulary.setMinimumHeight(110)
        v.addWidget(self.vocabulary)
        layout.addWidget(g)

        g = QGroupBox("Запомненные голоса")
        v = QVBoxLayout(g)
        self.remember = QCheckBox("Запоминать голоса участников и узнавать их в следующих созвонах")
        self.remember.setChecked(cfg.get("remember_voices", True))
        v.addWidget(self.remember)
        v.addWidget(_hint("Голос запоминается, когда вы называете участника по имени. «Отпечатки» голосов "
                          "хранятся только на этом компьютере."))
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

    def _page_record(self, layout: QVBoxLayout):
        cfg = self.cfg
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
        f.addRow(_hint("«По умолчанию» — запись сама переключается туда, где сейчас звучит звонок "
                       "(например, на подключённую гарнитуру)."))
        layout.addWidget(g)

    def _page_silent(self, layout: QVBoxLayout):
        cfg, parent = self.cfg, self._parent
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
        self.call_apps.setCursorPosition(0)  # длинный список — показываем его начало, а не конец
        f.addRow("Приложения для звонков:", self.call_apps)
        self.notifications = _combo(config.NOTIFICATIONS, cfg.get("notifications", "all"))
        f.addRow("Уведомления:", self.notifications)
        hotkey = getattr(parent, "hotkey_label", None)
        mark = getattr(parent, "bookmark_hotkey_label", None)
        if sys.platform == "win32":
            err = getattr(getattr(parent, "hotkey", None), "error", 0)
            how = (f"Начать или остановить запись из любого окна — <b>{hotkey}</b>"
                   + (f", отметить важный момент — <b>{mark}</b>." if mark else ".") if hotkey
                   else "Общесистемная горячая клавиша недоступна: Win+Shift+Z и Ctrl+Alt+Z заняты другими программами."
                   if err in (0, 1409) else f"Общесистемную горячую клавишу не удалось включить (ошибка Windows {err}).")
        else:
            how = ("Из любого окна: начать или остановить запись — <b>Meta+Shift+Z</b>, отметить важный момент — "
                   "<b>Meta+Shift+B</b> (сменить: Параметры системы → Комбинации клавиш → «Саммари созвонов»).")
        f.addRow(_hint("Значок в трее всегда показывает состояние: ожидание, запись, пауза, расшифровка. " + how))
        layout.addWidget(g)

    def _page_integrations(self, layout: QVBoxLayout):
        cfg = self.cfg
        g = QGroupBox("Календари")
        v = QVBoxLayout(g)
        v.addWidget(_hint("Ссылки на календари в формате iCal (.ics), по одной на строку. По ним программа узнаёт "
                          "название встречи и участников. В Google Календаре: настройки календаря → «Интеграция "
                          "календаря» → «Секретный адрес в формате iCal». Календарь только читается."))
        self.calendars = QPlainTextEdit(cfg.get("calendar_urls", ""))
        self.calendars.setPlaceholderText("https://calendar.google.com/calendar/ical/…/basic.ics")
        self.calendars.setMaximumHeight(80)
        v.addWidget(self.calendars)
        row = QHBoxLayout()
        self.cal_status = _hint("")
        row.addWidget(self.cal_status, 1)
        b = QPushButton("Проверить")
        b.clicked.connect(self._check_calendar)
        row.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(row)
        layout.addWidget(g)

        g = QGroupBox("YouTrack")
        f = QFormLayout(g)
        self.yt_url = QLineEdit(cfg.get("youtrack_url", ""))
        self.yt_url.setPlaceholderText("https://company.youtrack.cloud")
        f.addRow("Адрес:", self.yt_url)
        self.yt_token = _secret(cfg.get("youtrack_token", ""), "perm:…")
        f.addRow("Постоянный токен:", self.yt_token)
        self.yt_project = QLineEdit(cfg.get("youtrack_project", ""))
        self.yt_project.setPlaceholderText("Короткое имя проекта, например APP")
        f.addRow("Проект:", self.yt_project)
        row = QHBoxLayout()
        self.yt_status = _hint("Токен: профиль → «Безопасность аккаунта» → «Новый токен».")
        row.addWidget(self.yt_status, 1)
        b = QPushButton("Проверить")
        b.clicked.connect(self._check_youtrack)
        row.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        f.addRow(row)
        layout.addWidget(g)

        g = QGroupBox("Todoist")
        f = QFormLayout(g)
        self.todoist = _secret(cfg.get("todoist_token", ""), "Токен API")
        f.addRow("Токен:", self.todoist)
        row = QHBoxLayout()
        self.td_status = _hint("Настройки Todoist → «Интеграции» → «Разработчик» → «Токен API».")
        row.addWidget(self.td_status, 1)
        b = QPushButton("Проверить")
        b.clicked.connect(self._check_todoist)
        row.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        f.addRow(row)
        layout.addWidget(g)

        g = QGroupBox("Obsidian")
        f = QFormLayout(g)
        row = QHBoxLayout()
        self.obsidian = QLineEdit(cfg.get("obsidian_dir", ""))
        self.obsidian.setPlaceholderText("Папка внутри хранилища, например …/Хранилище/Созвоны")
        row.addWidget(self.obsidian, 1)
        b = QPushButton("Выбрать…")
        b.clicked.connect(self._pick_obsidian)
        row.addWidget(b)
        f.addRow("Папка заметок:", row)
        self.obsidian_auto = QCheckBox("Сохранять туда каждое готовое саммари")
        self.obsidian_auto.setChecked(bool(cfg.get("obsidian_auto")))
        f.addRow(self.obsidian_auto)
        f.addRow(_hint("Заметка получает дату, участников ссылками [[Имя]], встречу из календаря и тег «созвон»; "
                       "транскрипт — в свёрнутом блоке. Вручную — кнопка экспорта над саммари."))
        layout.addWidget(g)
        layout.addWidget(_hint("Токены хранятся в файле настроек на этом компьютере. Задачи отправляются, "
                               "только когда вы нажимаете кнопку в окне «Задачи»."))

    def _page_storage(self, layout: QVBoxLayout):
        cfg = self.cfg
        g = QGroupBox("Хранение")
        f = QFormLayout(g)
        self.retention = _combo([(str(d), label) for d, label in config.RETENTION],
                                str(cfg.get("audio_retention_days", 0)))
        f.addRow("Удалять аудио старше:", self.retention)
        f.addRow(_hint("Удаляется только звук: транскрипты, саммари и запомненные голоса остаются. "
                       "Чтобы сохранить запись конкретного созвона, отметьте в его меню «⋯» "
                       "пункт «Не удалять аудио при автоочистке»."))
        f.addRow("Записи:", _hint(str(config.CALLS_DIR)))
        layout.addWidget(g)

    # ---------------- поведение ----------------

    def _on_backend(self):
        ollama = self.backend.currentData() == "ollama"
        self.claude_box.setVisible(not ollama)
        self.ollama_box.setVisible(ollama)
        if ollama and self.ollama_model.count() <= 1:
            self._refresh_ollama()

    def _on_template(self):
        self.prompt_box.setVisible(self.template.currentData() == templates.CUSTOM)

    def _refresh_ollama(self):
        cfg = {"ollama_url": self.ollama_url.text().strip()}
        current = self.ollama_model.currentText()
        models = llm.ollama_models(cfg)
        self.ollama_model.clear()
        self.ollama_model.addItems(models)
        if current:
            if current not in models:
                self.ollama_model.insertItem(0, current)
            self.ollama_model.setCurrentText(current)
        self.ollama_status.setText(f"Найдено моделей: {len(models)}." if models else
                                   f"Ollama не отвечает по адресу {llm.ollama_url(cfg)} или моделей пока нет.")

    def _check_updates(self):
        self.update_status.setText("Проверяю…")
        self.btn_update_now.hide()
        self.update_release = None
        self.checker.run({"github_token": self.github_token.text().strip()})

    def _update_found(self, release):
        self.update_status.setText(f"Доступна версия {release.version}.")
        self.btn_update_now.show()
        self.btn_update_now.clicked.disconnect()
        self.btn_update_now.clicked.connect(lambda: (setattr(self, "update_release", release), self.accept()))

    def _check_calendar(self):
        from . import calendar_ics

        cfg = {"calendar_urls": self.calendars.toPlainText()}
        self.bg.run(self.cal_status, lambda: calendar_ics.check(cfg))

    def _check_youtrack(self):
        from . import trackers

        cfg = {"youtrack_url": self.yt_url.text(), "youtrack_token": self.yt_token.text(),
               "youtrack_project": self.yt_project.text()}
        if not trackers.youtrack_ready(cfg):
            self.yt_status.setText("Заполните адрес, токен и проект.")
            return
        self.bg.run(self.yt_status, lambda: (trackers.youtrack_project_id(cfg), "Подключение работает, проект найден.")[1])

    def _check_todoist(self):
        from . import trackers

        token = self.todoist.text().strip()
        if not token:
            self.td_status.setText("Вставьте токен.")
            return
        self.bg.run(self.td_status, lambda: (trackers._request("GET", f"{trackers.TODOIST_API}/projects", token),
                                             "Токен подходит.")[1])

    def _pick_obsidian(self):
        folder = QFileDialog.getExistingDirectory(self, "Папка для заметок Obsidian", self.obsidian.text())
        if folder:
            self.obsidian.setText(folder)

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
            llm_backend=self.backend.currentData(),
            api_key=self.api_key.text().strip(),
            claude_model=self.claude_model.currentData(),
            effort=self.effort.currentData(),
            ollama_url=self.ollama_url.text().strip(),
            ollama_model=self.ollama_model.currentText().strip(),
            summary_template=self.template.currentData(),
            summary_prompt=self.prompt.toPlainText().strip() or config.DEFAULT_PROMPT,
            digest_schedule=self.digest.currentData(),
            whisper_model=self.whisper_model.currentData(),
            language=self.language.currentData(),
            whisper_device=self.device.currentData(),
            denoise=self.denoise.isChecked(),
            diarize=self.diarize.isChecked(),
            auto_process=self.auto.isChecked(),
            record_mic=self.record_mic.isChecked(),
            mic_source=self.mic.currentData(),
            record_system=self.record_system.isChecked(),
            system_source=self.system.currentData(),
            calendar_urls=self.calendars.toPlainText().strip(),
            youtrack_url=self.yt_url.text().strip(),
            youtrack_token=self.yt_token.text().strip(),
            youtrack_project=self.yt_project.text().strip(),
            todoist_token=self.todoist.text().strip(),
            obsidian_dir=self.obsidian.text().strip(),
            obsidian_auto=self.obsidian_auto.isChecked(),
            update_check=self.update_check.isChecked(),
            github_token=self.github_token.text().strip(),
        )
        return cfg
