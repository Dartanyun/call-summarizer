"""Саммари транскрипта и имена участников — через Claude API или локальную модель (llm.py)."""

from collections.abc import Callable

from . import llm

SYSTEM_PROMPT = """\
Ты помогаешь человеку разбирать записи его рабочих созвонов: делаешь точные, \
структурированные и полезные саммари.

Транскрипт получен автоматическим распознаванием речи, поэтому в нём бывают ошибки \
в словах, именах, названиях и терминах. Исправляй их по смыслу, когда уверен, \
а сомнительные места не выдумывай.

У реплик есть метки говорящих, определённые автоматически по голосу: «Я» — пользователь, \
который записывал созвон; «Собеседник» или «Спикер 1», «Спикер 2»… — другие участники. \
Метки иногда ошибаются на стыках реплик. Если меток нет, говорящих определяй по контексту, \
а если не получается — не указывай.\
"""

NAMES_INSTRUCTIONS = """\
Определи по разговору, как зовут участников с метками: {labels}.

Опирайся только на то, что прозвучало: к человеку обратились по имени, и он ответил; \
он представился; о нём сказали в третьем лице так, что понятно, кто это. \
Пиши имя так, как к человеку обращаются (например, «Ира», «Денис Петрович»), в именительном падеже.
- confident: true — если имя следует из разговора однозначно.
- confident: false — если это лишь догадка.
- name: пустая строка — если определить нельзя.
"""

NAMES_SCHEMA = {
    "type": "object",
    "properties": {
        "speakers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "name": {"type": "string"},
                    "confident": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["label", "name", "confident", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["speakers"],
    "additionalProperties": False,
}

TIMECODES = (
    "Где это помогает, ставь в конце пункта таймкод момента из транскрипта в квадратных скобках, "
    "например [00:12:34]: по нему пользователь перейдёт к этому месту записи. Не больше одного таймкода на пункт."
)

SummaryError = llm.LLMError  # прежнее имя: им пользуются окно и очередь обработки


def vocabulary_note(terms: list[str]) -> str:
    if not terms:
        return ""
    return ("Слова и имена, которые часто звучат на этих созвонах (исправляй на них похожие по звучанию "
            "ошибки распознавания): " + ", ".join(terms) + ".")


def _blocks(transcript: str, task: str) -> list[llm.Block]:
    # Транскрипт — отдельным блоком с кэшированием: запрос имён и саммари идут подряд
    # с одинаковым началом, и второй раз длинный транскрипт оплачивается по сниженной цене.
    return [llm.Block(f"<transcript>\n{transcript}\n</transcript>", cache=True), llm.Block(task)]


def identify_speakers(cfg: dict, transcript: str, labels: list[str], vocabulary: list[str] = (),
                      hint: str = "") -> dict[str, tuple[str, bool]]:
    """Имена участников по контексту: {метка: (имя, уверенно ли)}. hint — например, список приглашённых."""
    task = "\n\n".join(filter(None, [
        vocabulary_note(list(vocabulary)), hint,
        NAMES_INSTRUCTIONS.format(labels=", ".join(f"«{x}»" for x in labels)),
    ]))
    data = llm.complete_json(cfg, SYSTEM_PROMPT, _blocks(transcript, task), NAMES_SCHEMA) or {}
    result = {}
    for item in data.get("speakers", []):
        if not isinstance(item, dict):
            continue
        label, name = str(item.get("label", "")).strip("«» "), str(item.get("name", "")).strip()
        if label in labels and name:
            result[label] = (name, bool(item.get("confident")))
    return result


def summarize(cfg: dict, transcript: str, instructions: str, on_text: Callable[[str], None],
              cancelled: Callable[[], bool] = lambda: False, notes: list[str] = (),
              vocabulary: list[str] = ()) -> str:
    """notes — что ещё знаем о встрече: имена, календарь, отмеченные моменты."""
    task = "\n\n".join(filter(None, [vocabulary_note(list(vocabulary)), *notes, instructions, TIMECODES]))
    text, truncated = llm.stream(cfg, SYSTEM_PROMPT, [{"role": "user", "content": _blocks(transcript, task)}],
                                 on_text, cancelled)
    if truncated:
        text += "\n\n*(Саммари обрезано: достигнут лимит длины ответа.)*"
    return text
