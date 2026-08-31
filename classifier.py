"""
Класифікація одного запиту через Gemini API зі структурованим виводом.

Обробка невалідного виводу LLM:
    1. Пробуємо отримати та провалідувати відповідь до MAX_RETRIES+1 разів.
    2. Якщо LLM повертає невалідний JSON або дані, що не проходять
       Pydantic-валідацію (наприклад, значення category поза списком
       дозволених) - пробуємо ще раз.
    3. Якщо всі спроби невдалі - повертаємо "запис-заглушку" з
       processing_status="failed" і needs_clarification=True, замість
       того щоб впасти з винятком і зупинити обробку всього файлу.
       Це гарантує, що один проблемний запит не ламає весь пайплайн.
"""

import json
import time
import os
import re

from google import genai
from pydantic import ValidationError

from schema import RequestAnalysis

MODEL = "gemini-flash-lite-latest"    # alias - Google сам оновлює на актуальну версію
FALLBACK_MODEL = "gemini-flash-latest"  # теж alias, трохи потужніша модель як резерв


MAX_RETRIES = 3
BASE_RETRY_DELAY_SECONDS = 3  # з exponential backoff: 3s, 6s, 12s...

_client = None


def _get_client():
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
    """Запис-заглушка для випадку, коли всі спроби отримати валідну
    відповідь від LLM провалились."""
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
    match = re.search(r"retry in (\d+(?:\.\d+)?)s", error_message)
    if match:
        return float(match.group(1)) + 1
    return default


def classify_error(exception) -> str:
    message = str(exception)
    if "429" in message or "RESOURCE_EXHAUSTED" in message:
        return "rate_limit"
    if "503" in message or "UNAVAILABLE" in message:
        return "temporary_unavailable"
    if "404" in message or "NOT_FOUND" in message:
        return "model_not_found"  # <-- новий рядок
    if "401" in message or "403" in message or "API key" in message:
        return "auth_error"
    if "400" in message or "INVALID_ARGUMENT" in message:
        return "bad_request"
    return "unknown"




def classify_request(request_id: str, raw_text: str, channel: str) -> RequestAnalysis:
    client = _get_client()
    prompt = _build_prompt(raw_text, channel)

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        # Після половини невдалих спроб - пробуємо резервну модель,
        # раптом саме основна зараз перевантажена (503)
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
            data["id"] = request_id  # гарантуємо, що id завжди правильний
            data.setdefault("processing_status", "ok")
            return RequestAnalysis.model_validate(data)

        except (json.JSONDecodeError, ValidationError) as e:
            last_error = e
            delay = BASE_RETRY_DELAY_SECONDS * (2 ** attempt)
            print(f"  [{request_id}] спроба {attempt + 1} невдала (валідація): {e}")
            time.sleep(delay)
            continue

        except Exception as e:
            error_type = classify_error(e)
            last_error = e

            if error_type == "rate_limit":
                delay = parse_retry_delay(str(e))
                print(f"  [{request_id}] rate limit на {model}, чекаю {delay:.0f} сек (за рекомендацією Google)...")
                time.sleep(delay)
                continue
            
            if error_type == "model_not_found":
                print(f"  [{request_id}] модель {model} недоступна, переходжу без затримки...")
                continue  # без time.sleep - одразу наступна спроба (вже з fallback моделлю)

            if error_type == "auth_error":
                raise RuntimeError(
                    f"Помилка авторизації API: {e}\nПеревір GEMINI_API_KEY у .env"
                ) from e

            if error_type == "bad_request":
                print(f"  [{request_id}] некоректний запит до API: {e}")
                break

            delay = BASE_RETRY_DELAY_SECONDS * (2 ** attempt)
            print(f"  [{request_id}] {error_type} на {model} (спроба {attempt + 1}): {e}")
            print(f"  [{request_id}] чекаю {delay} сек...")
            time.sleep(delay)
            continue

    print(f"  [{request_id}] всі спроби невдалі, останнiй error: {last_error}")
    return _fallback_record(request_id, raw_text)