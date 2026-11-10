"""Тести HTTP-обгортки api.py без мережі й бази: Gemini та база замінені заглушками."""

import pytest
from fastapi.testclient import TestClient

import api
from db import DatabaseUnavailableError
from schema import RequestAnalysis

API_KEY = "test-key"
HEADERS = {"X-API-Key": API_KEY}

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


def make_record(**overrides) -> dict:
    record = {
        "id": 7,
        "run_id": 3,
        "request_id": "REQ-001",
        "channel": "Slack",
        "category": "баг/підтримка",
        "target_department": "Фінанси",
        "priority": "low",
        "priority_override": None,
        "effective_priority": "low",
        "overridden_by": None,
        "overridden_at": None,
        "short_summary": "Не працює звіт.",
        "requested_actions": [],
        "needs_clarification": False,
        "processing_status": "ok",
    }
    record.update(overrides)
    return record


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("API_KEY", API_KEY)


@pytest.fixture
def fake_classifier(monkeypatch):
    """Підміняє classify_request і базу; запам'ятовує, з якими аргументами викликали."""
    calls = []
    saved = []

    def fake(request_id, raw_text, channel):
        calls.append((request_id, raw_text, channel))
        return make_analysis(request_id)

    def fake_save(record):
        saved.append(record)
        return 42

    monkeypatch.setattr(api, "classify_request", fake)
    monkeypatch.setattr(api, "save_api_result", fake_save)
    fake.calls, fake.saved = calls, saved
    return fake


def test_health_needs_no_key():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- Захист ключем -------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("post", "/classify", {"id": "R", "text": "t"}),
        ("get", "/results/1", None),
        ("patch", "/results/1/priority", {"priority": "high", "changed_by": "m"}),
        ("get", "/stats/overrides", None),
    ],
)
def test_protected_endpoints_reject_missing_and_wrong_key(fake_classifier, method, url, body):
    assert client.request(method, url, json=body).status_code == 401
    assert client.request(method, url, json=body, headers={"X-API-Key": "wrong"}).status_code == 401
    assert fake_classifier.calls == []  # до Gemini не дійшли


def test_protected_endpoints_closed_when_server_has_no_key(monkeypatch, fake_classifier):
    monkeypatch.delenv("API_KEY", raising=False)

    response = client.post("/classify", json={"id": "R", "text": "t"}, headers=HEADERS)

    assert response.status_code == 503
    assert fake_classifier.calls == []


# --- /classify -----------------------------------------------------------------


