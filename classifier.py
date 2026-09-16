"""
Класифікація одного запиту через Gemini API зі структурованим виводом.

Обробка невалідного виводу LLM:
    1. Пробуємо отримати та провалідувати відповідь до MAX_RETRIES+1 разів.
    2. Розрізняємо ТИПИ помилок (rate_limit, тимчасова недоступність,
       модель не знайдена, авторизація, некоректний запит) і реагуємо
       по-різному на кожен - замість того щоб ловити все підряд як
       одне загальне Exception.
    3. Якщо всі спроби невдалі - повертаємо "запис-заглушку" з
       processing_status="failed", а не падаємо з винятком, що зупинило б
       обробку всього файлу.

Логування: замість print() використовується стандартний модуль logging,
щоб повідомлення мали рівень важливості (INFO/WARNING/ERROR), часову
мітку, і могли бути перенаправлені у файл чи систему моніторингу в
продакшн-середовищі, а не губились у виводі консолі.
"""

import json
import logging
import os
import re
import time

from google import genai
from pydantic import ValidationError

from schema import RequestAnalysis

logger = logging.getLogger(__name__)

MODEL = "gemini-flash-lite-latest"  # висока денна квота (~1000/день),
                                       # ідеально для класифікаційних задач
FALLBACK_MODEL = "gemini-flash-latest"  # резерв, якщо основна недоступна
MAX_RETRIES = 3
BASE_RETRY_DELAY_SECONDS = 3  # з exponential backoff: 3s, 6s, 12s, 24s

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Повертає єдиний (singleton) екземпляр Gemini API клієнта,
    створюючи його при першому виклику.

    Returns:
        Ініціалізований клієнт google.genai.Client.

    Raises:
        RuntimeError: якщо змінна середовища GEMINI_API_KEY не задана.
    """
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY не знайдено. Створи .env файл на основі "
                ".env.example і встав свій ключ з Google AI Studio."
            )
        _client = genai.Client(api_key=api_key)
    return _client


def _build_prompt(raw_text: str, channel: str) -> str:
    """Формує текст промта для LLM на основі тексту запиту та каналу.

    Args:
        raw_text: Оригінальний текст запиту від користувача.
        channel: Канал, з якого надійшов запит (Slack/Telegram/Email).

    Returns:
        Повний текст промта з описом усіх полів, які має заповнити LLM.
    """
    return f"""Ти аналізуєш вхідний запит від внутрішньої команди компанії
(маркетинг, продажі, аналітика, PM, HR тощо), написаний у вільній формі
через {channel}.

Текст запиту:
\"\"\"{raw_text}\"\"\"

Визнач такі поля:

- category: одна з ["автоматизація", "інтеграція", "звіт/аналітика",
  "баг/підтримка", "питання/консультація", "поза скоупом"].
  "поза скоупом" - якщо запит взагалі не стосується AI/автоматизації
  (наприклад, прохання купити обладнання, подяка без запиту, оффтоп).

- target_department: відділ-замовник, якщо можна визначити з тексту
  або каналу (наприклад "маркетинг", "продажі", "HR", "аналітика").
  Якщо незрозуміло - постав порожній рядок "".

- priority: "low", "medium" або "high", виходячи з тону й змісту тексту
  (слова типу "терміново", "горить", дедлайни - ознака high).

- short_summary: суть запиту одним реченням українською.

- requested_actions: список конкретних дій, які просять зробити.
  Може бути порожній список, якщо запит не містить конкретних дій
  (наприклад, це просто питання чи подяка).

- needs_clarification: true, якщо запит надто розмитий чи короткий,
  щоб брати його в роботу як є (наприклад, "треба бот" без деталей).
