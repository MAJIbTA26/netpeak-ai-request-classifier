"""Тести для db.py (PostgreSQL).

Фікстура db_url (тимчасова схема) лежить у conftest.py. Без TEST_DATABASE_URL тести пропускаються.
"""

import psycopg
import pytest

from db import (
    DatabaseUnavailableError,
    append_stats_to_report,
    build_stats_section,
    get_override_stats,
    get_result,
    init_db,
    save_api_result,
    save_results,
    set_priority_override,
)


def make_result(req_id: str = "REQ-001", **overrides) -> dict:
    result = {
        "id": req_id,
        "category": "баг/підтримка",
        "target_department": "Фінанси",
        "priority": "high",
        "short_summary": "Не працює вивантаження звіту.",
        "requested_actions": ["перевірити експорт", "виправити помилку"],
        "needs_clarification": False,
        "processing_status": "ok",
        "channel": "email",
        "timestamp": "2026-01-01 10:00:00",
    }
    result.update(overrides)
    return result


def fetch_all(db_url: str, sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(db_url) as conn:
        return conn.execute(sql, params).fetchall()


def test_save_results_creates_run_and_rows(db_url):
    results = [make_result("REQ-001"), make_result("REQ-002", priority="low")]

    run_id = save_results(results, "input_requests.csv", db_url=db_url)

    assert run_id == 1
    assert fetch_all(db_url, "SELECT source_file, total, failed FROM runs") == [("input_requests.csv", 2, 0)]
    assert fetch_all(db_url, "SELECT COUNT(*) FROM results WHERE run_id = %s", (run_id,)) == [(2,)]


def test_failed_rows_are_counted_and_kept(db_url):
    results = [
        make_result("REQ-001"),
        make_result("REQ-002", processing_status="failed", short_summary="не вдалося"),
    ]

    save_results(results, "in.csv", db_url=db_url)

    assert fetch_all(db_url, "SELECT failed FROM runs") == [(1,)]
    rows = fetch_all(db_url, "SELECT request_id FROM results WHERE processing_status = 'failed'")
    assert rows == [("REQ-002",)]


def test_requested_actions_stored_as_jsonb(db_url):
    save_results([make_result()], "in.csv", db_url=db_url)

    (stored,) = fetch_all(db_url, "SELECT requested_actions FROM results")[0]
    assert stored == ["перевірити експорт", "виправити помилку"]  # psycopg сам перетворює JSONB у list


def test_jsonb_query_operators_work(db_url):
    save_results([make_result("A"), make_result("B", requested_actions=[])], "in.csv", db_url=db_url)

    rows = fetch_all(db_url, "SELECT request_id FROM results WHERE jsonb_array_length(requested_actions) > 0")
    assert rows == [("A",)]


def test_boolean_and_timestamp_types(db_url):
    save_results([make_result(needs_clarification=True)], "in.csv", db_url=db_url)

    (flag,) = fetch_all(db_url, "SELECT needs_clarification FROM results")[0]
    (started,) = fetch_all(db_url, "SELECT started_at FROM runs")[0]
    assert flag is True
    assert started.tzinfo is not None


def test_empty_department_and_missing_actions_use_defaults(db_url):
    result = make_result(target_department="", requested_actions=None)

    save_results([result], "in.csv", db_url=db_url)

    (department, actions) = fetch_all(db_url, "SELECT target_department, requested_actions FROM results")[0]
    assert department == ""
    assert actions == []


def test_two_runs_create_two_run_rows(db_url):
    first = save_results([make_result()], "a.csv", db_url=db_url)
    second = save_results([make_result()], "b.csv", db_url=db_url)

    assert (first, second) == (1, 2)
    assert fetch_all(db_url, "SELECT COUNT(*) FROM runs") == [(2,)]
    assert fetch_all(db_url, "SELECT COUNT(*) FROM results") == [(2,)]


def test_disabled_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert save_results([make_result()], "in.csv") is None


def test_uses_database_url_from_env(monkeypatch, db_url):
    monkeypatch.setenv("DATABASE_URL", db_url)

    assert save_results([make_result()], "in.csv") == 1
    assert fetch_all(db_url, "SELECT COUNT(*) FROM results") == [(1,)]


def test_empty_results_returns_none(db_url):
    assert save_results([], "in.csv", db_url=db_url) is None


def test_db_error_does_not_raise():
    # порт 1: нічого не слухає, підключення одразу відхиляється
    assert save_results([make_result()], "in.csv", db_url="postgresql://x:y@127.0.0.1:1/none") is None


def test_malformed_result_rolls_back_whole_run(db_url):
    # Перший запуск успішний: він створює таблиці й лишається в базі.
    assert save_results([make_result("OK-1")], "good.csv", db_url=db_url) == 1

    broken = make_result("REQ-002")
    del broken["category"]  # обов'язкове поле відсутнє -> KeyError

    assert save_results([make_result("REQ-001"), broken], "bad.csv", db_url=db_url) is None
    # Зламаний запуск не лишив нічого: ні рядка в runs, ні жодного з його результатів
    assert fetch_all(db_url, "SELECT source_file FROM runs") == [("good.csv",)]
    assert fetch_all(db_url, "SELECT request_id FROM results") == [("OK-1",)]


def test_foreign_key_is_enforced(db_url):
    with psycopg.connect(db_url) as conn:
        init_db(conn)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO results (run_id, request_id, category, priority, short_summary, processing_status) "
                "VALUES (999, 'X', 'інтеграція', 'low', 's', 'ok')"
            )


