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

from google import genai
from pydantic import ValidationError

from schema import RequestAnalysis

MODEL = "gemini-3.6-flash"
FALLBACK_MODEL = "gemini-3.6-flash"  # резерв на випадок перевантаження основної
                                       # (503 UNAVAILABLE) - конкретна стабільна
                                       # версія, а не alias, для передбачуваності
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
            last_error = e
            delay = BASE_RETRY_DELAY_SECONDS * (2 ** attempt)
            print(f"  [{request_id}] помилка API на моделі {model} (спроба {attempt + 1}): {e}")
            print(f"  [{request_id}] чекаю {delay} сек перед наступною спробою...")
            time.sleep(delay)
            continue

    print(f"  [{request_id}] всі спроби невдалі, останнiй error: {last_error}")
    return _fallback_record(request_id, raw_text)