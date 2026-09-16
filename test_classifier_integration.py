"""
Інтеграційні тести для classify_request() з mock'ом genai.Client.

Це прямо реалізує пропозицію з технічного рев'ю: "Додайте мок-об'єкти
для genai.Client і перевірте, що processing_status змінюється правильно".

На відміну від test_classifier.py (юніт-тести окремих функцій), тут
перевіряється ПОВНИЙ цикл: виклик API -> парсинг JSON -> Pydantic-
валідація -> повернення RequestAnalysis, включно зі сценарієм повного
провалу всіх спроб (retry -> fallback-запис).
"""

import json
from unittest.mock import MagicMock, patch

from classifier import classify_request


def _make_mock_response(response_dict: dict) -> MagicMock:
    """Створює mock-об'єкт відповіді Gemini API з заданим JSON-текстом."""
    mock_response = MagicMock()
    mock_response.text = json.dumps(response_dict, ensure_ascii=False)
    return mock_response


@patch("classifier._get_client")
def test_classify_request_returns_valid_analysis_on_success(mock_get_client):
    """Перевіряємо повний "щасливий шлях": API одразу повертає валідний
    JSON, і processing_status встановлюється в "ok"."""
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = _make_mock_response({
        "id": "REQ-001",
        "category": "автоматизація",
        "target_department": "маркетинг",
        "priority": "medium",
        "short_summary": "Тестовий запит",
        "requested_actions": ["зробити щось"],
        "needs_clarification": False,
    })
    mock_get_client.return_value = mock_client

    result = classify_request("REQ-001", "Тестовий текст запиту", "Slack")

    assert result.id == "REQ-001"
    assert result.category == "автоматизація"
    assert result.processing_status == "ok"
    # Перевіряємо, що API реально викликався лише ОДИН раз (без зайвих retry)
    assert mock_client.models.generate_content.call_count == 1


@patch("classifier._get_client")
@patch("classifier.time.sleep")  # прибираємо реальні затримки під час тесту
def test_classify_request_falls_back_after_all_retries_fail(mock_sleep, mock_get_client):
    """Перевіряємо, що коли API постійно повертає невалідний JSON,
    після вичерпання спроб повертається fallback-запис з
    processing_status="failed", а НЕ виняток, що зупинив би всю обробку."""
    mock_client = MagicMock()
    # Симулюємо, що API щоразу повертає зламаний JSON
    broken_response = MagicMock()
    broken_response.text = "це не валідний json {{{"
    mock_client.models.generate_content.return_value = broken_response
    mock_get_client.return_value = mock_client

    result = classify_request("REQ-002", "Якийсь текст", "Email")

    assert result.id == "REQ-002"
    assert result.processing_status == "failed"
    assert result.needs_clarification is True
    # Перевіряємо, що дійсно було зроблено декілька спроб (MAX_RETRIES + 1 = 4)
    assert mock_client.models.generate_content.call_count == 4


@patch("classifier._get_client")
def test_classify_request_recovers_after_one_bad_attempt(mock_get_client):
    """Перевіряємо реалістичний сценарій: перша спроба повертає
    невалідні дані (категорія поза списком), друга - валідні. Результат
    має бути успішним, без переходу у fallback."""
    mock_client = MagicMock()

    bad_response = _make_mock_response({
        "id": "REQ-003",
        "category": "щось_чого_немає_у_списку",  # невалідна категорія
        "priority": "low",
        "short_summary": "Тест",
        "needs_clarification": False,
    })
    good_response = _make_mock_response({
        "id": "REQ-003",
        "category": "питання/консультація",
        "target_department": "",
        "priority": "low",
        "short_summary": "Тест після виправлення",
        "requested_actions": [],
        "needs_clarification": False,
    })

    mock_client.models.generate_content.side_effect = [bad_response, good_response]
    mock_get_client.return_value = mock_client

    with patch("classifier.time.sleep"):  # прибираємо реальну затримку
        result = classify_request("REQ-003", "Текст запиту", "Slack")

    assert result.processing_status == "ok"
    assert result.category == "питання/консультація"
    assert mock_client.models.generate_content.call_count == 2
