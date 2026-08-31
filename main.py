"""
Головний скрипт: читає input_requests.csv, класифікує кожен запит через
Gemini API, зберігає результат у output.json та формує короткий звіт
report.md з агрегатами.

Запуск:
    python main.py
"""

import csv
import json
import time

from dotenv import load_dotenv

load_dotenv()  # обов'язково ДО імпорту classifier (там читається GEMINI_API_KEY)

from classifier import classify_request

INPUT_FILE = "input_requests.csv"
OUTPUT_JSON = "output.json"
REPORT_FILE = "report.md"
REQUIRED_COLUMNS = ["id", "channel", "timestamp", "raw_text"]


class InputValidationError(Exception):
    """Піднімається, коли вхідний CSV не відповідає очікуваній структурі."""
    pass

DELAY_BETWEEN_REQUESTS = 1.5  # секунди; бережемо ліміт безкоштовного тіру (RPM)


def load_requests(filepath):
    with open(filepath, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def process_all(requests):
    results = []
    total = len(requests)

    for i, row in enumerate(requests, start=1):
        req_id = row["id"]
        raw_text = row["raw_text"]
        channel = row["channel"]

        print(f"[{i}/{total}] Обробка {req_id}...")
        analysis = classify_request(req_id, raw_text, channel)

        result = analysis.model_dump()
        result["channel"] = channel
        result["timestamp"] = row["timestamp"]
        results.append(result)

        if i < total:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    return results


def save_output(results, filepath):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nЗбережено {filepath}")


def build_report(results, filepath):
    total = len(results)
    failed = [r for r in results if r["processing_status"] == "failed"]
    needs_clarification = [r for r in results if r["needs_clarification"]]

    by_category = {}
    by_priority = {}
    by_department = {}

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
    print(f"Збережено {filepath}")


def main():
    print(f"Читаю {INPUT_FILE}...")
    try:
        requests = load_requests(INPUT_FILE)
    except InputValidationError as e:
        print(f"\n❌ Помилка валідації вхідних даних:\n{e}")
        return

    print(f"Знайдено {len(requests)} запитів.\n")

    results = process_all(requests)

    save_output(results, OUTPUT_JSON)
    build_report(results, REPORT_FILE)

    print("\nГотово!")


if __name__ == "__main__":
    main()
