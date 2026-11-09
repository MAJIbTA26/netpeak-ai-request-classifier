"""
HTTP-обгортка (FastAPI) навколо класифікатора запитів.

Запуск локально:
    uvicorn api:app --reload

Документація (Swagger) після запуску: http://127.0.0.1:8000/docs

Ендпоінти:
    GET  /health    - перевірка, що сервіс живий
    POST /classify  - класифікація одного запиту через Gemini
"""

import logging
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, StringConstraints

from classifier import classify_request
from schema import RequestAnalysis

load_dotenv()  # підхоплює GEMINI_API_KEY з .env при локальному запуску

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Request Classifier API",
    description="Класифікація вхідних запитів команди через Gemini (структурований JSON).",
    version="0.1.0",
)

# Текст без пробілів по краях, не порожній, з розумним верхнім лімітом
# (захист від надто великих запитів, які коштують грошей і часу).
RequestText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]


class ClassifyRequest(BaseModel):
    """Тіло запиту до POST /classify."""

    id: str = Field(min_length=1, max_length=100, description="Ідентифікатор запиту, напр. REQ-001")
    text: RequestText = Field(description="Текст запиту у вільній формі")
    channel: str = Field(default="API", max_length=50, description="Канал: Slack, Telegram, Email тощо")


@app.get("/health")
def health() -> dict[str, str]:
    """Перевірка стану сервісу (для Docker healthcheck і моніторингу)."""
    return {"status": "ok"}


# Звичайна функція (def), а не async def: classify_request блокуюча
# (мережевий виклик і time.sleep при повторах). FastAPI запускає такі
# функції в пулі потоків, тож один повільний запит не блокує інші.
@app.post("/classify", response_model=RequestAnalysis)
def classify(payload: ClassifyRequest) -> RequestAnalysis:
    """Класифікує один запит.

    Якщо LLM не змогла дати валідну відповідь після повторних спроб,
    повертається 200 із processing_status="failed" (так само, як у CSV-конвеєрі).
    Помилки конфігурації (немає ключа, неправильний ключ) дають 503.
    """
    try:
        return classify_request(payload.id, payload.text, payload.channel)
    except RuntimeError as e:
        # Деталі (можуть містити підказки про ключ) лишаємо в логах, клієнту не віддаємо.
        logger.error(f"Класифікація неможлива: {e}")
        raise HTTPException(status_code=503, detail="Сервіс класифікації тимчасово недоступний.") from e