"""


def _fallback_record(request_id: str, raw_text: str) -> RequestAnalysis:
    """Створює запис-заглушку для випадку, коли всі спроби отримати
    валідну відповідь від LLM провалились.

    Args:
        request_id: Ідентифікатор запиту.
        raw_text: Оригінальний текст запиту (для короткого опису помилки).

    Returns:
        RequestAnalysis із processing_status="failed" та
        needs_clarification=True, щоб запит залишався видимим для
        ручного review, а не губився мовчки.
    """
    return RequestAnalysis(
        id=request_id,
        category="поза скоупом",
        target_department="",
        priority="low",
        short_summary=f"[НЕ ВДАЛОСЬ ОБРОБИТИ] {raw_text[:120]}",
        requested_actions=[],
        needs_clarification=True,
        processing_status="failed",
    )


def parse_retry_delay(error_message: str, default: float = 5.0) -> float:
    """Витягує з тексту помилки Google рекомендований час очікування.

    Args:
        error_message: Текст помилки від API (наприклад, містить
            "Please retry in 13.05s").
        default: Значення, яке повертається, якщо у тексті немає
            інформації про час очікування.

    Returns:
        Рекомендований час очікування в секундах (+1 сек про запас),
        або default, якщо не вдалось знайти підказку в тексті.
    """
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", error_message)
    if match:
        return float(match.group(1)) + 1
    return default


def classify_error(exception: BaseException) -> str:
    """Визначає категорію помилки за текстом винятку.

    Дозволяє реагувати по-різному на різні типи проблем: чекати
    рекомендований час при rate limit, одразу переходити до fallback-
    моделі при 404, і не витрачати час на retry для помилок авторизації
    (які retry все одно не виправить).

    Args:
        exception: Об'єкт винятку, отриманий під час виклику API.

    Returns:
        Один з рядків: "rate_limit", "temporary_unavailable",
        "model_not_found", "auth_error", "bad_request", "unknown".
    """
    message = str(exception)

    if "429" in message or "RESOURCE_EXHAUSTED" in message:
        return "rate_limit"
    if "503" in message or "UNAVAILABLE" in message:
        return "temporary_unavailable"
    if "404" in message or "NOT_FOUND" in message:
        return "model_not_found"
    if "401" in message or "403" in message or "API key" in message:
        return "auth_error"
    if "400" in message or "INVALID_ARGUMENT" in message:
        return "bad_request"
    return "unknown"


def classify_request(request_id: str, raw_text: str, channel: str) -> RequestAnalysis:
    """Класифікує один вхідний запит через Gemini API.

    Виконує до MAX_RETRIES+1 спроб з типізованою обробкою помилок:
    rate limit чекає рекомендований Google час, тимчасова недоступність
    використовує exponential backoff, застаріла модель одразу переходить
    на fallback без затримки, помилка авторизації одразу зупиняє (retry
    безглуздий), некоректний запит одразу йде у fallback-запис.

    Args:
        request_id: Ідентифікатор запиту з вхідного CSV.
        raw_text: Текст запиту, який потрібно класифікувати.
        channel: Канал, з якого надійшов запит.

    Returns:
        RequestAnalysis з результатом класифікації. У разі, якщо всі
        спроби провалились, повертає запис-заглушку з
        processing_status="failed" (див. _fallback_record).
    """
    client = _get_client()
    prompt = _build_prompt(raw_text, channel)

    last_error: BaseException | None = None

    for attempt in range(MAX_RETRIES + 1):
        model = MODEL if attempt < 2 else FALLBACK_MODEL

        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": RequestAnalysis,
                    "temperature": 0,
                },
            )
            data = json.loads(response.text)
            data["id"] = request_id
            data.setdefault("processing_status", "ok")
            result = RequestAnalysis.model_validate(data)
            logger.info(f"[{request_id}] успішно класифіковано (модель: {model})")
            return result

        except (json.JSONDecodeError, ValidationError) as e:
            last_error = e
            delay = BASE_RETRY_DELAY_SECONDS * (2 ** attempt)
            logger.warning(f"[{request_id}] спроба {attempt + 1} невдала (валідація): {e}")
            time.sleep(delay)
            continue

        except Exception as e:
            error_type = classify_error(e)
            last_error = e

            if error_type == "rate_limit":
                delay = parse_retry_delay(str(e))
                logger.warning(
                    f"[{request_id}] rate limit на {model}, "
                    f"чекаю {delay:.0f} сек (за рекомендацією Google)"
                )
                time.sleep(delay)
                continue

            if error_type == "model_not_found":
                logger.warning(f"[{request_id}] модель {model} недоступна, переходжу без затримки")
                continue

            if error_type == "auth_error":
                logger.error(f"[{request_id}] помилка авторизації API: {e}")
                raise RuntimeError(
                    f"Помилка авторизації API: {e}\nПеревір GEMINI_API_KEY у .env"
                ) from e

            if error_type == "bad_request":
                logger.warning(f"[{request_id}] некоректний запит до API: {e}")
                break

            delay = BASE_RETRY_DELAY_SECONDS * (2 ** attempt)
            logger.warning(f"[{request_id}] {error_type} на {model} (спроба {attempt + 1}): {e}")
            time.sleep(delay)
            continue

    logger.error(f"[{request_id}] всі спроби невдалі, останнiй error: {last_error}")
    return _fallback_record(request_id, raw_text)