def test_classify_returns_analysis_and_record_id(fake_classifier):
    response = client.post(
        "/classify", json={"id": "REQ-001", "text": "Не працює звіт", "channel": "Slack"}, headers=HEADERS
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "REQ-001"
    assert body["category"] == "баг/підтримка"
    assert body["priority"] == "high"
    assert body["processing_status"] == "ok"
    assert body["record_id"] == 42
    assert fake_classifier.calls == [("REQ-001", "Не працює звіт", "Slack")]


def test_classify_saves_record_with_channel_and_timestamp(fake_classifier):
    client.post("/classify", json={"id": "REQ-009", "text": "Потрібен бот", "channel": "Email"}, headers=HEADERS)

    (record,) = fake_classifier.saved
    assert record["id"] == "REQ-009"
    assert record["channel"] == "Email"
    assert record["timestamp"]


def test_classify_works_without_database(monkeypatch, fake_classifier):
    monkeypatch.setattr(api, "save_api_result", lambda record: None)

    response = client.post("/classify", json={"id": "REQ-010", "text": "тест"}, headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["record_id"] is None


def test_channel_defaults_to_api(fake_classifier):
    client.post("/classify", json={"id": "REQ-002", "text": "Потрібен бот"}, headers=HEADERS)

    assert fake_classifier.calls == [("REQ-002", "Потрібен бот", "API")]


def test_text_is_stripped(fake_classifier):
    client.post("/classify", json={"id": "REQ-003", "text": "   Потрібен звіт   "}, headers=HEADERS)

    assert fake_classifier.calls[0][1] == "Потрібен звіт"


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
    response = client.post("/classify", json=payload, headers=HEADERS)

    assert response.status_code == 422
    assert fake_classifier.calls == []


def test_failed_status_is_passed_through(monkeypatch):
    failed = make_analysis("REQ-004", processing_status="failed", needs_clarification=True)
    monkeypatch.setattr(api, "classify_request", lambda *args: failed)
    monkeypatch.setattr(api, "save_api_result", lambda record: 1)

    response = client.post("/classify", json={"id": "REQ-004", "text": "щось незрозуміле"}, headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["processing_status"] == "failed"


def test_configuration_error_returns_503_without_details(monkeypatch):
    def broken(*args):
        raise RuntimeError("GEMINI_API_KEY не знайдено")

    monkeypatch.setattr(api, "classify_request", broken)

    response = client.post("/classify", json={"id": "REQ-005", "text": "тест"}, headers=HEADERS)

    assert response.status_code == 503
    assert "GEMINI" not in response.text


# --- /results, /stats ----------------------------------------------------------


def test_read_result_returns_record(monkeypatch):
    monkeypatch.setattr(api, "get_result", lambda record_id: make_record(id=record_id))

    response = client.get("/results/7", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["id"] == 7
    assert response.json()["effective_priority"] == "low"


def test_read_result_not_found(monkeypatch):
    monkeypatch.setattr(api, "get_result", lambda record_id: None)

    assert client.get("/results/7", headers=HEADERS).status_code == 404


@pytest.mark.parametrize("bad_id", ["0", "-5", "abc", str(2**63)])
def test_invalid_record_id_returns_422(monkeypatch, bad_id):
    monkeypatch.setattr(api, "get_result", lambda record_id: make_record())

    assert client.get(f"/results/{bad_id}", headers=HEADERS).status_code == 422


def test_change_priority_passes_arguments_and_returns_record(monkeypatch):
    calls = []

    def fake(record_id, priority, changed_by):
        calls.append((record_id, priority, changed_by))
        return make_record(priority_override=priority, effective_priority=priority, overridden_by=changed_by)

    monkeypatch.setattr(api, "set_priority_override", fake)

    response = client.patch("/results/7/priority", json={"priority": "high", "changed_by": "  anna  "}, headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["effective_priority"] == "high"
    assert calls == [(7, "high", "anna")]


@pytest.mark.parametrize(
    "payload",
    [
        {"priority": "urgent", "changed_by": "m"},
        {"priority": "high"},
        {"priority": "high", "changed_by": "   "},
        {"changed_by": "m"},
    ],
)
def test_change_priority_validates_payload(monkeypatch, payload):
    monkeypatch.setattr(api, "set_priority_override", lambda *args: make_record())

    assert client.patch("/results/7/priority", json=payload, headers=HEADERS).status_code == 422


def test_change_priority_not_found(monkeypatch):
    monkeypatch.setattr(api, "set_priority_override", lambda *args: None)

    response = client.patch("/results/7/priority", json={"priority": "high", "changed_by": "m"}, headers=HEADERS)

    assert response.status_code == 404


def test_database_unavailable_returns_503(monkeypatch):
    def down(*args):
        raise DatabaseUnavailableError("down")

    monkeypatch.setattr(api, "get_result", down)
    monkeypatch.setattr(api, "set_priority_override", down)
    monkeypatch.setattr(api, "get_override_stats", down)

    assert client.get("/results/7", headers=HEADERS).status_code == 503
    assert (
        client.patch("/results/7/priority", json={"priority": "low", "changed_by": "m"}, headers=HEADERS).status_code
        == 503
    )
    assert client.get("/stats/overrides", headers=HEADERS).status_code == 503


def test_override_stats_endpoint(monkeypatch):
    stats = {"total": 3, "reviewed": 2, "changed": 1, "disagreement_rate": 0.5}
    monkeypatch.setattr(api, "get_override_stats", lambda: stats)

    response = client.get("/stats/overrides", headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == stats
