"""Саммари транскрипта и имена участников через Claude API."""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import anthropic

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

# если модель откажется отвечать по соображениям безопасности,
# API сам повторит запрос на подходящей запасной модели
_FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


class SummaryError(RuntimeError):
    pass


def _client(api_key: str) -> anthropic.Anthropic:
    if not api_key:
        raise SummaryError(
            "Не задан API-ключ Anthropic. Укажите его в «Настройках» "
            "(получить ключ: console.anthropic.com → API Keys)."
        )
    return anthropic.Anthropic(api_key=api_key)


@contextmanager
def _api_errors(model: str) -> Iterator[None]:
    try:
        yield
    except anthropic.AuthenticationError as e:
        raise SummaryError("API-ключ не подошёл. Проверьте его в «Настройках».") from e
    except anthropic.PermissionDeniedError as e:
        raise SummaryError(f"Нет доступа к модели {model}: {e.message}") from e
    except anthropic.NotFoundError as e:
        raise SummaryError(f"Модель {model} не найдена: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise SummaryError("Превышен лимит запросов к API. Попробуйте через минуту.") from e
    except anthropic.BadRequestError as e:
        raise SummaryError(f"API отклонил запрос: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise SummaryError(f"Ошибка API ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise SummaryError("Нет соединения с API Anthropic. Проверьте интернет.") from e


def vocabulary_note(terms: list[str]) -> str:
    if not terms:
        return ""
    return ("Слова и имена, которые часто звучат на этих созвонах (исправляй на них похожие по звучанию "
            "ошибки распознавания): " + ", ".join(terms) + ".")


def _content(transcript: str, task: str) -> list[dict]:
    # Транскрипт — отдельным блоком с кэшированием: запрос имён и саммари идут подряд
    # с одинаковым началом, и второй раз длинный транскрипт оплачивается по сниженной цене.
    return [
        {"type": "text", "text": f"<transcript>\n{transcript}\n</transcript>",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": task},
    ]


def identify_speakers(
    transcript: str, labels: list[str], api_key: str, model: str, effort: str, vocabulary: list[str] = ()
) -> dict[str, tuple[str, bool]]:
    """Имена участников по контексту: {метка: (имя, уверенно ли)}."""
    client = _client(api_key)
    with _api_errors(model):
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            **_FALLBACK,
            thinking={"type": "adaptive"},
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": NAMES_SCHEMA}},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _content(
                transcript,
                "\n\n".join(filter(None, [
                    vocabulary_note(list(vocabulary)),
                    NAMES_INSTRUCTIONS.format(labels=", ".join(f"«{x}»" for x in labels)),
                ])),
            )}],
        )
    if response.stop_reason in ("refusal", "max_tokens"):
        return {}
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    result = {}
    for item in data.get("speakers", []):
        label, name = item.get("label", "").strip("«» "), item.get("name", "").strip()
        if label in labels and name:
            result[label] = (name, bool(item.get("confident")))
    return result


def summarize(
    transcript: str,
    api_key: str,
    model: str,
    effort: str,
    instructions: str,
    on_text: Callable[[str], None],
    cancelled: Callable[[], bool] = lambda: False,
    speaker_note: str = "",
    vocabulary: list[str] = (),
) -> str:
    client = _client(api_key)
    task = "\n\n".join(filter(None, [vocabulary_note(list(vocabulary)), speaker_note, instructions, TIMECODES]))
    parts: list[str] = []
    with _api_errors(model):
        with client.beta.messages.stream(
            model=model,
            max_tokens=64000,
            **_FALLBACK,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _content(transcript, task)}],
        ) as stream:
            for text in stream.text_stream:
                if cancelled():
                    raise SummaryError("Отменено.")
                parts.append(text)
                on_text(text)
            final = stream.get_final_message()

    if final.stop_reason == "refusal":
        raise SummaryError("Модель отказалась обрабатывать этот транскрипт.")
    text = "".join(parts).strip()
    if final.stop_reason == "max_tokens":
        text += "\n\n*(Саммари обрезано: достигнут лимит длины ответа.)*"
    return text
