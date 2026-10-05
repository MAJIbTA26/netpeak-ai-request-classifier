"""
Запис результатів класифікації в Google Sheets (реєстр запитів).

Після обробки кожен запит додається в таблицю окремим рядком: час обробки,
файл, id, канал, категорія, відділ, пріоритет, суть, дії, ознаки
"потрібне уточнення" / "не вдалося обробити" і колонка "Статус роботи"
(спочатку "нова") - під подальше відстеження людиною.

Два способи запису (обирається за змінними середовища):

1. Google Apps Script (веб-застосунок) - НАЙПРОСТІШИЙ, без Google Cloud і ключів:
    GOOGLE_APPS_SCRIPT_URL     - URL розгорнутого веб-застосунку (.../exec)
    GOOGLE_APPS_SCRIPT_TOKEN   - секретний токен, який перевіряє скрипт
   Скрипт лежить у таблиці (Розширення -> Apps Script), приклад - у README.

2. Google Sheets API через сервісний акаунт (потрібен JSON-ключ; у частини
   організацій створення ключів заборонене політикою):
    GOOGLE_SHEET_ID               - ID таблиці (частина URL між /d/ і /edit)
    GOOGLE_SERVICE_ACCOUNT_JSON   - вміст JSON-ключа сервісного акаунта (для Lambda)
    GOOGLE_SERVICE_ACCOUNT_FILE   - або шлях до файлу з ключем (для локального запуску)

Спільне:
    GOOGLE_SHEET_TAB              - назва аркуша (необов'язково; за замовчуванням перший аркуш)

Якщо задано GOOGLE_APPS_SCRIPT_URL, використовується спосіб 1.

Якщо налаштувань немає - запис тихо пропускається. Збій Google НЕ ламає
основний конвеєр: помилка лише логується.

Спосіб 1 використовує лише стандартну бібліотеку; спосіб 2 - google-auth і
requests, які вже потрібні google-genai. Дані записуються як текст, тому
значення, що починаються з "=", не виконуються як формули.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
REQUEST_TIMEOUT_SECONDS = 15
WEBHOOK_TIMEOUT_SECONDS = 30  # Apps Script відповідає повільніше за Sheets API
INITIAL_STATUS = "нова"

HEADER = [
    "Оброблено (UTC)",
    "Файл",
    "ID",
    "Канал",
    "Час запиту",
    "Категорія",
    "Відділ",
    "Пріоритет",
    "Суть",
    "Дії",
    "Потрібне уточнення",
    "Статус обробки",
    "Статус роботи",
]
LAST_COLUMN = chr(ord("A") + len(HEADER) - 1)  # "M"


class SheetsError(Exception):
    """Не вдалося записати дані в Google Sheets."""


def build_row(result: dict[str, Any], source_name: str, processed_at: str) -> list[str]:
    """Перетворює один результат класифікації на рядок таблиці.

    Args:
        result: Один елемент результатів класифікації.
        source_name: Назва оброблюваного файлу.
        processed_at: Час обробки (UTC, рядок).

    Returns:
        Список значень у порядку колонок HEADER.
    """
    actions = "; ".join(str(a).strip() for a in result.get("requested_actions") or [] if str(a).strip())
    return [
        processed_at,
        source_name,
        str(result.get("id", "")),
        str(result.get("channel", "")),
        str(result.get("timestamp", "")),
        str(result.get("category", "")),
        str(result.get("target_department", "") or "не визначено"),
        str(result.get("priority", "")),
        str(result.get("short_summary", "")),
        actions,
        "так" if result.get("needs_clarification") else "ні",
        str(result.get("processing_status", "")),
        INITIAL_STATUS,
    ]


def _load_service_account_info() -> dict[str, Any] | None:
    """Читає ключ сервісного акаунта зі змінної (JSON) або з файлу.

    Returns:
        Словник з ключем або None, якщо ключ не задано.

    Raises:
        SheetsError: якщо ключ задано, але його не вдалося прочитати.
    """
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    try:
        if raw:
            return json.loads(raw)
        if path:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError):
        raise SheetsError("Не вдалося прочитати ключ сервісного акаунта Google (перевір JSON / шлях).") from None
    return None


def _build_session(info: dict[str, Any]) -> Any:
    """Створює авторизовану HTTP-сесію для Google Sheets API.

    Імпорти зроблено всередині функції, щоб модуль не вимагав google-auth,
    поки запис у таблицю не налаштовано.
    """
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(credentials)


def _call(session: Any, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """Виконує запит до Sheets API і повертає JSON-відповідь.

    Raises:
        SheetsError: якщо API повернув помилку (з короткою причиною від Google).
    """
    response = session.request(method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
    if response.status_code >= 400:
        try:
            reason = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            reason = "без деталей"
        raise SheetsError(f"Google Sheets API: HTTP {response.status_code}: {reason}")
    return response.json() if response.content else {}


def _range(tab: str, cells: str) -> str:
    """Формує A1-діапазон; для аркуша з назвою обгортає її в лапки."""
    if not tab:
        return cells
    return f"'{tab.replace(chr(39), chr(39) * 2)}'!{cells}"


def _append_via_webhook(url: str, token: str, tab: str, rows: list[list[str]]) -> None:
    """Надсилає рядки в Google Apps Script (веб-застосунок), що додає їх у таблицю.

    Args:
        url: URL розгорнутого веб-застосунку (закінчується на /exec).
        token: Секретний токен, який перевіряє скрипт.
        tab: Назва аркуша (порожній рядок - перший аркуш).
        rows: Рядки для додавання.

    Raises:
        SheetsError: якщо скрипт недоступний, відповів не JSON або відхилив запит.
            Текст помилки не містить URL і токена.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps({"token": token, "tab": tab, "header": HEADER, "rows": rows}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise SheetsError(f"Apps Script повернув HTTP {e.code}") from None
    except OSError as e:
        raise SheetsError(f"Apps Script недоступний ({type(e).__name__})") from None

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise SheetsError(
            'Apps Script відповів не JSON (перевір: доступ веб-застосунку "Усі" / Anyone, URL закінчується на /exec)'
        ) from None
    if not data.get("ok"):
        raise SheetsError(f"Apps Script відхилив запит: {str(data.get('error', 'без деталей'))[:200]}")


def _append_via_api(sheet_id: str, tab: str, rows: list[list[str]]) -> bool:
    """Додає рядки через Google Sheets API (сервісний акаунт).

    Returns:
        True, якщо рядки записано; False, якщо ключ сервісного акаунта не задано.

    Raises:
        SheetsError: якщо Sheets API повернув помилку.
    """
    info = _load_service_account_info()
    if info is None:
        logger.info("Google Sheets: не задано ключ сервісного акаунта, пропускаю.")
        return False

    session = _build_session(info)
    base = f"{SHEETS_API}/{sheet_id}"

    header_range = _range(tab, f"A1:{LAST_COLUMN}1")
    existing = _call(session, "GET", f"{base}/values/{header_range}")
    if not existing.get("values"):
        _call(
            session,
            "PUT",
            f"{base}/values/{header_range}",
            params={"valueInputOption": "RAW"},
            json={"values": [HEADER]},
        )

    _call(
        session,
        "POST",
        f"{base}/values/{_range(tab, 'A1')}:append",
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": rows},
    )
    return True


def append_results(results: list[dict[str, Any]], source_name: str) -> bool:
    """Додає результати класифікації в Google Sheets.

    Ніколи не піднімає виняток: помилка лише логується, щоб не зіпсувати
    вже готовий результат обробки. Якщо таблиця порожня, спочатку
    записується рядок із заголовками.

    Args:
        results: Список результатів класифікації.
        source_name: Назва оброблюваного файлу.

    Returns:
        True, якщо рядки записано; False, якщо запис не налаштовано, немає
        результатів або виникла помилка.
    """
    webhook_url = os.environ.get("GOOGLE_APPS_SCRIPT_URL", "").strip()
    webhook_token = os.environ.get("GOOGLE_APPS_SCRIPT_TOKEN", "").strip()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    tab = os.environ.get("GOOGLE_SHEET_TAB", "").strip()

    if not webhook_url and not sheet_id:
        logger.info("Google Sheets не налаштовано (GOOGLE_APPS_SCRIPT_URL або GOOGLE_SHEET_ID), пропускаю.")
        return False
    if webhook_url and not webhook_token:
        logger.warning("Задано GOOGLE_APPS_SCRIPT_URL, але не задано GOOGLE_APPS_SCRIPT_TOKEN, пропускаю.")
        return False
    if not results:
        return False

    processed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = [build_row(r, source_name, processed_at) for r in results]

    try:
        if webhook_url:
            _append_via_webhook(webhook_url, webhook_token, tab, rows)
        elif not _append_via_api(sheet_id, tab, rows):
            return False
    except SheetsError as e:
        logger.error(f"Не вдалося записати в Google Sheets: {e}")
        return False
    except Exception as e:  # будь-який збій Google (автентифікація, мережа) не має ламати конвеєр
        logger.error(f"Не вдалося записати в Google Sheets ({type(e).__name__}): {str(e)[:200]}")
        return False

    logger.info(f"Google Sheets: записано {len(rows)} рядків.")
    return True
