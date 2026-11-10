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
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5


class DatabaseUnavailableError(Exception):
    """База не налаштована або недоступна. API перетворює це на відповідь 503."""


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
                        CHECK (processing_status IN ('ok', 'failed')),
    priority_override   TEXT
                        CHECK (priority_override IN ('low', 'medium', 'high')),
    overridden_by       TEXT,
    overridden_at       TIMESTAMPTZ
);

-- Міграція: CREATE TABLE IF NOT EXISTS не змінює вже існуючу таблицю,
-- тому для баз, створених раніше, додаємо нові колонки окремо.
ALTER TABLE results ADD COLUMN IF NOT EXISTS priority_override TEXT
    CHECK (priority_override IN ('low', 'medium', 'high'));
ALTER TABLE results ADD COLUMN IF NOT EXISTS overridden_by TEXT;
ALTER TABLE results ADD COLUMN IF NOT EXISTS overridden_at TIMESTAMPTZ;

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

INSERT_RESULT_RETURNING = INSERT_RESULT.rstrip() + "\nRETURNING id"

SELECT_RESULT = """
SELECT
    id, run_id, request_id, channel, category, target_department,
    priority, priority_override,
    COALESCE(priority_override, priority) AS effective_priority,
    overridden_by, overridden_at, short_summary, requested_actions,
    needs_clarification, processing_status
FROM results
WHERE id = %s
"""

UPDATE_PRIORITY = """
UPDATE results
SET priority_override = %s, overridden_by = %s, overridden_at = now()
WHERE id = %s
"""

# Скільки заявок переглянув керівник і скільки разів не погодився з AI.
STATS_OVERRIDES = """
SELECT
    COUNT(*) AS total,
    COUNT(priority_override) AS reviewed,
    COUNT(*) FILTER (WHERE priority_override IS NOT NULL AND priority_override <> priority) AS changed
FROM results
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

# WHERE + GROUP BY + CASE: запити з фактичним пріоритетом high по відділах
# (якщо керівник змінив пріоритет, береться його значення).
STATS_HIGH_BY_DEPARTMENT = """
SELECT
    CASE WHEN target_department = '' THEN 'не визначено' ELSE target_department END AS department,
    COUNT(*) AS cnt
FROM results
WHERE run_id = %s AND COALESCE(priority_override, priority) = 'high'
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


def _require_db_url(db_url: str | None) -> str:
    """Рядок підключення або DatabaseUnavailableError, якщо базу не налаштовано."""
    url = get_database_url(db_url)
    if not url:
        raise DatabaseUnavailableError("DATABASE_URL не задано")
    return url


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


def save_api_result(result: dict[str, Any], source_file: str = "api", db_url: str | None = None) -> int | None:
    """Зберігає одну заявку, отриману через API, і повертає id рядка в results.

    Кожна заявка з API це окремий запуск (runs.total = 1) із джерелом "api".
    Ніколи не піднімає виняток: якщо бази немає чи сталась помилка, повертає None,
    а класифікація для користувача все одно працює.
    """
    db_url = get_database_url(db_url)
    if not db_url:
        return None

    failed = 1 if result.get("processing_status") == "failed" else 0
    try:
        with psycopg.connect(db_url, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
            init_db(conn)
            run_id = conn.execute(INSERT_RUN, (source_file, datetime.now(timezone.utc), 1, failed)).fetchone()[0]
            record_id = conn.execute(INSERT_RESULT_RETURNING, _row_values(run_id, result)).fetchone()[0]
    except (psycopg.Error, KeyError) as e:
        logger.error(f"Не вдалося зберегти заявку з API в базу: {e}")
        return None

    return record_id


def get_result(record_id: int, db_url: str | None = None) -> dict[str, Any] | None:
    """Повертає один запис із results (словник) або None, якщо такого id немає.

    Raises:
        DatabaseUnavailableError: базу не налаштовано або вона недоступна.
    """
    db_url = _require_db_url(db_url)
    try:
        with psycopg.connect(db_url, connect_timeout=CONNECT_TIMEOUT_SECONDS, row_factory=dict_row) as conn:
            return conn.execute(SELECT_RESULT, (record_id,)).fetchone()
    except psycopg.Error as e:
        logger.error(f"Не вдалося прочитати запис {record_id}: {e}")
        raise DatabaseUnavailableError("база недоступна") from e


def set_priority_override(
    record_id: int, priority: str, changed_by: str, db_url: str | None = None
) -> dict[str, Any] | None:
    """Записує пріоритет, який виставила людина, і повертає оновлений запис.

    Пріоритет від AI (колонка priority) не змінюється: це аудит. Хто й коли
    змінив, лишається в overridden_by та overridden_at.

    Returns:
        Оновлений запис або None, якщо запису з таким id немає.

    Raises:
        DatabaseUnavailableError: базу не налаштовано або вона недоступна.
    """
    db_url = _require_db_url(db_url)
    try:
        with psycopg.connect(db_url, connect_timeout=CONNECT_TIMEOUT_SECONDS, row_factory=dict_row) as conn:
            cursor = conn.execute(UPDATE_PRIORITY, (priority, changed_by, record_id))
            if cursor.rowcount == 0:
                return None
            return conn.execute(SELECT_RESULT, (record_id,)).fetchone()
    except psycopg.Error as e:
        logger.error(f"Не вдалося змінити пріоритет запису {record_id}: {e}")
        raise DatabaseUnavailableError("база недоступна") from e


def get_override_stats(db_url: str | None = None) -> dict[str, Any]:
    """Скільки заявок переглянув керівник і як часто не погодився з AI.

    Returns:
        {"total": усього заявок, "reviewed": переглянуто людиною,
         "changed": пріоритет змінено, "disagreement_rate": changed / reviewed або None}

    Raises:
        DatabaseUnavailableError: базу не налаштовано або вона недоступна.
    """
    db_url = _require_db_url(db_url)
    try:
        with psycopg.connect(db_url, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
            total, reviewed, changed = conn.execute(STATS_OVERRIDES).fetchone()
    except psycopg.Error as e:
        logger.error(f"Не вдалося порахувати статистику змін пріоритету: {e}")
        raise DatabaseUnavailableError("база недоступна") from e

    rate = round(changed / reviewed, 3) if reviewed else None
    return {"total": total, "reviewed": reviewed, "changed": changed, "disagreement_rate": rate}


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
