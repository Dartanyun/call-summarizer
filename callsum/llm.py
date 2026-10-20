"""Языковая модель для саммари, вопросов по созвонам и итогов недели.

Два варианта: Claude API (облако Anthropic) и локальная модель через Ollama — тогда текст
созвона не покидает компьютер. Остальной код работает с ними одинаково.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from . import config, osdeps

BACKENDS = [
    ("claude", "Claude API (облако Anthropic)"),
    ("ollama", "Локальная модель (Ollama) — текст не уходит в интернет"),
]
OLLAMA_URL = "http://localhost:11434"

# если модель Claude откажется отвечать по соображениям безопасности,
# API сам повторит запрос на подходящей запасной модели
_FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


class LLMError(RuntimeError):
    pass


@dataclass
class Block:
    """Часть сообщения. cache — длинный общий кусок (транскрипт): Claude кэширует его между запросами."""
    text: str
    cache: bool = False


Message = dict  # {"role": "user" | "assistant", "content": str | list[Block]}


def backend(cfg: dict) -> str:
    return cfg.get("llm_backend") or "claude"


def ready(cfg: dict) -> bool:
    """Можно ли сейчас делать саммари: есть ключ Claude или выбрана локальная модель."""
    if backend(cfg) == "ollama":
        return bool(cfg.get("ollama_model"))
    return bool(config.api_key(cfg))


def not_ready_text(cfg: dict) -> str:
    """Чего не хватает — со строчной буквы, для середины фразы («Чтобы …, добавьте …»)."""
    if backend(cfg) == "ollama":
        return "выберите локальную модель Ollama в настройках"
    return "добавьте API-ключ Anthropic"


def who(cfg: dict) -> str:
    """Кто пишет текст — для подписей «… пишет саммари»."""
    return "Локальная модель" if backend(cfg) == "ollama" else "Claude"


def model_label(cfg: dict) -> str:
    if backend(cfg) == "ollama":
        return f"Ollama · {cfg.get('ollama_model', '')}"
    label = dict(config.CLAUDE_MODELS).get(cfg["claude_model"], cfg["claude_model"])
    return label.split(" — ")[0]


# ---------------- общий вход ----------------

def complete_json(cfg: dict, system: str, blocks: list[Block], schema: dict, max_tokens: int = 16000) -> dict | None:
    """Ответ строго по JSON-схеме. None — модель отказалась или ответ не разобрался."""
    if backend(cfg) == "ollama":
        text = _ollama_chat(cfg, system, [{"role": "user", "content": blocks}], schema=schema)
    else:
        text = _claude_json(cfg, system, blocks, schema, max_tokens)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def stream(cfg: dict, system: str, messages: list[Message], on_text: Callable[[str], None],
           cancelled: Callable[[], bool] = lambda: False, max_tokens: int = 64000) -> tuple[str, bool]:
    """Пишет ответ по кусочкам (on_text) и возвращает (весь текст, обрезан ли по длине)."""
    if backend(cfg) == "ollama":
        parts: list[str] = []

        def collect(chunk: str):
            if cancelled():
                raise LLMError("Отменено.")
            parts.append(chunk)
            on_text(chunk)

        _ollama_chat(cfg, system, messages, on_text=collect)
        return "".join(parts).strip(), False
    return _claude_stream(cfg, system, messages, on_text, cancelled, max_tokens)


# ---------------- Claude ----------------

def _claude_client(cfg: dict):
    import anthropic

    key = config.api_key(cfg)
    if not key:
        raise LLMError("Не задан API-ключ Anthropic. Укажите его в «Настройках» "
                       "(получить ключ: console.anthropic.com → API Keys).")
    return anthropic.Anthropic(api_key=key)


@contextmanager
def _claude_errors(model: str) -> Iterator[None]:
    import anthropic

    try:
        yield
    except anthropic.AuthenticationError as e:
        raise LLMError("API-ключ не подошёл. Проверьте его в «Настройках».") from e
    except anthropic.PermissionDeniedError as e:
        raise LLMError(f"Нет доступа к модели {model}: {e.message}") from e
    except anthropic.NotFoundError as e:
        raise LLMError(f"Модель {model} не найдена: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise LLMError("Превышен лимит запросов к API. Попробуйте через минуту.") from e
    except anthropic.BadRequestError as e:
        raise LLMError(f"API отклонил запрос: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Ошибка API ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        # APITimeoutError — тоже APIConnectionError
        how = (f"Проверьте прокси в «Настройках» → «Общие» → «Сеть» (сейчас {osdeps.proxy_label(proxy)})." if (proxy := config.load().get("proxy"))
               else "Проверьте интернет. Если Claude API недоступен из вашей сети, укажите прокси "
                    "в «Настройках» → «Общие» → «Сеть».")
        raise LLMError(f"Нет соединения с API Anthropic. {how}") from e


def _claude_content(content) -> list[dict] | str:
    if isinstance(content, str):
        return content
    out = []
    for b in content:
        part = {"type": "text", "text": b.text}
        if b.cache:
            part["cache_control"] = {"type": "ephemeral"}
        out.append(part)
    return out


def _claude_json(cfg: dict, system: str, blocks: list[Block], schema: dict, max_tokens: int) -> str | None:
    client = _claude_client(cfg)
    model = cfg["claude_model"]
    with _claude_errors(model):
        response = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            **_FALLBACK,
            thinking={"type": "adaptive"},
            output_config={"effort": cfg["effort"], "format": {"type": "json_schema", "schema": schema}},
            system=system,
            messages=[{"role": "user", "content": _claude_content(blocks)}],
        )
    if response.stop_reason in ("refusal", "max_tokens"):
        return None
    return next((b.text for b in response.content if b.type == "text"), None)


def _claude_stream(cfg: dict, system: str, messages: list[Message], on_text, cancelled, max_tokens: int):
    client = _claude_client(cfg)
    model = cfg["claude_model"]
    parts: list[str] = []
    with _claude_errors(model):
        with client.beta.messages.stream(
            model=model,
            max_tokens=max_tokens,
            **_FALLBACK,
            thinking={"type": "adaptive"},
            output_config={"effort": cfg["effort"]},
            system=system,
            messages=[{"role": m["role"], "content": _claude_content(m["content"])} for m in messages],
        ) as s:
            for text in s.text_stream:
                if cancelled():
                    raise LLMError("Отменено.")
                parts.append(text)
                on_text(text)
            final = s.get_final_message()
    if final.stop_reason == "refusal":
        raise LLMError("Модель отказалась обрабатывать этот текст.")
    return "".join(parts).strip(), final.stop_reason == "max_tokens"


# ---------------- Ollama ----------------

def ollama_url(cfg: dict) -> str:
    return (cfg.get("ollama_url") or OLLAMA_URL).rstrip("/")


def ollama_models(cfg: dict) -> list[str]:
    """Модели, уже скачанные в Ollama (для списка в настройках)."""
    try:
        with urllib.request.urlopen(f"{ollama_url(cfg)}/api/tags", timeout=3) as r:
            data = json.load(r)
    except (OSError, ValueError):
        return []
    return sorted(m.get("name", "") for m in data.get("models", []) if m.get("name"))


def _flatten(content) -> str:
    return content if isinstance(content, str) else "\n\n".join(b.text for b in content)


def _ollama_chat(cfg: dict, system: str, messages: list[Message], schema: dict | None = None,
                 on_text: Callable[[str], None] | None = None) -> str:
    model = cfg.get("ollama_model") or ""
    if not model:
        raise LLMError("Не выбрана локальная модель. Укажите её в «Настройках» → «Саммари».")
    chat = [{"role": "system", "content": system}] + [
        {"role": m["role"], "content": _flatten(m["content"])} for m in messages]
    chars = sum(len(m["content"]) for m in chat)
    # По умолчанию Ollama помнит всего несколько тысяч токенов и молча отрезает начало
    # транскрипта — задаём окно по длине текста (≈3 символа на токен) с запасом на ответ.
    num_ctx = min(131072, max(8192, (chars // 3 + 6000 + 4095) // 4096 * 4096))
    body = {"model": model, "messages": chat, "stream": on_text is not None,
            "options": {"num_ctx": num_ctx, "temperature": 0.3}}
    if schema is not None:
        body["format"] = schema
    req = urllib.request.Request(f"{ollama_url(cfg)}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            if on_text is None:
                return json.load(r).get("message", {}).get("content", "")
            for line in r:
                if not line.strip():
                    continue
                part = json.loads(line)
                if part.get("error"):
                    raise LLMError(f"Ollama: {part['error']}")
                chunk = part.get("message", {}).get("content", "")
                if chunk:
                    on_text(chunk)
                if part.get("done"):
                    break
            return ""
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read() or b"{}").get("error", "")
        except ValueError:
            pass
        if e.code == 404:
            raise LLMError(f"В Ollama нет модели «{model}». Скачайте её: ollama pull {model}") from e
        raise LLMError(f"Ollama ответил ошибкой {e.code}: {detail or e.reason}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"Не удалось подключиться к Ollama ({ollama_url(cfg)}). "
                       "Запущен ли он? Установка: ollama.com/download") from e
    except (OSError, ValueError) as e:  # оборвалось соединение, истекло время, мусор в ответе
        raise LLMError(f"Ollama не ответил: {e}") from e
