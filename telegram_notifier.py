"""
Telegram-сповіщення про результати обробки запитів.

Після обробки CSV сервіс надсилає в Telegram:
    1. Підсумкове повідомлення (скільки запитів, пріоритети, відділи,
       скільки потребують уточнення чи не вдалось обробити).
    2. По одному повідомленню на кожен відділ зі списком його запитів
       (спочатку найважливіші). Якщо для відділу задано окремий чат
       (TELEGRAM_DEPARTMENT_CHATS) - повідомлення йде туди, інакше - у
       основний чат (TELEGRAM_CHAT_ID).

Налаштування (змінні середовища):
    TELEGRAM_BOT_TOKEN         - токен бота від @BotFather (обов'язково)
    TELEGRAM_CHAT_ID           - основний чат (обов'язково)
    TELEGRAM_DEPARTMENT_CHATS  - JSON {"маркетинг": "-100123", ...} (необов'язково)

Якщо токен або чат не задані - сповіщення тихо пропускаються. Збій
Telegram НЕ ламає основний конвеєр: помилка лише логується.

Використовується тільки стандартна бібліотека (urllib), щоб не збільшувати
розмір пакета для Lambda.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4000  # ліміт Telegram - 4096 символів, лишаємо запас
SEND_DELAY_SECONDS = 0.5  # пауза між повідомленнями (ліміт Telegram ~1 повідомлення/сек у чат)
UNKNOWN_DEPARTMENT = "не визначено"

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


class TelegramError(Exception):
    """Не вдалося надіслати повідомлення в Telegram."""


def get_department_chats() -> dict[str, str]:
    """Читає зі змінної TELEGRAM_DEPARTMENT_CHATS відповідність відділ -> chat_id.

    Returns:
        Словник {назва відділу в нижньому регістрі: chat_id}. Порожній,
        якщо змінну не задано або вона містить некоректний JSON.
    """
    raw = os.environ.get("TELEGRAM_DEPARTMENT_CHATS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("TELEGRAM_DEPARTMENT_CHATS містить некоректний JSON, ігнорую.")
        return {}
    if not isinstance(data, dict):
        logger.warning("TELEGRAM_DEPARTMENT_CHATS має бути JSON-об'єктом, ігнорую.")
        return {}
    return {str(dept).strip().lower(): str(chat).strip() for dept, chat in data.items()}


def _department_of(result: dict[str, Any]) -> str:
    return (result.get("target_department") or "").strip() or UNKNOWN_DEPARTMENT


def _sort_key(result: dict[str, Any]) -> tuple[int, int, str]:
    failed = 1 if result.get("processing_status") == "failed" else 0
    return (failed, PRIORITY_ORDER.get(result.get("priority", "low"), 3), str(result.get("id", "")))


def build_summary(results: list[dict[str, Any]], source_name: str) -> str:
    """Формує підсумкове повідомлення по всій обробці.

    Args:
        results: Список результатів класифікації.
        source_name: Назва оброблюваного файлу (для заголовка).

    Returns:
        Текст повідомлення.
    """
    by_priority = {"high": 0, "medium": 0, "low": 0}
    by_department: dict[str, int] = {}
    for r in results:
        if r.get("priority") in by_priority:
            by_priority[r["priority"]] += 1
        dept = _department_of(r)
        by_department[dept] = by_department.get(dept, 0) + 1

    failed = sum(1 for r in results if r.get("processing_status") == "failed")
    unclear = sum(1 for r in results if r.get("needs_clarification"))

    lines = [
        f"📬 Request Classifier: оброблено {len(results)} запитів",
        f"Файл: {source_name}",
        "",
        (
            f"{PRIORITY_EMOJI['high']} high: {by_priority['high']}  "
            f"{PRIORITY_EMOJI['medium']} medium: {by_priority['medium']}  "
            f"{PRIORITY_EMOJI['low']} low: {by_priority['low']}"
        ),
        f"❓ Потребують уточнення: {unclear}",
        f"⚠️ Не вдалося обробити: {failed}",
        "",
        "По відділах:",
    ]
    for dept, count in sorted(by_department.items(), key=lambda x: -x[1]):
        lines.append(f"• {dept}: {count}")
    return "\n".join(lines)


def build_card(result: dict[str, Any]) -> str:
    """Формує короткий текстовий опис одного запиту.

    Args:
        result: Один елемент результатів класифікації.

    Returns:
        Багаторядковий текст: пріоритет, id, категорія, суть, дії, позначки.
    """
    failed = result.get("processing_status") == "failed"
    emoji = "⚠️" if failed else PRIORITY_EMOJI.get(result.get("priority", ""), "⚪")
    lines = [f"{emoji} {result.get('id', '?')} · {result.get('category', '?')}"]

    summary = str(result.get("short_summary", "")).strip()
    if len(summary) > 300:
        summary = summary[:297] + "..."
    lines.append(summary)

    actions = [str(a).strip() for a in result.get("requested_actions") or [] if str(a).strip()]
    if actions:
        lines.append("→ " + "; ".join(actions[:3]))
    if result.get("needs_clarification") and not failed:
        lines.append("❓ потрібне уточнення")
    return "\n".join(lines)


def build_department_message(department: str, items: list[dict[str, Any]]) -> str:
    """Формує повідомлення зі списком запитів одного відділу (важливі зверху).

    Args:
        department: Назва відділу.
        items: Результати, що належать цьому відділу.

    Returns:
        Текст повідомлення (може перевищувати ліміт Telegram - див. chunk_text).
    """
    ordered = sorted(items, key=_sort_key)
    header = f"📂 {department}: {len(ordered)} запитів"
    return header + "\n\n" + "\n\n".join(build_card(r) for r in ordered)


def chunk_text(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Ділить довгий текст на частини не довші за limit, по межах абзаців.

    Args:
        text: Вихідний текст.
        limit: Максимальна довжина однієї частини.

    Returns:
        Список частин (завжди хоча б одна).
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        while len(paragraph) > limit:  # аварійний випадок: один абзац довший за ліміт
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > limit:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _retry_after(error: urllib.error.HTTPError, default: float = 5.0, maximum: float = 30.0) -> float:
    try:
        body = json.loads(error.read().decode("utf-8"))
        return min(float(body["parameters"]["retry_after"]), maximum)
    except (ValueError, KeyError, TypeError, OSError):
        return default


def send_message(token: str, chat_id: str, text: str, timeout: float = 10.0) -> None:
    """Надсилає одне текстове повідомлення через Telegram Bot API.

    При відповіді 429 (занадто часто) один раз чекає рекомендований час і
    повторює спробу.

    Args:
        token: Токен бота.
        chat_id: Ідентифікатор чату.
        text: Текст повідомлення (до 4096 символів).
        timeout: Таймаут запиту в секундах.

    Raises:
        TelegramError: якщо надіслати не вдалося. Текст помилки НЕ містить
            токена (він є частиною URL, тому виняток urllib не прокидається).
    """
    request = urllib.request.Request(
        f"{API_URL}/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                delay = _retry_after(e)
                logger.warning(f"Telegram: забагато запитів, чекаю {delay:.0f} сек.")
                time.sleep(delay)
                continue
            raise TelegramError(f"Telegram API повернув HTTP {e.code}") from None
        except OSError as e:
            raise TelegramError(f"Telegram недоступний ({type(e).__name__})") from None


def notify_results(results: list[dict[str, Any]], source_name: str) -> bool:
    """Надсилає в Telegram підсумок і запити по відділах.

    Ніколи не піднімає виняток: помилка Telegram лише логується, щоб не
    зіпсувати вже готовий результат обробки.

    Args:
        results: Список результатів класифікації.
        source_name: Назва оброблюваного файлу.

    Returns:
        True, якщо всі повідомлення надіслано; False, якщо сповіщення не
        налаштовано, немає результатів або виникла помилка.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    default_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not default_chat:
        logger.info("Telegram не налаштовано (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID), пропускаю.")
        return False
    if not results:
        return False

    department_chats = get_department_chats()
    by_department: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_department.setdefault(_department_of(r), []).append(r)

    messages: list[tuple[str, str]] = [(default_chat, build_summary(results, source_name))]
    for department, items in by_department.items():
        chat = department_chats.get(department.lower(), default_chat)
        messages.extend((chat, part) for part in chunk_text(build_department_message(department, items)))

    try:
        for i, (chat, text) in enumerate(messages):
            if i:
                time.sleep(SEND_DELAY_SECONDS)
            send_message(token, chat, text)
    except TelegramError as e:
        logger.error(f"Не вдалося надіслати Telegram-сповіщення: {e}")
        return False

    logger.info(f"Telegram: надіслано {len(messages)} повідомлень.")
    return True
