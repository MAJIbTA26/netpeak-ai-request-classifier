"""
Головний скрипт: читає input_requests.csv, класифікує кожен запит через
Gemini API, зберігає результат у output.json та формує короткий звіт
report.md з агрегатами.

Запуск:
    python main.py
"""

import asyncio
import csv
import json
import logging
import os
import time
from typing import Any

from dotenv import load_dotenv

from classifier import classify_request
from sheets_logger import append_results
from telegram_notifier import notify_results

# .env читається до першого виклику API: classifier бере GEMINI_API_KEY
# з середовища лише всередині _get_client(), а не під час імпорту.
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

INPUT_FILE = "input_requests.csv"
OUTPUT_JSON = "output.json"
REPORT_FILE = "report.md"
REQUIRED_COLUMNS = ["id", "channel", "timestamp", "raw_text"]
DELAY_BETWEEN_REQUESTS = 1.5  # секунди; бережемо ліміт безкоштовного тіру (RPM)
DEFAULT_MAX_CONCURRENCY = 3  # скільки запитів до Gemini виконується одночасно


class InputValidationError(Exception):
    """Піднімається, коли вхідний CSV не відповідає очікуваній структурі."""


def load_requests(filepath: str) -> list[dict[str, str]]:
    """Читає та валідує вхідний CSV-файл із запитами.

    Перевіряє наявність усіх обов'язкових колонок, пропускає рядки з
    порожнім id/raw_text та дублікатами id (з попередженням у логах),
    замість того щоб впасти з незрозумілим KeyError десь посередині
    обробки.

    Args:
        filepath: Шлях до вхідного CSV-файлу.

    Returns:
        Список валідних рядків (кожен - словник колонка->значення).

    Raises:
        InputValidationError: якщо відсутні обов'язкові колонки, файл
            порожній, або після валідації не залишилось жодного
            коректного запису.
    """
    with open(filepath, encoding="utf-8") as f:
        reader = csv.DictReader(f)

        actual_columns = set(reader.fieldnames or [])
        missing = [col for col in REQUIRED_COLUMNS if col not in actual_columns]
        if missing:
            raise InputValidationError(
                f"У файлі '{filepath}' відсутні обов'язкові колонки: {missing}.\n"
                f"Знайдені колонки: {sorted(actual_columns)}\n"
                f"Очікувані колонки: {REQUIRED_COLUMNS}"
            )

        rows = list(reader)

        if not rows:
            raise InputValidationError(f"Файл '{filepath}' не містить жодного запису.")

        seen_ids = set()
        valid_rows: list[dict[str, str]] = []
        for i, row in enumerate(rows, start=2):
            req_id = (row.get("id") or "").strip()
            raw_text = (row.get("raw_text") or "").strip()

            if not req_id:
                logger.warning(f"Рядок {i}: порожній id, запис пропущено.")
                continue
            if req_id in seen_ids:
                logger.warning(f"Рядок {i}: дублікат id '{req_id}', запис пропущено.")
                continue
            if not raw_text:
                logger.warning(f"Рядок {i} ({req_id}): порожній raw_text, запис пропущено.")
                continue

            seen_ids.add(req_id)
            valid_rows.append(row)

        if not valid_rows:
            raise InputValidationError(
                f"Файл '{filepath}' не містить жодного коректного запису після валідації (перевір id та raw_text)."
            )

        return valid_rows


