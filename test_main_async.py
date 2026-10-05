"""
Тести паралельної обробки (process_all_async): порядок результатів,
реальна паралельність, дотримання ліміту Semaphore, збереження часткових
результатів при критичній помилці.
"""

import asyncio
import json
import threading
import time
from unittest.mock import patch

import pytest

import main
from main import get_max_concurrency, process_all_async
from schema import RequestAnalysis


def _ok(request_id: str) -> RequestAnalysis:
    return RequestAnalysis(
        id=request_id,
        category="автоматизація",
        target_department="",
        priority="low",
        short_summary=f"Результат для {request_id}",
        requested_actions=[],
        needs_clarification=False,
    )


def _rows(n: int) -> list[dict[str, str]]:
    return [
        {"id": f"REQ-{i:03d}", "raw_text": f"текст {i}", "channel": "Slack", "timestamp": "2026-01-01"}
        for i in range(1, n + 1)
    ]


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(main, "DELAY_BETWEEN_REQUESTS", 0)


def test_results_keep_input_order():
    """Навіть якщо запити завершуються в зворотному порядку, результат
    має йти в порядку вхідного списку."""

    def fake(request_id, raw_text, channel):
        time.sleep(0.05 if request_id == "REQ-001" else 0.0)  # перший завершується останнім
        return _ok(request_id)

    with patch("main.classify_request", side_effect=fake):
        results = asyncio.run(process_all_async(_rows(4), max_concurrency=4))

    assert [r["id"] for r in results] == ["REQ-001", "REQ-002", "REQ-003", "REQ-004"]
    assert all(r["channel"] == "Slack" and r["timestamp"] == "2026-01-01" for r in results)


def test_semaphore_limits_concurrency():
    """Одночасно виконується більше одного запиту, але не більше ліміту."""
    lock = threading.Lock()
    active = 0
    peak = 0

    def fake(request_id, raw_text, channel):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return _ok(request_id)

    with patch("main.classify_request", side_effect=fake):
        results = asyncio.run(process_all_async(_rows(8), max_concurrency=2))

    assert len(results) == 8
    assert peak == 2


def test_parallel_is_faster_than_sequential():
    """6 запитів по 0.1с при ліміті 3 мають виконатись приблизно за 0.2с,
    а не за 0.6с (послідовно)."""

    def fake(request_id, raw_text, channel):
        time.sleep(0.1)
        return _ok(request_id)

    with patch("main.classify_request", side_effect=fake):
        start = time.perf_counter()
        asyncio.run(process_all_async(_rows(6), max_concurrency=3))
        elapsed = time.perf_counter() - start

    assert elapsed < 0.45


def test_partial_results_saved_on_runtime_error(tmp_path, monkeypatch):
    """При критичній помилці на 3-му запиті два вже оброблені зберігаються,
    а решта запитів не стартує."""
    monkeypatch.chdir(tmp_path)
    called = []

    def fake(request_id, raw_text, channel):
        called.append(request_id)
        if request_id == "REQ-003":
            raise RuntimeError("Помилка авторизації API: невалідний ключ")
        return _ok(request_id)

    with patch("main.classify_request", side_effect=fake), pytest.raises(RuntimeError):
        asyncio.run(process_all_async(_rows(5), max_concurrency=1))

    saved = json.loads((tmp_path / "output.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in saved] == ["REQ-001", "REQ-002"]
    assert called == ["REQ-001", "REQ-002", "REQ-003"]  # REQ-004/005 не стартували


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5", 5), ("0", 1), ("-3", 1), ("abc", 3), ("", 3)],
)
def test_get_max_concurrency(monkeypatch, value, expected):
    monkeypatch.setenv("MAX_CONCURRENCY", value)
    assert get_max_concurrency() == expected
