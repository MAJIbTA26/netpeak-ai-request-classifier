"""
Тест для перевірки того, що при критичній помилці (RuntimeError через
невалідний API-ключ) посеред обробки списку запитів - уже оброблені
результати НЕ губляться, а зберігаються перед тим, як помилка
прокидається далі.

Це прямо перевіряє виправлення реальної прогалини, знайденої під час
код-рев'ю: "при виклику classify_request не обробляються винятки, що
може призвести до аварійного завершення" (і втрати вже зробленої
роботи).
"""

import os
from unittest.mock import patch

import pytest

from main import process_all
from schema import RequestAnalysis


def _make_ok_result(request_id: str) -> RequestAnalysis:
    return RequestAnalysis(
        id=request_id,
        category="автоматизація",
        target_department="",
        priority="low",
        short_summary=f"Результат для {request_id}",
        requested_actions=[],
        needs_clarification=False,
    )


@patch("main.classify_request")
@patch("main.time.sleep")
def test_process_all_saves_partial_results_on_runtime_error(mock_sleep, mock_classify, tmp_path):
    """Перевіряємо: якщо третій з чотирьох запитів провалюється з
    RuntimeError (напр. невалідний ключ), два вже оброблені результати
    зберігаються у файл, а сама помилка прокидається далі."""

    # Перші два виклики успішні, третій - критична помилка
    mock_classify.side_effect = [
        _make_ok_result("REQ-001"),
        _make_ok_result("REQ-002"),
        RuntimeError("Помилка авторизації API: невалідний ключ"),
    ]

    requests = [
        {"id": "REQ-001", "raw_text": "текст 1", "channel": "Slack", "timestamp": "2026-01-01"},
        {"id": "REQ-002", "raw_text": "текст 2", "channel": "Slack", "timestamp": "2026-01-01"},
        {"id": "REQ-003", "raw_text": "текст 3", "channel": "Slack", "timestamp": "2026-01-01"},
        {"id": "REQ-004", "raw_text": "текст 4", "channel": "Slack", "timestamp": "2026-01-01"},
    ]

    # Працюємо у тимчасовій директорії, щоб не засмічувати реальний output.json
    original_dir = os.getcwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            process_all(requests)

        # Перевіряємо, що часткові результати РЕАЛЬНО збереглись у файл
        assert (tmp_path / "output.json").exists()

        import json
        with open(tmp_path / "output.json", encoding="utf-8") as f:
            saved = json.load(f)

        assert len(saved) == 2  # тільки REQ-001 і REQ-002 встигли обробитись
        assert saved[0]["id"] == "REQ-001"
        assert saved[1]["id"] == "REQ-002"

    finally:
        os.chdir(original_dir)
