"""Экспорт созвона: Markdown, PDF, Word (DOCX), сообщение для Telegram и заметка Obsidian."""

import re
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import sections, speakers
from .storage import Call

_H1 = re.compile(r"\A\s*#\s+[^\n]*\n*")


def safe_name(title: str) -> str:
    """Имя файла из названия встречи: без символов, запрещённых в Windows и Linux."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title).strip(" .")
    return re.sub(r"\s+", " ", name)[:100] or "созвон"


def _duration(sec: float) -> str:
    m = round(sec / 60)
    return f"{m // 60} ч {m % 60} мин" if m >= 60 else f"{max(m, 1)} мин"


def _people(call: Call) -> list[str]:
    return [n for n in call.participants() if n != speakers.default_names([speakers.ME])[speakers.ME]]


def body(call: Call) -> str:
    """Саммари без заголовка (заголовок — название созвона, его ставим сами)."""
    return _H1.sub("", call.summary()).strip()


def info_line(call: Call) -> str:
    parts = [call.created.strftime("%d.%m.%Y, %H:%M")]
    if call.duration:
        parts.append(_duration(call.duration))
    people = _people(call)
    if people:
        parts.append("участники: " + ", ".join(people))
    return " · ".join(parts)


def markdown(call: Call, with_transcript: bool = True) -> str:
    parts = [f"# {call.title}", f"*{info_line(call)}*"]
    if call.summary():
        parts.append(body(call))
    if with_transcript and call.has_transcript():
        parts.append("## Транскрипт\n\n" + call.transcript())
    return "\n\n".join(parts) + "\n"


def save_markdown(call: Call, path: Path) -> None:
    path.write_text(markdown(call), encoding="utf-8")


def save_pdf(call: Call, path: Path) -> None:
    """PDF средствами Qt: шрифты встраиваются, кириллица на месте. Вызывать из потока окна."""
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPdfWriter, QTextDocument

    from . import theme

    doc = QTextDocument()
    font = QFont(theme.ui_font())
    font.setPointSizeF(10.5)
    doc.setDefaultFont(font)
    doc.setMarkdown(markdown(call), QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    writer = QPdfWriter(str(path))
    writer.setTitle(call.title)
    writer.setCreator("Саммари созвонов")
    writer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait,
                                     QMarginsF(18, 16, 18, 16), QPageLayout.Unit.Millimeter))
    writer.setResolution(300)
    # размер страницы документа не задаём: тогда Qt сам разобьёт текст на страницы A4
    # и пересчитает размер шрифта под разрешение PDF
    doc.print_(writer)


# ---------------- DOCX ----------------

_INLINE = re.compile(r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|_[^_\s][^_]*_|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def _runs(text: str, bold: bool = False) -> str:
    """Строка Markdown → фрагменты Word с жирным, курсивом и моноширинным текстом."""
    out = []
    for piece in _INLINE.split(text):
        if not piece:
            continue
        b, i, mono = bold, False, False
        if piece.startswith(("**", "__")) and len(piece) > 4:
            piece, b = piece[2:-2], True
        elif piece.startswith("`"):
            piece, mono = piece[1:-1], True
        elif piece.startswith("[") and "](" in piece:
            piece = piece[1:piece.index("](")]
        elif piece[0] in "*_" and piece[-1] == piece[0] and len(piece) > 2:
            piece, i = piece[1:-1], True
        props = ("<w:b/>" if b else "") + ("<w:i/>" if i else "") + (
            '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>' if mono else "")
        rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(piece)}</w:t></w:r>')
    return "".join(out)


def _para(runs: str, style: str = "", indent: int = 0) -> str:
    ppr = ""
    if style or indent:
        ppr = "<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "") + (
            f'<w:ind w:left="{indent}" w:hanging="280"/>' if indent else "") + "</w:pPr>"
    return f"<w:p>{ppr}{runs}</w:p>"


def _md_to_docx_body(md: str) -> list[str]:
    paras = []
    for line in md.splitlines():
        if not line.strip() or line.strip() in ("---", "***"):
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            paras.append(_para(_runs(m.group(2)), f"Heading{len(m.group(1))}"))
            continue
        m = re.match(r"^(\s*)(?:[-*+]|(\d+[.)]))\s+(?:\[( |x|X)\]\s+)?(.*)$", line)
        if m:
            level = len(m.group(1).replace("\t", "  ")) // 2
            mark = (m.group(2) + " ") if m.group(2) else ("☑ " if (m.group(3) or "").lower() == "x"
                                                          else "☐ " if m.group(3) else "• ")
            paras.append(_para(_runs(mark + m.group(4)), "ListParagraph", indent=420 + 360 * level))
            continue
        paras.append(_para(_runs(line.strip())))
    return paras


_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>
<w:sz w:val="22"/><w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
<w:pPr><w:keepNext/><w:spacing w:before="0" w:after="160"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
<w:pPr><w:keepNext/><w:spacing w:before="280" w:after="100"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
<w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>
<w:pPr><w:spacing w:after="60"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Subtle"><w:name w:val="Subtle"/><w:basedOn w:val="Normal"/><w:rPr><w:i/><w:color w:val="666666"/></w:rPr></w:style>
</w:styles>"""

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def save_docx(call: Call, path: Path) -> None:
    """Документ Word без сторонних библиотек: заголовки, списки, чек-листы, транскрипт."""
    paras = [_para(_runs(call.title), "Heading1"), _para(_runs(info_line(call)), "Subtle")]
    if call.summary():
        paras += _md_to_docx_body(body(call))
    if call.has_transcript():
        paras.append(_para(_runs("Транскрипт"), "Heading2"))
        names = call.speaker_names()
        for p in call.paragraphs():
            who = names.get(p.get("speaker"), "") if p.get("speaker") else ""
            s = int(p["start"])
            head = f"[{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}] " + (f"{who}: " if who else "")
            paras.append(_para(_runs(head, bold=True) + _runs(p["text"])))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                + "".join(paras)
                + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                  '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="708" w:footer="708" w:gutter="0"/>'
                  "</w:sectPr></w:body></w:document>")
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%SZ")
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{escape(call.title)}</dc:title><dc:creator>Саммари созвонов</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created></cp:coreProperties>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("docProps/core.xml", core)


