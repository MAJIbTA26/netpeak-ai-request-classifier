"""
Тести Telegram-сповіщень: форматування, розбиття довгих текстів,
маршрутизація по відділах, відсутність винятків при збоях.
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import telegram_notifier
from telegram_notifier import (
    TelegramError,
    build_card,
    build_summary,
    chunk_text,
    get_department_chats,
    notify_results,
    send_message,
)


def _result(request_id, department="маркетинг", priority="medium", **extra):
    base = {
        "id": request_id,
        "category": "автоматизація",
        "target_department": department,
        "priority": priority,
        "short_summary": f"Суть {request_id}",
        "requested_actions": [],
        "needs_clarification": False,
        "processing_status": "ok",
    }
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(telegram_notifier, "SEND_DELAY_SECONDS", 0)
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_DEPARTMENT_CHATS"):
        monkeypatch.delenv(name, raising=False)


def test_summary_counts():
    results = [
        _result("REQ-001", priority="high", needs_clarification=True),
        _result("REQ-002", department="HR", priority="low"),
        _result("REQ-003", department="", processing_status="failed"),
    ]
    text = build_summary(results, "file.csv")
    assert "оброблено 3 запитів" in text
    assert "file.csv" in text
    assert "high: 1" in text
    assert "Потребують уточнення: 1" in text
    assert "Не вдалося обробити: 1" in text
    assert "не визначено: 1" in text


def test_card_shows_priority_actions_and_clarification():
    card = build_card(
        _result("REQ-007", priority="high", requested_actions=["зробити звіт", "надіслати"], needs_clarification=True)
    )
    assert card.startswith("🔴 REQ-007")
    assert "→ зробити звіт; надіслати" in card
    assert "потрібне уточнення" in card


def test_card_marks_failed():
    assert build_card(_result("REQ-009", processing_status="failed")).startswith("⚠️ REQ-009")


def test_chunk_text_respects_limit():
    text = "\n\n".join(f"абзац {i} " + "x" * 100 for i in range(20))
    chunks = chunk_text(text, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    assert "\n\n".join(chunks) == text


def test_chunk_text_splits_single_huge_paragraph():
    chunks = chunk_text("y" * 1200, limit=500)
    assert [len(c) for c in chunks] == [500, 500, 200]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"Маркетинг": "-100", "HR": 5}', {"маркетинг": "-100", "hr": "5"}),
        ("", {}),
        ("не json", {}),
        ('["список"]', {}),
    ],
)
def test_get_department_chats(monkeypatch, raw, expected):
    monkeypatch.setenv("TELEGRAM_DEPARTMENT_CHATS", raw)
    assert get_department_chats() == expected


def test_notify_skipped_without_config():
    with patch("telegram_notifier.send_message") as send:
        assert notify_results([_result("REQ-001")], "f.csv") is False
    send.assert_not_called()


def test_notify_routes_departments_to_their_chats(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "MAIN")
    monkeypatch.setenv("TELEGRAM_DEPARTMENT_CHATS", '{"маркетинг": "MKT"}')
    results = [_result("REQ-001", "маркетинг"), _result("REQ-002", "HR")]

    with patch("telegram_notifier.send_message") as send:
        assert notify_results(results, "f.csv") is True

    chats = [call.args[1] for call in send.call_args_list]
    # підсумок -> основний чат; маркетинг -> свій чат; HR (чату немає) -> основний
    assert chats == ["MAIN", "MKT", "MAIN"]
    assert all(call.args[0] == "TOKEN" for call in send.call_args_list)


def test_notify_never_raises_on_telegram_failure(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "MAIN")
    with patch("telegram_notifier.send_message", side_effect=TelegramError("boom")):
        assert notify_results([_result("REQ-001")], "f.csv") is False


def test_send_message_builds_correct_request():
    response = MagicMock()
    response.__enter__.return_value = response
    with patch("telegram_notifier.urllib.request.urlopen", return_value=response) as urlopen:
        send_message("TOKEN", "123", "привіт")

    request = urlopen.call_args.args[0]
    assert request.full_url == "https://api.telegram.org/botTOKEN/sendMessage"
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["chat_id"] == "123"
    assert payload["text"] == "привіт"


def test_send_message_error_does_not_leak_token():
    error = urllib.error.HTTPError("https://api.telegram.org/botSECRET/sendMessage", 401, "Unauthorized", {}, None)
    with patch("telegram_notifier.urllib.request.urlopen", side_effect=error), pytest.raises(TelegramError) as info:
        send_message("SECRET", "123", "текст")
    assert "SECRET" not in str(info.value)
    assert "401" in str(info.value)


def test_send_message_retries_once_on_429(monkeypatch):
    monkeypatch.setattr(telegram_notifier.time, "sleep", lambda _: None)
    body = MagicMock()
    body.read.return_value = json.dumps({"parameters": {"retry_after": 1}}).encode("utf-8")
    too_many = urllib.error.HTTPError("https://api.telegram.org/botT/sendMessage", 429, "Too Many", {}, body)
    ok = MagicMock()
    ok.__enter__.return_value = ok

    with patch("telegram_notifier.urllib.request.urlopen", side_effect=[too_many, ok]) as urlopen:
        send_message("T", "1", "текст")

    assert urlopen.call_count == 2
