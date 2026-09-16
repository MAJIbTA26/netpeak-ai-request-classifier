"""
Схема даних для структурованого виводу LLM.

Розширення схеми: додано поле `processing_status`, якого немає в мінімальних
вимогах завдання. Причина: завдання явно вимагає "коректно обробляти
випадки, коли модель повернула невалідні дані". Замість того, щоб просто
падати з помилкою або мовчки пропускати такі запити, ми позначаємо їх
статусом "failed" і залишаємо видимими в output.json та report.md для
ручного review. Це робить обробку невалідних відповідей прозорою й
трасованою, а не прихованою.

Технічна деталь: поле `target_department` - це `str = ""` (порожній
рядок), а не `Optional[str] = None`. Причина: Pydantic Optional-поля
конвертуються у формат, несумісний з внутрішнім Schema-типом Gemini
structured output (помилка `literal_error` для типу 'NULL'). Порожній
рядок як sentinel-значення "не визначено" - обхідний шлях цієї
несумісності.
"""

from typing import Literal

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
    """Структурований результат класифікації одного вхідного запиту.

    Attributes:
        id: Ідентифікатор запиту, збігається з полем 'id' у вхідному CSV.
        category: Одна з 6 фіксованих категорій запиту.
        target_department: Відділ-замовник, або порожній рядок "",
            якщо LLM не змогла визначити відділ з тексту.
        priority: Пріоритет запиту (low/medium/high), визначений LLM
            на основі тону й змісту тексту.
        short_summary: Стислий переказ суті запиту одним реченням.
        requested_actions: Список конкретних дій, які просить виконати
            автор запиту (може бути порожнім списком).
        needs_clarification: True, якщо запит надто розмитий, щоб
            братись до роботи без додаткових уточнень.
        processing_status: "ok" для нормально оброблених запитів,
            "failed" - якщо LLM не змогла дати валідну відповідь навіть
            після повторних спроб (див. classifier.py).
    """

    id: str
    category: Category
    target_department: str = Field(
        default="",
        description="Відділ-замовник, або порожній рядок, якщо незрозуміло",
    )
    priority: Priority
    short_summary: str
    requested_actions: list[str] = Field(default_factory=list)
    needs_clarification: bool
    processing_status: ProcessingStatus = "ok"