# ---------------- мессенджеры ----------------

TELEGRAM_LIMIT = 4096


def telegram_text(call: Call) -> str:
    """Сообщение для Telegram: **жирные** заголовки, «•» и «☐» вместо разметки, без таймкодов и хронологии.

    Telegram Desktop сам превращает **текст** в жирный при отправке, в других мессенджерах звёздочки
    просто не мешают читать."""
    md = sections.without_section(body(call), sections.CHAPTER_HEADINGS)
    lines = [f"**{call.title}**", info_line(call), ""]
    for line in md.splitlines():
        line = re.sub(r"\s*" + sections.TIMECODE.pattern, "", line).rstrip()
        m = re.match(r"^#{1,3}\s+(.*)$", line)
        if m:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"**{m.group(1).strip()}**")
            continue
        m = re.match(r"^(\s*)[-*+]\s+(?:\[( |x|X)\]\s+)?(.*)$", line)
        if m:
            mark = "☑" if (m.group(2) or "").lower() == "x" else "☐" if m.group(2) else "•"
            lines.append(f"{'  ' * (len(m.group(1)) // 2)}{mark} {m.group(3)}")
            continue
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


# ---------------- Obsidian ----------------

def _yaml(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def obsidian_note(call: Call) -> str:
    """Заметка с метаданными (дата, участники-ссылки, теги) — их видят поиск, Dataview и граф Obsidian."""
    people = _people(call)
    front = ["---", f"date: {call.created:%Y-%m-%d %H:%M}"]
    if call.duration:
        front.append(f"duration: {_duration(call.duration)}")
    if people:
        front.append("participants:")
        front += [f"  - {_yaml(f'[[{p}]]')}" for p in people]
    cal = call.meta.get("calendar") or {}
    if cal.get("title"):
        front.append(f"calendar: {_yaml(cal['title'])}")
    if call.meta.get("template"):
        front.append(f"type: {call.meta['template']}")
    front += ["tags:", "  - созвон", "---"]
    parts = ["\n".join(front), f"# {call.title}"]
    if call.summary():
        parts.append(body(call))
    if call.has_transcript():
        quoted = "\n".join(f"> {line}" if line else ">" for line in call.transcript().splitlines())
        parts.append(f"> [!note]- Транскрипт\n{quoted}")
    return "\n\n".join(parts) + "\n"


def save_obsidian(call: Call, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    # одна заметка на созвон: при повторном экспорте обновляем тот же файл
    name = call.meta.get("obsidian_file") or f"{call.created:%Y-%m-%d %H-%M} {safe_name(call.title)}.md"
    path = folder / name
    path.write_text(obsidian_note(call), encoding="utf-8")
    if call.meta.get("obsidian_file") != name:
        call.save(obsidian_file=name)
    return path


def auto_obsidian(call: Call, cfg: dict) -> Path | None:
    folder = (cfg.get("obsidian_dir") or "").strip()
    if not (folder and cfg.get("obsidian_auto")):
        return None
    try:
        return save_obsidian(call, Path(folder).expanduser())
    except OSError:
        return None
