"""
Тести запису в Google Sheets: формування рядків, створення заголовків,
append-запит, пропуск без налаштувань, відсутність винятків при збоях.
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import sheets_logger
from sheets_logger import HEADER, SheetsError, append_results, build_row


def _result(request_id="REQ-001", **extra):
    base = {
        "id": request_id,
        "channel": "Slack",
        "timestamp": "2026-01-01",
        "category": "автоматизація",
        "target_department": "маркетинг",
        "priority": "high",
        "short_summary": "Суть запиту",
        "requested_actions": ["зробити звіт", "надіслати"],
        "needs_clarification": False,
        "processing_status": "ok",
    }
    base.update(extra)
    return base


def _response(status=200, body=None):
    response = MagicMock()
    response.status_code = status
    response.content = b"x" if body is not None else b""
    response.json.return_value = body if body is not None else {}
    return response


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "GOOGLE_APPS_SCRIPT_URL",
        "GOOGLE_APPS_SCRIPT_TOKEN",
        "GOOGLE_SHEET_ID",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "GOOGLE_SHEET_TAB",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "SHEET123")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps({"client_email": "bot@example.iam"}))


def test_build_row_matches_header_and_formats_values():
    row = build_row(_result(needs_clarification=True), "file.csv", "2026-10-05 18:00:00")
    assert len(row) == len(HEADER)
    assert row[0] == "2026-10-05 18:00:00"
    assert row[1] == "file.csv"
    assert row[2] == "REQ-001"
    assert row[6] == "маркетинг"
    assert row[9] == "зробити звіт; надіслати"
    assert row[10] == "так"
    assert row[12] == "нова"


def test_build_row_empty_department_and_no_actions():
    row = build_row(_result(target_department="", requested_actions=[]), "f.csv", "t")
    assert row[6] == "не визначено"
    assert row[9] == ""
    assert row[10] == "ні"


def test_skipped_without_sheet_id():
    with patch("sheets_logger._build_session") as build:
        assert append_results([_result()], "f.csv") is False
    build.assert_not_called()


def test_skipped_without_credentials(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "SHEET123")
    with patch("sheets_logger._build_session") as build:
        assert append_results([_result()], "f.csv") is False
    build.assert_not_called()


def test_writes_header_when_sheet_is_empty_then_appends(configured):
    session = MagicMock()
    session.request.side_effect = [_response(200, {}), _response(200, {}), _response(200, {"updates": {}})]

    with patch("sheets_logger._build_session", return_value=session):
        assert append_results([_result("REQ-001"), _result("REQ-002")], "f.csv") is True

    methods = [c.args[0] for c in session.request.call_args_list]
    assert methods == ["GET", "PUT", "POST"]
    put = session.request.call_args_list[1]
    assert put.kwargs["json"] == {"values": [HEADER]}
    post = session.request.call_args_list[2]
    assert post.args[1].endswith("/values/A1:append")
    assert "SHEET123" in post.args[1]
    assert post.kwargs["params"]["valueInputOption"] == "RAW"
    rows = post.kwargs["json"]["values"]
    assert [r[2] for r in rows] == ["REQ-001", "REQ-002"]


def test_skips_header_when_already_present(configured):
    session = MagicMock()
    session.request.side_effect = [_response(200, {"values": [HEADER]}), _response(200, {})]

    with patch("sheets_logger._build_session", return_value=session):
        assert append_results([_result()], "f.csv") is True

    assert [c.args[0] for c in session.request.call_args_list] == ["GET", "POST"]


def test_uses_named_tab_in_range(configured, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_TAB", "Запити")
    session = MagicMock()
    session.request.side_effect = [_response(200, {"values": [HEADER]}), _response(200, {})]

    with patch("sheets_logger._build_session", return_value=session):
        append_results([_result()], "f.csv")

    assert "'Запити'!A1:append" in session.request.call_args_list[1].args[1]


def test_api_error_is_logged_not_raised(configured):
    session = MagicMock()
    session.request.return_value = _response(403, {"error": {"message": "The caller does not have permission"}})

    with patch("sheets_logger._build_session", return_value=session):
        assert append_results([_result()], "f.csv") is False


def test_unexpected_exception_is_not_raised(configured):
    with patch("sheets_logger._build_session", side_effect=ValueError("bad key")):
        assert append_results([_result()], "f.csv") is False


def test_invalid_service_account_json_is_not_raised(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "SHEET123")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "не json")
    with patch("sheets_logger._build_session") as build:
        assert append_results([_result()], "f.csv") is False
    build.assert_not_called()


def test_credentials_can_be_read_from_file(monkeypatch, tmp_path):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"client_email": "bot@example.iam"}), encoding="utf-8")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(key))
    assert sheets_logger._load_service_account_info() == {"client_email": "bot@example.iam"}


def test_call_raises_sheets_error_with_reason():
    session = MagicMock()
    session.request.return_value = _response(404, {"error": {"message": "Requested entity was not found."}})
    with pytest.raises(SheetsError, match="HTTP 404"):
        sheets_logger._call(session, "GET", "https://example.test")


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_URL", "https://script.google.com/macros/s/ABC/exec")
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_TOKEN", "SECRET-TOKEN")


def _http_response(body):
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = body.encode("utf-8")
    return response


def test_webhook_posts_rows_with_token_and_header(webhook, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_TAB", "Запити")
    with patch(
        "sheets_logger.urllib.request.urlopen", return_value=_http_response('{"ok": true, "written": 2}')
    ) as urlopen:
        assert append_results([_result("REQ-001"), _result("REQ-002")], "f.csv") is True

    request = urlopen.call_args.args[0]
    assert request.full_url == "https://script.google.com/macros/s/ABC/exec"
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["token"] == "SECRET-TOKEN"
    assert payload["tab"] == "Запити"
    assert payload["header"] == HEADER
    assert [r[2] for r in payload["rows"]] == ["REQ-001", "REQ-002"]


def test_webhook_takes_priority_over_service_account(webhook, configured):
    with (
        patch("sheets_logger.urllib.request.urlopen", return_value=_http_response('{"ok": true}')),
        patch("sheets_logger._build_session") as build,
    ):
        assert append_results([_result()], "f.csv") is True
    build.assert_not_called()


def test_webhook_skipped_without_token(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPS_SCRIPT_URL", "https://script.google.com/macros/s/ABC/exec")
    with patch("sheets_logger.urllib.request.urlopen") as urlopen:
        assert append_results([_result()], "f.csv") is False
    urlopen.assert_not_called()


def test_webhook_rejected_by_script_returns_false(webhook):
    with patch(
        "sheets_logger.urllib.request.urlopen", return_value=_http_response('{"ok": false, "error": "unauthorized"}')
    ):
        assert append_results([_result()], "f.csv") is False


def test_webhook_html_response_returns_false(webhook):
    with patch("sheets_logger.urllib.request.urlopen", return_value=_http_response("<html>Sign in</html>")):
        assert append_results([_result()], "f.csv") is False


def test_webhook_network_error_returns_false(webhook):
    with patch("sheets_logger.urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
        assert append_results([_result()], "f.csv") is False


def test_webhook_error_message_does_not_leak_url_or_token(webhook):
    error = urllib.error.HTTPError("https://script.google.com/macros/s/ABC/exec", 500, "err", {}, None)
    with patch("sheets_logger.urllib.request.urlopen", side_effect=error), pytest.raises(SheetsError) as info:
        sheets_logger._append_via_webhook("https://script.google.com/macros/s/ABC/exec", "SECRET-TOKEN", "", [])
    assert "SECRET-TOKEN" not in str(info.value)
    assert "ABC" not in str(info.value)
