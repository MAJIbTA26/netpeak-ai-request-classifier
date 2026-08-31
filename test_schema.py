from schema import RequestAnalysis
import pytest


def test_valid_record_is_accepted():
    """Перевіряємо, що коректні дані успішно проходять валідацію."""
    record = RequestAnalysis(
        id="REQ-001",
        category="автоматизація",
        target_department="маркетинг",
        priority="high",
        short_summary="Тестовий запис",
        requested_actions=["зробити щось"],
        needs_clarification=False,
    )

    assert record.id == "REQ-001"
    assert record.category == "автоматизація"
    assert record.processing_status == "ok"  # значення за замовчуванням
    
    
   


def test_invalid_category_is_rejected():
    """Перевіряємо, що схема відхиляє категорію поза дозволеним списком."""
    with pytest.raises(Exception):
        RequestAnalysis(
            id="REQ-002",
            category="якась_дурниця_поза_списком",
            priority="low",
            short_summary="Тест",
            needs_clarification=True,
        )