"""Тести HTTP-обгортки api.py. Gemini замінюємо заглушкою: мережі й ключа не потрібно."""

import pytest
from fastapi.testclient import TestClient

import api
from schema import RequestAnalysis

client = TestClient(api.app)


def make_analysis(request_id: str = "REQ-001", **overrides) -> RequestAnalysis:
    data = {
        "id": request_id,
        "category": "баг/підтримка",
        "target_department": "Фінанси",
        "priority": "high",
        "short_summary": "Не працює вивантаження звіту.",
        "requested_actions": ["перевірити експорт"],
        "needs_clarification": False,
    }
    data.update(overrides)
    return RequestAnalysis(**data)


@pytest.fixture
def fake_classifier(monkeypatch):
    """Підміняє classify_request і запам'ятовує, з якими аргументами його викликали."""
    calls = []

    def fake(request_id, raw_text, channel):
        calls.append((request_id, raw_text, channel))
        return make_analysis(request_id)

    monkeypatch.setattr(api, "classify_request", fake)
    return calls


def test_health():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_classify_returns_analysis(fake_classifier):
    response = client.post("/classify", json={"id": "REQ-001", "text": "Не працює звіт", "channel": "Slack"})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "REQ-001"
    assert body["category"] == "баг/підтримка"
    assert body["priority"] == "high"
    assert body["processing_status"] == "ok"
    assert fake_classifier == [("REQ-001", "Не працює звіт", "Slack")]


def test_channel_defaults_to_api(fake_classifier):
    client.post("/classify", json={"id": "REQ-002", "text": "Потрібен бот"})

    assert fake_classifier == [("REQ-002", "Потрібен бот", "API")]


def test_text_is_stripped(fake_classifier):
    client.post("/classify", json={"id": "REQ-003", "text": "   Потрібен звіт   "})

    assert fake_classifier[0][1] == "Потрібен звіт"


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "немає id"},
        {"id": "REQ-001"},
        {"id": "REQ-001", "text": ""},
        {"id": "REQ-001", "text": "   "},
        {"id": "", "text": "порожній id"},
        {"id": "REQ-001", "text": "x" * 5001},
    ],
)
def test_invalid_payload_returns_422(fake_classifier, payload):
    response = client.post("/classify", json=payload)

    assert response.status_code == 422
    assert fake_classifier == []  # до Gemini навіть не дійшли


def test_failed_status_is_passed_through(monkeypatch):
    failed = make_analysis("REQ-004", processing_status="failed", needs_clarification=True)
    monkeypatch.setattr(api, "classify_request", lambda *args: failed)

    response = client.post("/classify", json={"id": "REQ-004", "text": "щось незрозуміле"})

    assert response.status_code == 200
    assert response.json()["processing_status"] == "failed"


def test_configuration_error_returns_503_without_details(monkeypatch):
    def broken(*args):
        raise RuntimeError("GEMINI_API_KEY не знайдено")

    monkeypatch.setattr(api, "classify_request", broken)

    response = client.post("/classify", json={"id": "REQ-005", "text": "тест"})

    assert response.status_code == 503
    assert "GEMINI" not in response.text
