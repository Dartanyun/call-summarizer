"""Оформление саммари (Markdown) и транскрипта для показа в окне."""

import html
import re

from PySide6.QtGui import QColor, QFont, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument
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


def show_summary(view: QTextBrowser, md: str) -> None:
    doc = view.document()
    doc.setDefaultFont(_base_font())
    doc.setDocumentMargin(26)
    doc.setMarkdown(linkify_timecodes(strip_title(md)), QTextDocument.MarkdownFeature.MarkdownDialectGitHub)

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