def process_all(requests: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Послідовно класифікує всі запити зі списку.

    Залишена як простий синхронний варіант; основний шлях виконання
    (main() та AWS Lambda) використовує process_all_async().

    Якщо посеред обробки станеться критична помилка (наприклад,
    RuntimeError через невалідний API-ключ - усі наступні запити все
    одно провалились б так само), уже оброблені результати НЕ
    губляться: вони зберігаються у output.json перед тим, як помилка
    прокидається далі. Це виправляє реальну прогалину, знайдену під
    час код-рев'ю: раніше часткові результати втрачались повністю.

    Args:
        requests: Список валідних рядків із load_requests().

    Returns:
        Список результатів (словників) - кожен доповнений оригінальними
        channel/timestamp з вхідного CSV.

    Raises:
        RuntimeError: прокидається далі після збереження часткових
            результатів, якщо classify_request() підняв критичну
            помилку авторизації (retry для неї безглуздий).
    """
    results: list[dict[str, Any]] = []
    total = len(requests)

    for i, row in enumerate(requests, start=1):
        req_id = row["id"]
        raw_text = row["raw_text"]
        channel = row["channel"]

        logger.info(f"[{i}/{total}] Обробка {req_id}...")

        try:
            analysis = classify_request(req_id, raw_text, channel)
        except RuntimeError as e:
            logger.error(
                f"Критична помилка на запиті {req_id} ({i}/{total}): {e}\n"
                f"Зберігаю {len(results)} уже оброблених результатів перед зупинкою."
            )
            if results:
                save_output(results, OUTPUT_JSON)
                build_report(results, REPORT_FILE)
            raise

        result = analysis.model_dump()
        result["channel"] = channel
        result["timestamp"] = row["timestamp"]
        results.append(result)

        if i < total:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    return results


def get_max_concurrency() -> int:
    """Повертає ліміт одночасних запитів до LLM.

    Береться зі змінної середовища MAX_CONCURRENCY (зручно задавати в
    Lambda -> Configuration -> Environment variables). Якщо змінна не
    задана або некоректна - використовується DEFAULT_MAX_CONCURRENCY.

    Returns:
        Ціле число не менше 1.
    """
    raw = os.environ.get("MAX_CONCURRENCY", "")
    try:
        return max(1, int(raw))
    except ValueError:
        if raw:
            logger.warning(f"MAX_CONCURRENCY='{raw}' некоректне, використовую {DEFAULT_MAX_CONCURRENCY}.")
        return DEFAULT_MAX_CONCURRENCY


def get_max_requests_per_minute() -> int | None:
    """Повертає ліміт стартів запитів на хвилину (RPM) або None, якщо
    обмеження не задано.

    Береться зі змінної середовища MAX_REQUESTS_PER_MINUTE. Для
    безкоштовного тіру Gemini (~15 RPM) безпечне значення - 12. Для
    платного тарифу змінну можна не задавати.

    Returns:
        Додатне ціле число або None (без обмеження).
    """
    raw = os.environ.get("MAX_REQUESTS_PER_MINUTE", "")
    try:
        value = int(raw)
    except ValueError:
        if raw:
            logger.warning(f"MAX_REQUESTS_PER_MINUTE='{raw}' некоректне, обмеження вимкнено.")
        return None
    return value if value > 0 else None


class RateLimiter:
    """Рівномірно розподіляє старти запитів у часі (не більше N на хвилину).

    Кожен виклик wait() резервує найближчий вільний слот: слоти йдуть із
    інтервалом 60/N секунд. Резервування відбувається синхронно (без await
    всередині), тому в asyncio воно атомарне й не потребує Lock.
    """

    def __init__(self, requests_per_minute: int | None) -> None:
        self._interval = 60.0 / requests_per_minute if requests_per_minute else 0.0
        self._next_slot = 0.0

    async def wait(self) -> None:
        """Чекає свого слота; якщо ліміт не задано - повертається одразу."""
        if self._interval <= 0:
            return
        now = time.monotonic()
        start = max(now, self._next_slot)
        self._next_slot = start + self._interval
        if start > now:
            await asyncio.sleep(start - now)


async def process_all_async(
    requests: list[dict[str, str]],
    max_concurrency: int | None = None,
    requests_per_minute: int | None = None,
) -> list[dict[str, Any]]:
    """Паралельно класифікує всі запити (asyncio.gather + Semaphore).

    Одночасно виконується не більше max_concurrency запитів: Semaphore
    не дає впертись у rate limit безкоштовного тіру Gemini. Блокуюча
    classify_request() (з усім retry/fallback-логіком) виконується в
    окремих потоках через asyncio.to_thread, тому event loop не блокується.

    Порядок результатів збігається з порядком вхідних запитів, незалежно
    від того, у якому порядку вони завершились.

    Поведінка при критичній помилці така сама, як у process_all():
    якщо classify_request() піднімає RuntimeError (невалідний API-ключ),
    нові запити більше не стартують, уже оброблені результати
    зберігаються в output.json/report.md, а помилка прокидається далі.

    Args:
        requests: Список валідних рядків із load_requests().
        max_concurrency: Ліміт одночасних запитів. Якщо None - береться
            з get_max_concurrency().
        requests_per_minute: Ліміт стартів запитів на хвилину. Якщо None -
            береться з get_max_requests_per_minute(); 0 вимикає обмеження.

    Returns:
        Список результатів у порядку вхідних запитів.

    Raises:
        RuntimeError: після збереження часткових результатів.
    """
    limit = max_concurrency if max_concurrency is not None else get_max_concurrency()
    semaphore = asyncio.Semaphore(max(1, limit))
    rpm = requests_per_minute if requests_per_minute is not None else get_max_requests_per_minute()
    limiter = RateLimiter(rpm)
    abort = asyncio.Event()
    total = len(requests)
    slots: list[dict[str, Any] | None] = [None] * total
    first_error: RuntimeError | None = None

    rpm_info = f", не більше {rpm} стартів/хв" if rpm else ""
    logger.info(f"Паралельна обробка: {total} запитів, одночасно не більше {limit}{rpm_info}.")

    async def worker(index: int, row: dict[str, str]) -> None:
        nonlocal first_error
        async with semaphore:
            if abort.is_set():
                return

            req_id = row["id"]
            channel = row["channel"]

            await limiter.wait()
            if abort.is_set():
                return
            logger.info(f"[{index + 1}/{total}] Обробка {req_id}...")

            try:
                analysis = await asyncio.to_thread(classify_request, req_id, row["raw_text"], channel)
            except RuntimeError as e:
                if first_error is None:
                    first_error = e
                    logger.error(f"Критична помилка на запиті {req_id} ({index + 1}/{total}): {e}")
                abort.set()
                return

            result = analysis.model_dump()
            result["channel"] = channel
            result["timestamp"] = row["timestamp"]
            slots[index] = result

            if index < total - 1:
                await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

    await asyncio.gather(*(worker(i, row) for i, row in enumerate(requests)))

    results = [r for r in slots if r is not None]

    if first_error is not None:
        logger.error(f"Зберігаю {len(results)} уже оброблених результатів перед зупинкою.")
        if results:
            save_output(results, OUTPUT_JSON)
            build_report(results, REPORT_FILE)
        raise first_error

    return results


def save_output(results: list[dict[str, Any]], filepath: str) -> None:
    """Зберігає повний структурований результат у JSON-файл.

    Args:
        results: Список результатів класифікації.
        filepath: Шлях до вихідного JSON-файлу.
    """
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    logger.info(f"Збережено {filepath}")


def build_report(results: list[dict[str, Any]], filepath: str) -> None:
    """Формує короткий Markdown-звіт з агрегатами по категоріях,
    пріоритету, відділах, та списками запитів, що потребують уточнення
    чи не вдалось обробити.

    Args:
        results: Список результатів класифікації.
        filepath: Шлях до вихідного .md файлу.
    """
    total = len(results)
    failed = [r for r in results if r["processing_status"] == "failed"]
    needs_clarification = [r for r in results if r["needs_clarification"]]

    by_category: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    by_department: dict[str, int] = {}

    for r in results:
        by_category[r["category"]] = by_category.get(r["category"], 0) + 1
        by_priority[r["priority"]] = by_priority.get(r["priority"], 0) + 1
        dept = r["target_department"] or "не визначено"
        by_department[dept] = by_department.get(dept, 0) + 1

    lines = ["# Звіт по обробці запитів", ""]
    lines.append(f"**Всього запитів:** {total}")
    lines.append(f"**Не вдалося обробити коректно:** {len(failed)}")
    lines.append("")

    lines.append("## По категоріях")
    lines.append("")
    lines.append("| Категорія | Кількість |")
    lines.append("|---|---|")
    for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
        lines.append(f"| {cat} | {count} |")
    lines.append("")

    lines.append("## По пріоритету")
    lines.append("")
    lines.append("| Пріоритет | Кількість |")
    lines.append("|---|---|")
    for pr in ["high", "medium", "low"]:
        lines.append(f"| {pr} | {by_priority.get(pr, 0)} |")
    lines.append("")

    lines.append("## По відділах")
    lines.append("")
    lines.append("| Відділ | Кількість |")
    lines.append("|---|---|")
    for dept, count in sorted(by_department.items(), key=lambda x: -x[1]):
        lines.append(f"| {dept} | {count} |")
    lines.append("")

    lines.append(f"## Потребують уточнення ({len(needs_clarification)})")
    lines.append("")
    if needs_clarification:
        for r in needs_clarification:
            lines.append(f"- **{r['id']}**: {r['short_summary']}")
    else:
        lines.append("Немає.")
    lines.append("")

    if failed:
        lines.append(f"## Не вдалося обробити ({len(failed)})")
        lines.append("")
        for r in failed:
            lines.append(f"- **{r['id']}**: {r['short_summary']}")
        lines.append("")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"Збережено {filepath}")


def main() -> None:
    """Точка входу: читає CSV, класифікує всі запити, зберігає
    output.json та report.md."""
    logger.info(f"Читаю {INPUT_FILE}...")
    try:
        requests = load_requests(INPUT_FILE)
    except InputValidationError as e:
        logger.error(f"Помилка валідації вхідних даних:\n{e}")
        return

    logger.info(f"Знайдено {len(requests)} запитів.")

    try:
        results = asyncio.run(process_all_async(requests))
    except RuntimeError as e:
        logger.error(
            f"Обробку зупинено через критичну помилку: {e}\n"
            f"Часткові результати вже збережено в {OUTPUT_JSON} та {REPORT_FILE} "
            f"(якщо встигли обробити хоча б один запит)."
        )
        return

    save_output(results, OUTPUT_JSON)
    build_report(results, REPORT_FILE)
    append_results(results, INPUT_FILE)
    notify_results(results, INPUT_FILE)

    logger.info("Готово!")


if __name__ == "__main__":
    main()
