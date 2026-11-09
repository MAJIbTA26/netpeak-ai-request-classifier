"""
Збереження результатів класифікації в PostgreSQL та SQL-статистика для звіту.

Модуль необов'язковий (як sheets_logger і telegram_notifier): вмикається
змінною середовища DATABASE_URL. Якщо вона не задана - нічого не відбувається.
Помилки бази не валять основний конвеєр: вони логуються, а результати
все одно лишаються в output.json.

Приклад DATABASE_URL (локально, база з docker-compose.yml):

    postgresql://classifier:ПАРОЛЬ@localhost:5432/classifier

Схема (дві таблиці, зв'язок "один до багатьох"):

    runs(id, source_file, started_at, total, failed)
      |
      +--< results(id, run_id -> runs.id, request_id, ...)

Один запуск обробки файлу = один рядок у runs і N рядків у results.

Відмінності від попередньої SQLite-версії:
    - плейсхолдери %s замість ?;
    - id: GENERATED ALWAYS AS IDENTITY, а новий id повертає INSERT ... RETURNING;
    - справжні типи: BOOLEAN, TIMESTAMPTZ, JSONB замість INTEGER 0/1 і TEXT;
    - підрахунок помилок: COUNT(*) FILTER (WHERE ...), бо сума булевих у PostgreSQL неможлива.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_file  TEXT        NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    total        INTEGER     NOT NULL,
    failed       INTEGER     NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS results (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id              BIGINT  NOT NULL REFERENCES runs (id),
    request_id          TEXT    NOT NULL,
    channel             TEXT,
    request_timestamp   TEXT,
    category            TEXT    NOT NULL,
    target_department   TEXT    NOT NULL DEFAULT '',
    priority            TEXT    NOT NULL
                        CHECK (priority IN ('low', 'medium', 'high')),
    short_summary       TEXT    NOT NULL,
    requested_actions   JSONB   NOT NULL DEFAULT '[]'::jsonb,
    needs_clarification BOOLEAN NOT NULL DEFAULT FALSE,
    processing_status   TEXT    NOT NULL
                        CHECK (processing_status IN ('ok', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_results_run_id   ON results (run_id);
CREATE INDEX IF NOT EXISTS idx_results_category ON results (category);
"""

INSERT_RUN = """
INSERT INTO runs (source_file, started_at, total, failed)
VALUES (%s, %s, %s, %s)
RETURNING id
"""

INSERT_RESULT = """
INSERT INTO results (
    run_id, request_id, channel, request_timestamp, category,
    target_department, priority, short_summary, requested_actions,
    needs_clarification, processing_status
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

# --- Запити для статистики (DQL) --------------------------------------------

# GROUP BY + COUNT: скільки запитів у кожній категорії в цьому запуску.
STATS_BY_CATEGORY = """
SELECT category, COUNT(*) AS cnt
FROM results
WHERE run_id = %s
GROUP BY category
ORDER BY cnt DESC, category
"""

# WHERE + GROUP BY + CASE: запити з пріоритетом high по відділах.
STATS_HIGH_BY_DEPARTMENT = """
SELECT
    CASE WHEN target_department = '' THEN 'не визначено' ELSE target_department END AS department,
    COUNT(*) AS cnt
FROM results
WHERE run_id = %s AND priority = 'high'
GROUP BY department
ORDER BY cnt DESC, department
"""

# LEFT JOIN + GROUP BY: останні запуски та скільки в кожному запитів / помилок.
# LEFT JOIN не губить запуск, навіть якщо до нього немає жодного результату.
# COUNT(*) FILTER (WHERE ...) - умовний підрахунок (аналог SUM(умова) у SQLite).
STATS_RECENT_RUNS = """
SELECT
    r.id,
    r.source_file,
    r.started_at,
    COUNT(x.id) AS total,
    COUNT(x.id) FILTER (WHERE x.processing_status = 'failed') AS failed
