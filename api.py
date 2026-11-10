"""
HTTP-обгортка (FastAPI) навколо класифікатора запитів.

Запуск локально:
    uvicorn api:app --reload

Документація (Swagger) після запуску: http://127.0.0.1:8000/docs

Ендпоінти:
    GET   /health                       - перевірка, що сервіс живий (без ключа)
    POST  /classify                     - класифікація запиту через Gemini + збереження в базу
    GET   /results/{id}                 - один збережений запис (AI-пріоритет і фактичний)
    PATCH /results/{id}/priority        - керівник змінює пріоритет (AI-значення лишається для аудиту)
    GET   /stats/overrides              - як часто людина не погоджується з AI

Усі ендпоінти, крім /health, вимагають заголовок X-API-Key зі значенням змінної API_KEY.
Якщо API_KEY на сервері не задано, захищені ендпоінти відповідають 503 (закрито за замовчуванням).
"""

import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Path
from pydantic import BaseModel, Field, StringConstraints

from classifier import classify_request
from db import (
    DatabaseUnavailableError,
    get_override_stats,
    get_result,
    save_api_result,
    set_priority_override,
)
from schema import Priority, RequestAnalysis

load_dotenv()  # підхоплює GEMINI_API_KEY, API_KEY, DATABASE_URL з .env при локальному запуску

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Request Classifier API",
    description="Класифікація вхідних запитів команди через Gemini (структурований JSON).",
    version="0.2.0",
)

# Текст без пробілів по краях, не порожній, з розумним верхнім лімітом
# (захист від надто великих запитів, які коштують грошей і часу).
RequestText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
ChangedBy = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
# BIGINT у PostgreSQL: більші числа викликали б помилку бази, тож відсікаємо їх на вході.
RecordId = Annotated[int, Path(ge=1, le=9_223_372_036_854_775_807, description="id запису (record_id)")]


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """Перевіряє заголовок X-API-Key. Порівняння сталого часу (захист від підбору по часу відповіді)."""
    expected = os.getenv("API_KEY")
    if not expected:
        logger.error("API_KEY не задано: захищені ендпоінти вимкнені.")
        raise HTTPException(status_code=503, detail="Сервіс не налаштовано.")
    if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Невірний або відсутній ключ API.")


protected = [Depends(require_api_key)]


class ClassifyRequest(BaseModel):
    """Тіло запиту до POST /classify."""

    id: str = Field(min_length=1, max_length=100, description="Ідентифікатор запиту, напр. REQ-001")
    text: RequestText = Field(description="Текст запиту у вільній формі")
    channel: str = Field(default="API", max_length=50, description="Канал: Slack, Telegram, Email тощо")


class ClassifyResponse(RequestAnalysis):
    """Результат класифікації + id запису в базі."""

    record_id: int | None = Field(
        default=None,
        description="id запису в базі (потрібен для зміни пріоритету). null, якщо базу не підключено.",
    )


class PriorityUpdate(BaseModel):
    """Тіло запиту до PATCH /results/{id}/priority."""

    priority: Priority
    changed_by: ChangedBy = Field(description="Хто змінює (ім'я або Telegram-логін керівника)")


class ResultRecord(BaseModel):
    """Збережений запис: пріоритет від AI, рішення людини і фактичний пріоритет."""

    id: int
    run_id: int
    request_id: str
    channel: str | None
    category: str
    target_department: str
    priority: Priority = Field(description="Пріоритет, який визначив AI (не змінюється)")
    priority_override: Priority | None = Field(description="Пріоритет, який виставила людина, або null")
    effective_priority: Priority = Field(description="Фактичний: рішення людини, а якщо його немає, то AI")
    overridden_by: str | None
    overridden_at: datetime | None
    short_summary: str
    requested_actions: list[str]
    needs_clarification: bool
    processing_status: str


class OverrideStats(BaseModel):
    total: int = Field(description="Усього збережених заявок")
    reviewed: int = Field(description="Скільки заявок переглянула людина")
    changed: int = Field(description="Скільки разів людина змінила пріоритет AI")
    disagreement_rate: float | None = Field(description="changed / reviewed, або null, якщо нічого не переглянуто")


def _database_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="База даних недоступна.")


@app.get("/health")
def health() -> dict[str, str]:
    """Перевірка стану сервісу (для Docker healthcheck і моніторингу). Ключ не потрібен."""
    return {"status": "ok"}


# Звичайні функції (def), а не async def: classify_request і робота з базою блокуючі
# (мережа, time.sleep при повторах). FastAPI запускає такі функції в пулі потоків,
# тож один повільний запит не блокує інші.
@app.post("/classify", response_model=ClassifyResponse, dependencies=protected)
def classify(payload: ClassifyRequest) -> ClassifyResponse:
    """Класифікує один запит і зберігає його в базу.

    Якщо LLM не змогла дати валідну відповідь після повторних спроб,
    повертається 200 із processing_status="failed" (так само, як у CSV-конвеєрі).
    Помилки конфігурації (немає ключа Gemini) дають 503.
    Якщо базу не підключено чи вона недоступна, класифікація все одно працює, а record_id = null.
    """
    try:
        analysis = classify_request(payload.id, payload.text, payload.channel)
    except RuntimeError as e:
        # Деталі (можуть містити підказки про ключ) лишаємо в логах, клієнту не віддаємо.
        logger.error(f"Класифікація неможлива: {e}")
        raise HTTPException(status_code=503, detail="Сервіс класифікації тимчасово недоступний.") from e

    record = {
        **analysis.model_dump(),
        "channel": payload.channel,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    record_id = save_api_result(record)
    return ClassifyResponse(**analysis.model_dump(), record_id=record_id)


@app.get("/results/{record_id}", response_model=ResultRecord, dependencies=protected)
def read_result(record_id: RecordId) -> dict:
    """Повертає збережений запис: пріоритет від AI, рішення людини і фактичний пріоритет."""
    try:
        record = get_result(record_id)
    except DatabaseUnavailableError as e:
        raise _database_unavailable() from e
    if record is None:
        raise HTTPException(status_code=404, detail="Запис не знайдено.")
    return record


@app.patch("/results/{record_id}/priority", response_model=ResultRecord, dependencies=protected)
def change_priority(record_id: RecordId, update: PriorityUpdate) -> dict:
    """Керівник виставляє пріоритет. AI-значення не перезаписується (аудит)."""
    try:
        record = set_priority_override(record_id, update.priority, update.changed_by)
    except DatabaseUnavailableError as e:
        raise _database_unavailable() from e
    if record is None:
        raise HTTPException(status_code=404, detail="Запис не знайдено.")
    logger.info(f"Пріоритет запису {record_id} змінено на {update.priority} ({update.changed_by})")
    return record


@app.get("/stats/overrides", response_model=OverrideStats, dependencies=protected)
def override_stats() -> dict:
    """Як часто керівник не погоджується з AI (корисно для покращення промпта)."""
    try:
        return get_override_stats()
    except DatabaseUnavailableError as e:
        raise _database_unavailable() from e
