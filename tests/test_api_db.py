"""Наскрізний тест API разом зі справжньою базою (Gemini замінено заглушкою).

Потрібен TEST_DATABASE_URL, інакше тест пропускається (фікстура db_url у conftest.py).
"""

import pytest
from fastapi.testclient import TestClient

import api
from schema import RequestAnalysis

HEADERS = {"X-API-Key": "test-key"}
client = TestClient(api.app)


@pytest.fixture
def wired(monkeypatch, db_url):
    """API з ключем, справжньою базою (тимчасова схема) і підміненою класифікацією."""
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", db_url)

    def fake(request_id, raw_text, channel):
        return RequestAnalysis(
            id=request_id,
            category="баг/підтримка",
            target_department="Фінанси",
            priority="low",
            short_summary="Тест.",
            requested_actions=["перевірити"],
            needs_clarification=False,
        )

    monkeypatch.setattr(api, "classify_request", fake)


def test_classify_then_manager_changes_priority(wired):
    created = client.post(
        "/classify", json={"id": "REQ-1", "text": "Не працює звіт", "channel": "Slack"}, headers=HEADERS
    ).json()
    record_id = created["record_id"]
    assert record_id == 1

    before = client.get(f"/results/{record_id}", headers=HEADERS).json()
    assert (before["priority"], before["priority_override"], before["effective_priority"]) == ("low", None, "low")

    patched = client.patch(
        f"/results/{record_id}/priority", json={"priority": "high", "changed_by": "anna"}, headers=HEADERS
    ).json()
    assert patched["priority"] == "low"  # AI-значення збережено
    assert patched["effective_priority"] == "high"
    assert patched["overridden_by"] == "anna"

    stats = client.get("/stats/overrides", headers=HEADERS).json()
    assert stats == {"total": 1, "reviewed": 1, "changed": 1, "disagreement_rate": 1.0}


def test_unknown_record_returns_404(wired):
    client.post("/classify", json={"id": "REQ-1", "text": "тест"}, headers=HEADERS)  # створює таблиці

    assert client.get("/results/999", headers=HEADERS).status_code == 404
    patch = client.patch("/results/999/priority", json={"priority": "high", "changed_by": "m"}, headers=HEADERS)
    assert patch.status_code == 404
