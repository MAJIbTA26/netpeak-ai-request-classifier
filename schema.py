"""
Схема даних для структурованого виводу LLM.

Розширення схеми: додано поле `processing_status`, якого немає в мінімальних
вимогах завдання. Причина: завдання явно вимагає "коректно обробляти
випадки, коли модель повернула невалідні дані". Замість того, щоб просто
падати з помилкою або мовчки пропускати такі запити, ми позначаємо їх
статусом "failed" і залишаємо видимими в output.json та report.md для
ручного review. Це робить обробку невалідних відповідей прозорою й
трасованою, а не прихованою.
"""

from typing import List, Literal, Optional
from pydantic import BaseModel, Field

Category = Literal[
    "автоматизація",
    "інтеграція",
    "звіт/аналітика",
    "баг/підтримка",
    "питання/консультація",
    "поза скоупом",
]

Priority = Literal["low", "medium", "high"]

ProcessingStatus = Literal["ok", "failed"]


class RequestAnalysis(BaseModel):
    id: str
    category: Category
    target_department:  str = ""
    priority: Priority
    short_summary: str
    requested_actions: List[str] = Field(default_factory=list)
    needs_clarification: bool
    processing_status: ProcessingStatus = "ok"