def test_check_constraint_rejects_bad_priority(db_url):
    with psycopg.connect(db_url) as conn:
        init_db(conn)
        conn.execute("INSERT INTO runs (source_file, started_at, total) VALUES ('f', now(), 1)")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO results (run_id, request_id, category, priority, short_summary, processing_status) "
                "VALUES (1, 'X', 'інтеграція', 'urgent', 's', 'ok')"
            )


# --- SQL-статистика для звіту ------------------------------------------------


def test_stats_section_counts_categories_for_run(db_url):
    results = [
        make_result("A", category="баг/підтримка"),
        make_result("B", category="баг/підтримка"),
        make_result("C", category="інтеграція"),
    ]
    run_id = save_results(results, "in.csv", db_url=db_url)

    section = build_stats_section(run_id, db_url=db_url)

    assert section is not None
    assert "## Статистика з бази (SQL)" in section
    assert "Запуск #1." in section
    assert "| баг/підтримка | 2 |" in section
    assert "| інтеграція | 1 |" in section
    # найчастіша категорія йде першою
    assert section.index("| баг/підтримка | 2 |") < section.index("| інтеграція | 1 |")


def test_stats_high_priority_by_department_with_unknown(db_url):
    results = [
        make_result("A", priority="high", target_department="Фінанси"),
        make_result("B", priority="high", target_department=""),
        make_result("C", priority="low", target_department="Фінанси"),
    ]
    run_id = save_results(results, "in.csv", db_url=db_url)

    section = build_stats_section(run_id, db_url=db_url)

    assert "| Фінанси | 1 |" in section  # low-запит не рахується
    assert "| не визначено | 1 |" in section


def test_stats_recent_runs_show_failed_percent_newest_first(db_url):
    save_results([make_result("A")], "first.csv", db_url=db_url)
    second = save_results(
        [make_result("B"), make_result("C", processing_status="failed")],
        "second.csv",
        db_url=db_url,
    )

    section = build_stats_section(second, db_url=db_url)

    assert "| #2 | second.csv |" in section
    assert "| 2 | 1 | 50.0% |" in section
    assert "| 1 | 0 | 0.0% |" in section
    assert section.index("second.csv") < section.index("first.csv")


def test_stats_recent_runs_keeps_run_without_results(db_url):
    with psycopg.connect(db_url) as conn:
        init_db(conn)
        conn.execute("INSERT INTO runs (source_file, started_at, total) VALUES ('empty.csv', now(), 0)")

    section = build_stats_section(1, db_url=db_url)

    assert "| #1 | empty.csv |" in section  # LEFT JOIN не губить запуск без результатів
    assert "| 0 | 0 | - |" in section


def test_stats_section_none_without_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert build_stats_section(1) is None
    assert build_stats_section(1, db_url="postgresql://x:y@127.0.0.1:1/none") is None


def test_append_stats_to_report_keeps_original_text(tmp_path, db_url):
    run_id = save_results([make_result()], "in.csv", db_url=db_url)
    report = tmp_path / "report.md"
    report.write_text("# Звіт по обробці запитів", encoding="utf-8")

    assert append_stats_to_report(str(report), run_id, db_url=db_url) is True

    text = report.read_text(encoding="utf-8")
    assert text.startswith("# Звіт по обробці запитів")
    assert "## Статистика з бази (SQL)" in text


def test_append_stats_to_report_false_when_disabled_or_unwritable(monkeypatch, tmp_path, db_url):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    report = tmp_path / "report.md"
    report.write_text("# Звіт", encoding="utf-8")
    assert append_stats_to_report(str(report), 1) is False
    assert report.read_text(encoding="utf-8") == "# Звіт"

    run_id = save_results([make_result()], "in.csv", db_url=db_url)
    bad_report = str(tmp_path / "no_such_dir" / "report.md")
    assert append_stats_to_report(bad_report, run_id, db_url=db_url) is False


