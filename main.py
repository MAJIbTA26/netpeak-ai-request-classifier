"""
Головний скрипт: читає input_requests.csv, класифікує кожен запит через
Gemini API, зберігає результат у output.json та формує короткий звіт
report.md з агрегатами.

Запуск:
    python main.py
"""

import csv
import json
import logging
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from classifier import classify_request

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
                f"Файл '{filepath}' не містить жодного коректного запису "
                f"після валідації (перевір id та raw_text)."
            )

        return valid_rows


def process_all(requests: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Послідовно класифікує всі запити зі списку.

    Args:
        requests: Список валідних рядків із load_requests().

    Returns:
        Список результатів (словників) - кожен доповнений оригінальними
        channel/timestamp з вхідного CSV.
    """
    results: list[dict[str, Any]] = []
    total = len(requests)

    for i, row in enumerate(requests, start=1):
        req_id = row["id"]
        raw_text = row["raw_text"]
        channel = row["channel"]

        logger.info(f"[{i}/{total}] Обробка {req_id}...")
        analysis = classify_request(req_id, raw_text, channel)

        result = analysis.model_dump()
        result["channel"] = channel
        result["timestamp"] = row["timestamp"]
        results.append(result)

        if i < total:
            time.sleep(DELAY_BETWEEN_REQUESTS)

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

    results = process_all(requests)

    save_output(results, OUTPUT_JSON)
    build_report(results, REPORT_FILE)

    logger.info("Готово!")


if __name__ == "__main__":
    main()
