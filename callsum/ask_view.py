"""Страницы «Спросить у созвонов» и «Итоги недели»."""

import re
import threading
import traceback
from collections.abc import Callable
from datetime import date, timedelta

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from . import ask, digest, llm, render, sections
from .storage import Call
from .widgets import Banner, FlowLayout, Segmented, icon_button, set_icon


class Job(QObject):
    """Запрос к модели в фоновом потоке: окно не замирает, текст появляется по мере написания."""
    chunk = Signal(str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, work: Callable[[Callable[[str], None], Callable[[], bool]], str]):
        super().__init__()
        self._work = work
        self.stopped = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self.done.emit(self._work(self.chunk.emit, lambda: self.stopped))
        except llm.LLMError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 — любую ошибку показываем человеку, а не роняем окно
            traceback.print_exc()
            self.failed.emit(str(e) or e.__class__.__name__)


def link_citations(md: str, calls: dict[str, Call]) -> str:
    """[[ID@00:12:34]] → ссылка «Название · 12:34», по которой открывается созвон в этом месте."""
    def repl(m: re.Match) -> str:
        call = calls.get(m.group(1))
        if call is None:
            return ""
        name = call.title if len(call.title) <= 32 else call.title[:30].rstrip() + "…"
        if m.group(2):
            secs = sections.seconds(m.group(2))
            label = f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}" if secs >= 3600 else f"{secs // 60}:{secs % 60:02d}"
            return f"[{name} · {label}](call:{call.id}@{secs})"
        return f"[{name}](call:{call.id})"
    return re.sub(r"[ \t]{2,}", " ", ask.CITATION.sub(repl, md))


def _page_header(title: str, sub: str) -> tuple[QVBoxLayout, QLabel]:
    col = QVBoxLayout()
    col.setSpacing(6)
    h = QLabel(title)
    h.setObjectName("H1")
    col.addWidget(h)
    s = QLabel(sub)
    s.setProperty("muted", True)
    s.setWordWrap(True)
    col.addWidget(s)
    return col, s


def _doc_view(on_link) -> QTextBrowser:
    view = QTextBrowser()
    view.setObjectName("Doc")
    view.setOpenLinks(False)
    view.anchorClicked.connect(on_link)
    view.viewport().setAutoFillBackground(False)
    return view


class AskPage(QWidget):
    SUGGESTIONS = ["Что мы решили за последнюю неделю?", "Какие задачи на мне?",
                   "Какие вопросы остались открытыми?", "Кто что обещал сделать?"]

    def __init__(self, get_calls: Callable[[], list[Call]], get_config: Callable[[], dict],
                 open_call: Callable[[str, float | None], None], open_settings: Callable[[], None]):
        super().__init__()
        self._get_calls, self._get_config, self._open_call = get_calls, get_config, open_call
        self.history: list[tuple[str, str]] = []
        self._pending = ""
        self._live = ""
        self.job: Job | None = None
        self.scope_call: Call | None = None

        v = QVBoxLayout(self)
        v.setContentsMargins(34, 26, 34, 24)
        v.setSpacing(14)
        head, _ = _page_header("Спросить у созвонов", "Вопрос по записям — что решили, кто что обещал, когда "
                                                      "обсуждали. Ответ со ссылками: по клику откроется созвон "
                                                      "в нужном месте.")
        top = QHBoxLayout()
        top.addLayout(head, 1)
        self.btn_new = QPushButton("Новый разговор")
        self.btn_new.setObjectName("Ghost")
        set_icon(self.btn_new, "refresh", "muted")
        self.btn_new.clicked.connect(self.reset)
        top.addWidget(self.btn_new, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(top)

        self.banner = Banner()
        self.banner.hide()
        self._open_settings = open_settings
        v.addWidget(self.banner)

        row = QHBoxLayout()
        self.scope = Segmented(["Все созвоны", "Выбранный созвон"], lambda _i: self._update_scope_hint())
        row.addWidget(self.scope)
        self.scope_hint = QLabel()
        self.scope_hint.setObjectName("Faint")
        row.addWidget(self.scope_hint, 1)
        v.addLayout(row)

        self.suggest_box = QWidget()
        self.suggest = FlowLayout(self.suggest_box, spacing=8)
        for text in self.SUGGESTIONS:
            b = QPushButton(text)
            b.setObjectName("Chip")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, t=text: self.send(t))
            self.suggest.addWidget(b)
        v.addWidget(self.suggest_box)

        self.view = _doc_view(self._on_link)
        v.addWidget(self.view, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.input = QLineEdit()
        self.input.setObjectName("Search")
        self.input.setPlaceholderText("Например: что Денис говорил про миграцию базы?")
        self.input.returnPressed.connect(lambda: self.send(self.input.text()))
        bottom.addWidget(self.input, 1)
        self.btn_send = QPushButton("Спросить")
        self.btn_send.setObjectName("Primary")
        self.btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
        set_icon(self.btn_send, "send", "on_accent")
        self.btn_send.clicked.connect(self._on_button)
        bottom.addWidget(self.btn_send)
        v.addLayout(bottom)

        self.render_timer = QTimer(self, interval=150, singleShot=True, timeout=self._render)
        self._render()

    # ---------- состояние ----------

    def set_scope_call(self, call: Call | None):
        self.scope_call = call
        self._update_scope_hint()

    def _update_scope_hint(self):
        if self.scope.index == 1:
            self.scope_hint.setText(f"«{self.scope_call.title}»" if self.scope_call else "Сначала выберите созвон в списке")
        else:
            n = len([c for c in self._get_calls() if c.has_transcript()])
            self.scope_hint.setText(f"{n} с транскриптом")

    def refresh(self):
        cfg = self._get_config()
        if llm.ready(cfg):
            self.banner.hide()
        else:
            self.banner.set("warn", "key", f"Чтобы задавать вопросы, {llm.not_ready_text(cfg)}",
                            "Вопросы задаются той же модели, что пишет саммари.",
                            button=("Открыть настройки", self._open_settings))
        self._update_scope_hint()
        self.input.setFocus()

    def reset(self):
        if self.job:
            self.job.stopped = True
            self.job = None
        self.history.clear()
        self._pending = self._live = ""
        self._busy(False)
        self._render()

    def _busy(self, busy: bool):
        self.input.setEnabled(not busy)
        self.btn_send.setText("Остановить" if busy else "Спросить")
        set_icon(self.btn_send, "stop" if busy else "send", "on_accent")

    # ---------- вопрос и ответ ----------

    def _on_button(self):
        if self.job:
            self.job.stopped = True
            self.job = None
            self.history.append((self._pending, self._live + "\n\n*(остановлено)*"))
            self._pending = self._live = ""
            self._busy(False)
            self._render()
        else:
            self.send(self.input.text())

    def send(self, question: str):
        question = question.strip()
        cfg = self._get_config()
        if not question or self.job:
            return
        if not llm.ready(cfg):
            self.refresh()
            return
        scope = self.scope_call if self.scope.index == 1 else None
        if self.scope.index == 1 and scope is None:
            return
        calls = [c for c in self._get_calls() if c.has_transcript() or c.summary()]
        history = list(self.history)
        self._pending, self._live = question, ""
        self.input.clear()
        self._busy(True)
        job = Job(lambda on_text, stopped: ask.answer(cfg, question, history, calls, scope, on_text, stopped))
        job.chunk.connect(lambda t, j=job: self._on_chunk(j, t))
        job.done.connect(lambda text, j=job: self._on_done(j, text))
        job.failed.connect(lambda msg, j=job: self._on_done(j, f"**Не получилось ответить:** {msg}"))
        self.job = job
        job.start()
        self._render()

    def _on_chunk(self, job: Job, text: str):
        if job is self.job:
            self._live += text
            if not self.render_timer.isActive():
                self.render_timer.start()

    def _on_done(self, job: Job, text: str):
        if job is not self.job:
            return
        self.job = None
        self.history.append((self._pending, text))
        self._pending = self._live = ""
        self._busy(False)
        self._render()
        self.input.setFocus()

    def _render(self):
        self.suggest_box.setVisible(not self.history and not self._pending)
        if not self.history and not self._pending:
            render.show_placeholder(self.view, "Спросите что-нибудь о своих созвонах",
                                    "Поиск идёт по транскриптам на этом компьютере; модели отправляются только "
                                    "подходящие фрагменты.")
            return
        calls = {c.id: c for c in self._get_calls()}
        parts = []
        for q, a in self.history + ([(self._pending, self._live or "*Ищу в созвонах…*")] if self._pending else []):
            parts.append(f"### {q}\n\n{link_citations(a, calls)}")
        render.show_summary(self.view, "\n\n".join(parts), keep_title=True)
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_link(self, url: QUrl):
        if url.scheme() == "call":
            cid, _, at = url.path().partition("@")
            self._open_call(cid, float(at) if at else None)
        elif url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)


class DigestPage(QWidget):
    ready = Signal(object)  # понедельник недели, итоги которой готовы

    def __init__(self, get_calls: Callable[[], list[Call]], get_config: Callable[[], dict],
                 open_call: Callable[[str, float | None], None], open_settings: Callable[[], None]):
        super().__init__()
        self._get_calls, self._get_config, self._open_call = get_calls, get_config, open_call
        self._open_settings = open_settings
        self.monday = digest.week_start(date.today())
        self.job: Job | None = None
        self.job_week: date | None = None
        self._live = ""

        v = QVBoxLayout(self)
        v.setContentsMargins(34, 26, 34, 24)
        v.setSpacing(14)
        head, self.sub = _page_header("Итоги недели", "")
        top = QHBoxLayout()
        top.addLayout(head, 1)
        self.btn_prev = icon_button("chevron_left", "Предыдущая неделя", lambda: self.shift(-7))
        self.btn_next = icon_button("chevron_right", "Следующая неделя", lambda: self.shift(7))
        self.btn_build = QPushButton("Собрать итоги")
        self.btn_build.setObjectName("Primary")
        self.btn_build.setCursor(Qt.CursorShape.PointingHandCursor)
        set_icon(self.btn_build, "sparkles", "on_accent")
        self.btn_build.clicked.connect(lambda: self.generate(self.monday))
        for w in (self.btn_prev, self.btn_next, self.btn_build):
            top.addWidget(w, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(top)
        self.banner = Banner()
        self.banner.hide()
        v.addWidget(self.banner)
        self.view = _doc_view(self._on_link)
        v.addWidget(self.view, 1)
        self.render_timer = QTimer(self, interval=150, singleShot=True, timeout=self.refresh)

    def shift(self, days: int):
        self.monday += timedelta(days=days)
        self.refresh()

    def show_week(self, monday: date):
        self.monday = monday
        self.refresh()

    def refresh(self):
        calls = self._get_calls()
        week = digest.calls_in_week(calls, self.monday)
        text, built_from = digest.load(self.monday)
        this_week = self.monday == digest.week_start(date.today())
        self.btn_next.setEnabled(not this_week)
        n = len(week)
        label = digest.period(self.monday) + (" (эта неделя)" if this_week else "")
        self.sub.setText(f"{label} · созвонов с саммари: {n}")
        cfg = self._get_config()
        busy = self.job is not None and self.job_week == self.monday
        self.btn_build.setEnabled(bool(week) and llm.ready(cfg) and self.job is None)
        self.btn_build.setText("Обновить" if text else "Собрать итоги")
        new = {c.id for c in week} - set(built_from)
        if not llm.ready(cfg):
            self.banner.set("warn", "key", f"Чтобы собирать итоги, {llm.not_ready_text(cfg)}",
                            button=("Открыть настройки", self._open_settings))
        elif text and new and not busy:
            self.banner.set("info", "refresh", "С тех пор появились новые созвоны",
                            "Нажмите «Обновить», чтобы учесть их в итогах.")
        else:
            self.banner.hide()
        if busy:
            render.show_summary(self.view, link_citations(self._live, {c.id: c for c in calls}) or "*Собираю итоги…*")
        elif text:
            render.show_summary(self.view, link_citations(text, {c.id: c for c in calls}))
        elif week:
            render.show_placeholder(self.view, "Итоги этой недели ещё не собраны",
                                    "Нажмите «Собрать итоги»: модель прочитает саммари всех созвонов недели "
                                    "и выделит главное, решения, задачи по людям и то, что зависло.")
        else:
            render.show_placeholder(self.view, "На этой неделе нет созвонов с саммари")

    def generate(self, monday: date, quiet: bool = False) -> bool:
        cfg = self._get_config()
        if self.job is not None or not llm.ready(cfg):
            return False
        calls = list(self._get_calls())
        self.job_week, self._live = monday, ""
        job = Job(lambda on_text, stopped: digest.build(cfg, calls, monday, on_text, stopped))
        job.chunk.connect(self._on_chunk)
        job.done.connect(lambda _t: self._finish(monday, None, quiet))
        job.failed.connect(lambda msg: self._finish(monday, msg, quiet))
        self.job = job
        job.start()
        self.refresh()
        return True

    def _on_chunk(self, text: str):
        self._live += text
        if self.job_week == self.monday and not self.render_timer.isActive():
            self.render_timer.start()

    def _finish(self, monday: date, error: str | None, quiet: bool):
        self.job = None
        self.job_week = None
        self.refresh()
        if error and not quiet:
            self.banner.set("error", "alert", "Не получилось собрать итоги", error)
        elif not error:
            self.ready.emit(monday)

    def _on_link(self, url: QUrl):
        if url.scheme() == "call":
            cid, _, at = url.path().partition("@")
            self._open_call(cid, float(at) if at else None)