# --- Заявки з API та зміна пріоритету людиною ----------------------------------


def test_save_api_result_returns_record_id_and_creates_api_run(db_url):
    record_id = save_api_result(make_result("API-1"), db_url=db_url)

    assert record_id == 1
    assert fetch_all(db_url, "SELECT source_file, total FROM runs") == [("api", 1)]
    assert fetch_all(db_url, "SELECT request_id FROM results WHERE id = %s", (record_id,)) == [("API-1",)]


def test_save_api_result_none_without_db_or_on_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert save_api_result(make_result()) is None
    assert save_api_result(make_result(), db_url="postgresql://x:y@127.0.0.1:1/none") is None


def test_get_result_shows_effective_priority(db_url):
    record_id = save_api_result(make_result(priority="medium"), db_url=db_url)

    record = get_result(record_id, db_url=db_url)

    assert record["priority"] == "medium"
    assert record["priority_override"] is None
    assert record["effective_priority"] == "medium"
    assert record["requested_actions"] == ["перевірити експорт", "виправити помилку"]


def test_get_result_missing_returns_none(db_url):
    with psycopg.connect(db_url) as conn:
        init_db(conn)
    assert get_result(999, db_url=db_url) is None


def test_set_priority_override_keeps_ai_value_and_records_audit(db_url):
    record_id = save_api_result(make_result(priority="low"), db_url=db_url)

    record = set_priority_override(record_id, "high", "manager", db_url=db_url)

    assert record["priority"] == "low"  # оригінал від AI не перезаписано
    assert record["priority_override"] == "high"
    assert record["effective_priority"] == "high"
    assert record["overridden_by"] == "manager"
    assert record["overridden_at"] is not None


def test_set_priority_override_unknown_id_returns_none(db_url):
    with psycopg.connect(db_url) as conn:
        init_db(conn)
    assert set_priority_override(999, "high", "manager", db_url=db_url) is None


def test_set_priority_override_rejects_bad_priority_at_db_level(db_url):
    record_id = save_api_result(make_result(), db_url=db_url)

    with pytest.raises(DatabaseUnavailableError):
        set_priority_override(record_id, "urgent", "manager", db_url=db_url)


def test_get_and_set_raise_without_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(DatabaseUnavailableError):
        get_result(1)
    with pytest.raises(DatabaseUnavailableError):
        set_priority_override(1, "high", "manager")
    with pytest.raises(DatabaseUnavailableError):
        get_override_stats()


def test_override_stats_counts_reviewed_and_changed(db_url):
    a = save_api_result(make_result("A", priority="low"), db_url=db_url)
    b = save_api_result(make_result("B", priority="high"), db_url=db_url)
    save_api_result(make_result("C", priority="medium"), db_url=db_url)  # не переглядали

    set_priority_override(a, "high", "manager", db_url=db_url)  # змінив
    set_priority_override(b, "high", "manager", db_url=db_url)  # підтвердив

    stats = get_override_stats(db_url=db_url)

    assert stats == {"total": 3, "reviewed": 2, "changed": 1, "disagreement_rate": 0.5}


def test_override_stats_rate_is_none_without_reviews(db_url):
    save_api_result(make_result(), db_url=db_url)

    stats = get_override_stats(db_url=db_url)

    assert stats["reviewed"] == 0
    assert stats["disagreement_rate"] is None


def test_stats_high_priority_uses_manager_decision(db_url):
    results = [
        make_result("A", priority="low", target_department="Фінанси"),
        make_result("B", priority="high", target_department="HR"),
    ]
    run_id = save_results(results, "in.csv", db_url=db_url)
    with psycopg.connect(db_url) as conn:
        # керівник підняв A до high і опустив B до low
        conn.execute("UPDATE results SET priority_override = 'high' WHERE request_id = 'A'")
        conn.execute("UPDATE results SET priority_override = 'low' WHERE request_id = 'B'")

    section = build_stats_section(run_id, db_url=db_url)

    assert "| Фінанси | 1 |" in section
    assert "HR" not in section


def test_init_db_migrates_existing_table_without_override_columns(db_url):
    with psycopg.connect(db_url) as conn:
        conn.execute("CREATE TABLE runs (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, source_file TEXT)")
        conn.execute(
            "CREATE TABLE results (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
            "run_id BIGINT, category TEXT, priority TEXT)"
        )
        init_db(conn)  # старі таблиці без нових колонок мають отримати їх

    columns = fetch_all(
        db_url,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'results'",
    )
    names = {c[0] for c in columns}
    assert {"priority_override", "overridden_by", "overridden_at"} <= names
