"""«Спросить у созвонов»: вопрос по всем записям или по одной — ответ со ссылками на моменты.

Транскрипты ищутся прямо на компьютере (BM25 по кускам разговора), модели отправляются
только подходящие куски, краткий каталог созвонов и саммари самых подходящих из них.
"""

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from . import llm, sections, transcribe
from .storage import Call

SYSTEM = """\
Ты помогаешь человеку разбираться в его рабочих созвонах: отвечаешь на вопросы по транскриптам \
и саммари. Транскрипты распознаны автоматически, в них бывают ошибки в словах и именах; «Я» — \
это сам пользователь.

Правила:
- Отвечай только по материалам из сообщения. Если ответа в них нет, так и скажи и предложи, \
как переформулировать вопрос.
- После каждого утверждения ставь ссылку на источник в формате [[ID@ЧЧ:ММ:СС]] — ID созвона \
из материалов и момент записи. Если момент неизвестен — [[ID]]. Не выдумывай ID и время.
- Отвечай по-русски, кратко и по делу; Markdown можно."""

CITATION = re.compile(r"\[\[([\w.-]+?)(?:@(\d{1,2}:\d{2}(?::\d{2})?))?\]\]")

_STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было
вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам
ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз
тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее
сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про
всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более
всегда конечно всю между это мы наш наша наши""".split())


def _stem(word: str) -> str:
    # грубое приведение русских слов к основе: «миграции», «миграцию» → «миграц»
    return word[:6] if len(word) > 6 else word


def tokens(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"\w+", text.lower()) if len(w) > 2 and w not in _STOP]


@dataclass
class Chunk:
    call: Call
    start: float
    text: str


def chunks(call: Call, words_per_chunk: int = 140) -> list[Chunk]:
    """Транскрипт кусками примерно по 140 слов, со временем и именами говорящих."""
    names = call.speaker_names()
    out, lines, start, count = [], [], None, 0
    for p in call.paragraphs():
        who = names.get(p.get("speaker"), "") if p.get("speaker") else ""
        lines.append(f"[{transcribe._ts(p['start'])}] {who + ': ' if who else ''}{p['text']}")
        start = p["start"] if start is None else start
        count += len(p["text"].split())
        if count >= words_per_chunk:
            out.append(Chunk(call, start, "\n".join(lines)))
            lines, start, count = [], None, 0
    if lines:
        out.append(Chunk(call, start or 0.0, "\n".join(lines)))
    return out


class Index:
    """BM25 по кускам транскриптов всех созвонов."""

    def __init__(self, calls: list[Call]):
        self.chunks: list[Chunk] = []
        self.tf: list[Counter] = []
        for call in calls:
            for ch in chunks(call):
                self.chunks.append(ch)
                self.tf.append(Counter(tokens(ch.text)))
        self.df: Counter = Counter()
        for tf in self.tf:
            self.df.update(tf.keys())
        self.avg = sum(sum(tf.values()) for tf in self.tf) / max(1, len(self.tf))

    def search(self, query: str, k: int = 20) -> list[Chunk]:
        q = set(tokens(query))
        if not q or not self.chunks:
            return []
        n = len(self.chunks)
        scored = []
        for i, tf in enumerate(self.tf):
            length = sum(tf.values()) or 1
            score = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                    score += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * length / self.avg))
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        return [self.chunks[i] for _s, i in scored[:k]]


def _catalog_line(call: Call) -> str:
    short = sections.section(call.summary(), ("кратко",)).replace("\n", " ")
    short = sections.TIMECODE.sub("", short).strip()
    people = ", ".join(n for n in call.participants() if n != "Я")
    return (f"- {call.id} | {call.created:%d.%m.%Y %H:%M} | {call.title}"
            + (f" | участники: {people}" if people else "") + (f" | {short[:280]}" if short else ""))


def context(calls: list[Call], question: str, scope: Call | None = None, budget: int = 70000) -> str:
    """Материалы для ответа: каталог созвонов, подходящие саммари и куски транскриптов."""
    if scope is not None:
        parts = [f"<call id=\"{scope.id}\">\n{_catalog_line(scope)}"]
        if scope.summary():
            parts.append(f"<summary>\n{scope.summary()}\n</summary>")
        transcript = "\n\n".join(ch.text for ch in chunks(scope))
        if len(transcript) > budget:
            # длинный созвон: целиком не помещается — берём куски, подходящие к вопросу
            found = Index([scope]).search(question, k=40)
            transcript = "\n\n".join(ch.text for ch in sorted(found, key=lambda c: c.start))[:budget]
        parts.append(f"<transcript>\n{transcript}\n</transcript>\n</call>")
        return "\n".join(parts)

    catalog = "\n".join(_catalog_line(c) for c in calls[:300])
    found = Index(calls).search(question, k=30)
    picked, used = [], len(catalog)
    for ch in found:
        if used + len(ch.text) > budget * 0.6:
            break
        picked.append(ch)
        used += len(ch.text)
    # саммари созвонов, где нашлось больше всего подходящего
    best = [cid for cid, _n in Counter(ch.call.id for ch in picked).most_common(5)]
    by_id = {c.id: c for c in calls}
    summaries = []
    for cid in best:
        s = by_id[cid].summary()
        if s and used + len(s) < budget:
            summaries.append(f'<summary call="{cid}">\n{s}\n</summary>')
            used += len(s)
    excerpts = "\n\n".join(f'<excerpt call="{ch.call.id}">\n{ch.text}\n</excerpt>'
                           for ch in sorted(picked, key=lambda c: (c.call.created, c.start)))
    return (f"<calls>\nВсе созвоны (ID | дата | название | участники | кратко):\n{catalog}\n</calls>\n\n"
            + "\n\n".join(summaries) + ("\n\n" if summaries else "")
            + (f"<excerpts>\nФрагменты транскриптов, подходящие к вопросу:\n{excerpts}\n</excerpts>" if excerpts
               else "<excerpts>Подходящих фрагментов транскриптов не нашлось.</excerpts>"))


def answer(cfg: dict, question: str, history: list[tuple[str, str]], calls: list[Call], scope: Call | None,
           on_text: Callable[[str], None], cancelled: Callable[[], bool] = lambda: False) -> str:
    """history — прошлые пары (вопрос, ответ) в этом разговоре: для уточняющих вопросов."""
    messages: list[llm.Message] = []
    for q, a in history[-6:]:
        messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    # уточняющий вопрос («а кто это делает?») ищем вместе с предыдущим
    search_for = " ".join([q for q, _a in history[-1:]] + [question])
    material = context(calls, search_for, scope)
    messages.append({"role": "user", "content": [
        llm.Block(f"<materials>\n{material}\n</materials>"), llm.Block(f"Вопрос: {question}")]})
    text, truncated = llm.stream(cfg, SYSTEM, messages, on_text, cancelled, max_tokens=16000)
    return text + ("\n\n*(Ответ обрезан по длине.)*" if truncated else "")
