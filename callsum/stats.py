"""Статистика разговора: кто сколько говорил, темп речи, перебивания, монологи, слова-паразиты."""

import re
from collections import Counter
from dataclasses import dataclass, field

FILLERS = ["ну", "вот", "как бы", "типа", "короче", "это самое", "в общем", "значит", "собственно",
           "так сказать", "блин", "в принципе", "как его", "слушай", "на самом деле", "реально",
           "um", "uh", "like", "you know", "basically", "actually"]
_FILLER_RE = re.compile(r"(?<!\w)(" + "|".join(sorted(map(re.escape, FILLERS), key=len, reverse=True)) + r")(?!\w)")

# частые служебные слова — их нет смысла показывать среди «чаще всего звучало»
_STOP = set("""
а без более бы был была были было быть в вам вас ваш весь во вот все всё всего всех вы где да даже для до его
ее её если есть ещё еще же за здесь и из или им их к как какой когда кто ли либо мне может мы на над надо наш не
него нее неё нет ни них но ну о об однако он она они оно от очень по под при про раз с сам свой себя со так также
такой там те тем то того тоже той только том ты у уже хотя чего чей чем что чтобы чье чья эта эти это этот я
можно нужно будет будем буду будут сейчас тогда потом вообще просто давайте давай типа короче значит например
сказать говорю говорит знаю думаю понятно хорошо ладно окей ага угу смотри слушай мой моя мои твой
the a an and or but to of in on at for is are was were be it this that with as by from we you i they he she
""".split())


@dataclass
class Person:
    name: str
    sids: list[str]
    talk: float = 0.0
    words: int = 0
    turns: int = 0
    interruptions: int = 0
    longest: float = 0.0
    longest_at: float = 0.0
    fillers: Counter = field(default_factory=Counter)

    @property
    def wpm(self) -> int:
        return round(self.words / (self.talk / 60)) if self.talk >= 30 else 0


@dataclass
class Stats:
    duration: float
    people: list[Person]
    talk_total: float
    turns: int
    interruptions: int
    top_words: list[tuple[str, int]]

    @property
    def silence(self) -> float:
        return max(0.0, self.duration - self.talk_total)


def compute(paragraphs: list[dict], names: dict[str, str], duration: float) -> Stats | None:
    paras = [p for p in paragraphs if p.get("speaker")]
    if not paras:
        return None
    # спикеры с одинаковым именем — один человек (как в списке участников)
    people: dict[str, Person] = {}
    for sid in dict.fromkeys(p["speaker"] for p in paras):
        name = names.get(sid, sid)
        people.setdefault(name, Person(name, []))
        people[name].sids.append(sid)
    by_sid = {sid: person for person in people.values() for sid in person.sids}

    vocab: Counter = Counter()
    for p in paras:
        person = by_sid[p["speaker"]]
        text = p["text"].lower()
        person.talk += max(0.0, p["end"] - p["start"])
        tokens = re.findall(r"\w+", text)
        person.words += len(tokens)
        person.fillers.update(m.group(1) for m in _FILLER_RE.finditer(text))
        vocab.update(t for t in tokens if len(t) > 3 and t not in _STOP and not t.isdigit())

    # реплики: подряд идущие абзацы одного человека с паузой меньше двух секунд
    turns = 0
    prev = None
    turn_start = turn_end = 0.0
    for p in paras:
        person = by_sid[p["speaker"]]
        if prev is not None and person is by_sid[prev["speaker"]] and p["start"] - turn_end < 2.0:
            turn_end = p["end"]
        else:
            if prev is not None:
                _close_turn(by_sid[prev["speaker"]], turn_start, turn_end)
                # перебил: предыдущий не договорил фразу, а новый вступил почти сразу
                if person is not by_sid[prev["speaker"]] and p["start"] - prev["end"] < 0.6 \
                        and not re.search(r"[.?!…]\W*$", prev["text"].strip()):
                    person.interruptions += 1
            person.turns += 1
            turns += 1
            turn_start, turn_end = p["start"], p["end"]
        prev = p
    _close_turn(by_sid[prev["speaker"]], turn_start, turn_end)

    ordered = sorted(people.values(), key=lambda x: -x.talk)
    total = sum(x.talk for x in ordered)
    return Stats(duration=max(duration, paras[-1]["end"]), people=ordered, talk_total=total, turns=turns,
                 interruptions=sum(x.interruptions for x in ordered), top_words=vocab.most_common(12))


def _close_turn(person: Person, start: float, end: float) -> None:
    if end - start > person.longest:
        person.longest, person.longest_at = end - start, start
