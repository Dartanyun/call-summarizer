"""Оформление саммари (Markdown) и транскрипта для показа в окне."""

import html
import re

from PySide6.QtGui import QColor, QFont, QImage, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import QTextBrowser

from . import theme

def _base_font() -> QFont:
    f = theme.ui_font()
    f.setPointSizeF(10.5)
    return f


_TIMECODE = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\](?!\()")


def _seconds(tc: str) -> int:
    parts = [int(x) for x in tc.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def linkify_timecodes(md: str) -> str:
    """[00:12:34] → ссылка, по которой плеер переходит к этому месту записи."""
    return _TIMECODE.sub(lambda m: f"[{m.group(1)}](t:{_seconds(m.group(1))})", md)


def strip_title(md: str) -> str:
    """Заголовок встречи уже показан над саммари — не дублируем его."""
    return re.sub(r"\A\s*#\s+[^\n]*\n+", "", md)


def show_summary(view: QTextBrowser, md: str, keep_title: bool = False) -> None:
    doc = view.document()
    doc.setDefaultFont(_base_font())
    doc.setDocumentMargin(26)
    doc.setMarkdown(linkify_timecodes(md if keep_title else strip_title(md)),
                    QTextDocument.MarkdownFeature.MarkdownDialectGitHub)

    sizes = {1: 15.0, 2: 12.5, 3: 11.0}
    block = doc.begin()
    first = True
    while block.isValid():
        cur = QTextCursor(block)
        cur.setPosition(block.position())
        cur.setPosition(block.position() + max(0, block.length() - 1), QTextCursor.MoveMode.KeepAnchor)
        bf = QTextBlockFormat()
        level = block.blockFormat().headingLevel()
        if level:
            cf = QTextCharFormat()
            cf.setFontPointSize(sizes.get(level, 11.0))
            cf.setFontWeight(QFont.Weight.Bold)
            cf.setForeground(QColor(theme.C["text"]))
            cur.mergeCharFormat(cf)
            bf.setTopMargin(0 if first else 20)
            bf.setBottomMargin(8)
        else:
            in_list = block.textList() is not None
            bf.setLineHeight(135 if in_list else 150, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            bf.setBottomMargin(4 if in_list else 8)
        cur.mergeBlockFormat(bf)
        first = False
        block = block.next()
    # курсор — в начало, иначе при первой раскладке окна текст прокрутится к концу
    view.moveCursor(QTextCursor.MoveOperation.Start)
    view.verticalScrollBar().setValue(0)


def show_placeholder(view: QTextBrowser, title: str, text: str = "") -> None:
    view.document().setDocumentMargin(26)
    sub = f'<p style="color:{theme.C["muted"]}">{html.escape(text)}</p>' if text else ""
    view.setHtml(
        f'<div align="center" style="font-family:\'{_base_font().family()}\'"><br><br><br>'
        f'<p style="font-size:12pt; font-weight:600; color:{theme.C["muted"]}">{html.escape(title)}</p>{sub}</div>'
    )


def show_transcript(view: QTextBrowser, paragraphs: list[dict], names: dict[str, str], active: int = -1) -> None:
    """Таймкоды — ссылки «t:секунды» для плеера; active — реплика, которая сейчас звучит."""
    mono = theme.mono_family()
    colors = theme.speaker_colors(names)
    rows = []
    prev = object()
    for i, p in enumerate(paragraphs):
        sid = p.get("speaker")
        # имя показываем, только когда говорящий сменился
        who = ""
        if sid and sid != prev:
            who = (f'<p style="margin:0 0 3px 0; color:{colors.get(sid, theme.C["muted"])}; font-weight:700">'
                   f"{html.escape(names.get(sid, sid))}</p>")
        prev = sid
        s = int(p["start"])
        ts = f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
        bg = f' background-color:{theme.C["accent_soft"]};' if i == active else ""
        rows.append(
            "<tr>"
            f'<td width="84" valign="top" style="padding: {"21" if who else "3"}px 8px 14px 6px;{bg}">'
            f'<a name="p{i}" href="t:{p["start"]}" style="color:{theme.C["faint"]}; text-decoration:none;'
            f' font-family:\'{mono}\'; font-size:9pt">{ts}</a></td>'
            f'<td valign="top" style="padding: 0 6px 14px 0;{bg}">{who}'
            f'<p style="line-height:140%; margin:0">{html.escape(p["text"])}</p></td>'
            "</tr>"
        )
    view.document().setDocumentMargin(26)
    view.setHtml(
        f'<div style="font-family:\'{_base_font().family()}\'; font-size:10.5pt">'
        f'<table cellspacing="0" cellpadding="0" width="100%">{"".join(rows)}</table></div>'
    )


# ---------------- статистика ----------------

def _dur(sec: float) -> str:
    s = int(round(sec))
    if s < 60:
        return f"{s} с"
    if s < 3600:
        return f"{s // 60} мин {s % 60:02d} с" if s < 600 else f"{round(s / 60)} мин"
    return f"{s // 3600} ч {s % 3600 // 60:02d} мин"


def _donut(shares: list[tuple[float, str]], size: int = 172) -> QImage:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QPainter, QPen

    img = QImage(size * 2, size * 2, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    img.setDevicePixelRatio(2)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(16, 16, size - 32, size - 32)
    total = sum(v for v, _ in shares) or 1.0
    start = 90 * 16
    gap = 2 * 16 if len(shares) > 1 else 0
    for value, color in shares:
        span = int(-360 * 16 * value / total)
        pen = QPen(QColor(color), 24)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        p.drawArc(rect, start - gap // 2, span + gap if abs(span) > gap else span)
        start += span
    p.end()
    return img


def show_stats(view: QTextBrowser, st, colors: dict[str, str], narrow: bool = False) -> None:
    """st — stats.Stats; colors — цвет каждого участника по имени; narrow — диаграмма над таблицей."""
    from PySide6.QtCore import QUrl

    doc = view.document()
    doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl("stats:donut"),
                    _donut([(x.talk, colors.get(x.name, theme.C["accent"])) for x in st.people]))
    muted, faint, text = theme.C["muted"], theme.C["faint"], theme.C["text"]
    total = st.talk_total or 1.0

    def card(value: str, label: str) -> str:
        size = "14pt" if narrow else "17pt"
        return (f'<td style="padding:0 {18 if narrow else 26}px 0 0"><p style="margin:0; font-size:{size}; '
                f'font-weight:700; white-space:nowrap">{value}</p>'
                f'<p style="margin:2px 0 0 0; color:{muted}">{html.escape(label)}</p></td>')

    talk_pct = round(st.talk_total / st.duration * 100) if st.duration else 0
    cards = "".join([card(_dur(st.duration), "длительность"), card(f"{talk_pct}%", "времени говорили"),
                     card(str(st.turns), "реплик"), card(str(st.interruptions), "перебиваний")])
    head = "".join(f'<th align="{a}" style="color:{faint}; font-weight:600; padding:0 14px 6px 0">{h}</th>'
                   for h, a in (("", "left"), ("Доля", "right"), ("Время", "right"), ("Слов/мин", "right"),
                                ("Реплик", "right"), ("Перебивал(а)", "right")))
    rows, monologues = [], []
    for x in st.people:
        color = colors.get(x.name, theme.C["accent"])
        at = int(x.longest_at)
        when = f"{at // 3600}:{at % 3600 // 60:02d}:{at % 60:02d}" if at >= 3600 else f"{at // 60}:{at % 60:02d}"
        mono = (f'<a href="t:{x.longest_at}" style="color:{theme.C["accent"]}; text-decoration:none">'
                f"{when} · {_dur(x.longest)}</a>") if x.longest else "—"
        if x.longest:
            monologues.append(f"<b>{html.escape(x.name)}</b> — {mono}")
        cells = [f'<span style="color:{color}">●</span>&nbsp; <b>{html.escape(x.name)}</b>',
                 f"{round(x.talk / total * 100)}%", _dur(x.talk), str(x.wpm or "—"), str(x.turns),
                 str(x.interruptions)]
        rows.append("<tr>" + "".join(
            f'<td align="{"left" if i == 0 else "right"}" style="padding:5px 16px 5px 0">{c}</td>'
            for i, c in enumerate(cells)) + "</tr>")
    fillers = []
    for x in st.people:
        top = x.fillers.most_common(4)
        if top:
            words = ", ".join(f"«{w}» ×{n}" for w, n in top)
            fillers.append(f'<p style="margin:0 0 6px 0"><b>{html.escape(x.name)}:</b> {html.escape(words)}</p>')
    chips = " ".join(f'<span style="color:{text}">{html.escape(w)}</span>'
                     f'<span style="color:{faint}"> {n}</span>&nbsp;&nbsp;' for w, n in st.top_words)

    def h(title: str) -> str:
        return f'<p style="margin:22px 0 8px 0; font-size:12.5pt; font-weight:700">{title}</p>'

    body = (f'<table cellspacing="0" cellpadding="0"><tr>{cards}</tr></table>'
            + h("Кто сколько говорил")
            + ('<p><img src="stats:donut" width="172" height="172"></p>'
               f'<table cellspacing="0" cellpadding="0"><tr>{head}</tr>{"".join(rows)}</table>' if narrow else
               '<table cellspacing="0" cellpadding="0"><tr><td valign="middle" style="padding-right:22px">'
               '<img src="stats:donut" width="172" height="172"></td><td valign="middle">'
               f'<table cellspacing="0" cellpadding="0"><tr>{head}</tr>{"".join(rows)}</table></td></tr></table>')
            + (h("Самые длинные монологи") + "<p>" + " &nbsp;·&nbsp; ".join(monologues) + "</p>" if monologues else "")
            + (h("Слова-паразиты") + "".join(fillers) if fillers else "")
            + (h("Чаще всего звучало") + f"<p>{chips}</p>" if chips else "")
            + f'<p style="margin-top:22px; color:{faint}">Время считается по распознанным репликам; '
              "«перебивал(а)» — вступил(а), когда другой ещё не закончил фразу. Тишина и паузы — "
              f"{_dur(st.silence)}.</p>")
    doc.setDocumentMargin(26)
    view.setHtml(f'<div style="font-family:\'{_base_font().family()}\'; font-size:10.5pt">{body}</div>')