FROM runs AS r
LEFT JOIN results AS x ON x.run_id = r.id
GROUP BY r.id, r.source_file, r.started_at
ORDER BY r.id DESC
LIMIT %s
"""


def get_database_url(db_url: str | None = None) -> str | None:
    """Повертає рядок підключення: переданий явно або зі змінної DATABASE_URL."""
    return db_url or os.getenv("DATABASE_URL") or None


def init_db(conn: psycopg.Connection) -> None:
    """Створює таблиці та індекси, якщо їх ще немає (DDL)."""
    conn.execute(SCHEMA)


def _row_values(run_id: int, result: dict[str, Any]) -> tuple:
    """Перетворює словник результату на кортеж значень для INSERT."""
    return (
        run_id,
        result["id"],
        result.get("channel"),
        result.get("timestamp"),
        result["category"],
        result.get("target_department") or "",
        result["priority"],
        result["short_summary"],
        Jsonb(result.get("requested_actions") or []),
        bool(result.get("needs_clarification")),
        result.get("processing_status", "ok"),
    )


def save_results(
    results: list[dict[str, Any]],
    source_file: str,
    db_url: str | None = None,
) -> int | None:
    """Зберігає один запуск обробки в базу.

    Args:
        results: Список результатів із process_all_async() (словники).
        source_file: Назва вхідного файлу (для таблиці runs).
        db_url: Рядок підключення до PostgreSQL. Якщо не задано - береться зі
            змінної середовища DATABASE_URL. Якщо її теж немає - модуль вимкнено.

    Returns:
        id створеного запуску (runs.id), або None, якщо базу вимкнено,
        результатів немає або сталась помилка.
    """
    db_url = get_database_url(db_url)
    if not db_url:
        logger.info("DATABASE_URL не задано, збереження в базу пропущено.")
        return None
    if not results:
        logger.info("Немає результатів для збереження в базу.")
        return None

    failed = sum(1 for r in results if r.get("processing_status") == "failed")
    started_at = datetime.now(timezone.utc)

    try:
        # Блок with: COMMIT при успіху, ROLLBACK при будь-якому винятку, потім закриття.
        # Тож або записуємо весь запуск, або нічого.
        with psycopg.connect(db_url, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
            init_db(conn)
            run_id = conn.execute(INSERT_RUN, (source_file, started_at, len(results), failed)).fetchone()[0]
            with conn.cursor() as cur:
                cur.executemany(INSERT_RESULT, [_row_values(run_id, r) for r in results])
    except (psycopg.Error, KeyError) as e:
        logger.error(f"Не вдалося зберегти результати в базу: {e}")
        return None

    logger.info(f"Збережено в базу: запуск #{run_id}, {len(results)} рядків.")
    return run_id


def _md_cell(value: Any) -> str:
    """Готує значення до клітинки Markdown-таблиці (символ | екранується)."""
    return str(value).replace("|", "\\|")


def _md_table(header: list[str], rows: list[tuple]) -> list[str]:
    """Формує рядки Markdown-таблиці із заголовка та рядків даних."""
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(_md_cell(v) for v in row) + " |" for row in rows)
    return lines


def build_stats_section(run_id: int, db_url: str | None = None, history_limit: int = 5) -> str | None:
    """Формує Markdown-розділ зі статистикою, порахованою SQL-запитами.

    Args:
        run_id: id запуску (runs.id), для якого рахується статистика.
        db_url: Рядок підключення (за замовчуванням - змінна DATABASE_URL).
        history_limit: Скільки останніх запусків показати в історії.

    Returns:
        Текст розділу або None, якщо базу вимкнено чи сталась помилка.
    """
    db_url = get_database_url(db_url)
    if not db_url:
        return None

    try:
        with psycopg.connect(db_url, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
            by_category = conn.execute(STATS_BY_CATEGORY, (run_id,)).fetchall()
            high_by_department = conn.execute(STATS_HIGH_BY_DEPARTMENT, (run_id,)).fetchall()
            recent_runs = conn.execute(STATS_RECENT_RUNS, (history_limit,)).fetchall()
    except psycopg.Error as e:
        logger.error(f"Не вдалося порахувати SQL-статистику: {e}")
        return None

    lines = ["## Статистика з бази (SQL)", "", f"Запуск #{run_id}.", ""]

    lines += ["### По категоріях (цей запуск)", ""]
    lines += _md_table(["Категорія", "Кількість"], by_category)
    lines.append("")

    lines += ["### Пріоритет high по відділах (цей запуск)", ""]
    if high_by_department:
        lines += _md_table(["Відділ", "Кількість"], high_by_department)
    else:
        lines.append("Немає.")
    lines.append("")

    history = []
    for rid, source_file, started_at, total, failed in recent_runs:
        pct = f"{100 * failed / total:.1f}%" if total else "-"
        history.append((f"#{rid}", source_file, started_at.strftime("%Y-%m-%d %H:%M:%S"), total, failed, pct))
    lines += [f"### Останні запуски (до {history_limit})", ""]
    lines += _md_table(["Запуск", "Файл", "Час (UTC)", "Запитів", "Не вдалося", "Частка помилок"], history)

    return "\n".join(lines)


def append_stats_to_report(report_path: str, run_id: int, db_url: str | None = None) -> bool:
    """Дописує SQL-статистику в кінець Markdown-звіту.

    Ніколи не піднімає виняток: помилка лише логується, а звіт лишається як був.

    Args:
        report_path: Шлях до report.md.
        run_id: id запуску, який щойно збережено (значення, що повернула save_results).
        db_url: Рядок підключення (за замовчуванням - змінна DATABASE_URL).

    Returns:
        True, якщо розділ дописано; False в інших випадках.
    """
    section = build_stats_section(run_id, db_url)
    if section is None:
        return False

    try:
        with open(report_path, "a", encoding="utf-8") as f:
            f.write("\n\n" + section + "\n")
    except OSError as e:
        logger.error(f"Не вдалося дописати статистику в {report_path}: {e}")
        return False

    logger.info(f"SQL-статистику дописано в {report_path}")
    return True
