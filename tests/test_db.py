"""Тести для db.py. Використовують тимчасові SQLite-файли (tmp_path)."""

import json
import sqlite3

import pytest

from db import append_stats_to_report, build_stats_section, init_db, save_results


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


def fetch_all(db_path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_save_results_creates_run_and_rows(tmp_path):
    db = str(tmp_path / "test.db")
    results = [make_result("REQ-001"), make_result("REQ-002", priority="low")]

    run_id = save_results(results, "input_requests.csv", db_path=db)

    assert run_id == 1
    assert fetch_all(db, "SELECT source_file, total, failed FROM runs") == [("input_requests.csv", 2, 0)]
    assert fetch_all(db, "SELECT COUNT(*) FROM results WHERE run_id = ?", (run_id,)) == [(2,)]


def test_failed_rows_are_counted_and_kept(tmp_path):
    db = str(tmp_path / "test.db")
    results = [
        make_result("REQ-001"),
        make_result("REQ-002", processing_status="failed", short_summary="не вдалося"),
    ]

    save_results(results, "in.csv", db_path=db)

    assert fetch_all(db, "SELECT failed FROM runs") == [(1,)]
    rows = fetch_all(db, "SELECT request_id FROM results WHERE processing_status = 'failed'")
    assert rows == [("REQ-002",)]


def test_requested_actions_stored_as_json(tmp_path):
    db = str(tmp_path / "test.db")
    save_results([make_result()], "in.csv", db_path=db)

    (stored,) = fetch_all(db, "SELECT requested_actions FROM results")[0]
    assert json.loads(stored) == ["перевірити експорт", "виправити помилку"]


def test_empty_department_and_missing_actions_use_defaults(tmp_path):
    db = str(tmp_path / "test.db")
    result = make_result(target_department="", requested_actions=None)

    save_results([result], "in.csv", db_path=db)

    row = fetch_all(db, "SELECT target_department, requested_actions FROM results")[0]
    assert row == ("", "[]")


def test_two_runs_create_two_run_rows(tmp_path):
    db = str(tmp_path / "test.db")
    first = save_results([make_result()], "a.csv", db_path=db)
    second = save_results([make_result()], "b.csv", db_path=db)

    assert (first, second) == (1, 2)
    assert fetch_all(db, "SELECT COUNT(*) FROM runs") == [(2,)]
    assert fetch_all(db, "SELECT COUNT(*) FROM results") == [(2,)]


def test_disabled_without_db_path(monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    assert save_results([make_result()], "in.csv") is None


def test_uses_db_path_from_env(monkeypatch, tmp_path):
    db = str(tmp_path / "env.db")
    monkeypatch.setenv("DB_PATH", db)

    assert save_results([make_result()], "in.csv") == 1
    assert fetch_all(db, "SELECT COUNT(*) FROM results") == [(1,)]


def test_empty_results_returns_none(tmp_path):
    assert save_results([], "in.csv", db_path=str(tmp_path / "t.db")) is None


def test_db_error_does_not_raise(tmp_path):
    bad_path = str(tmp_path / "no_such_dir" / "test.db")
    assert save_results([make_result()], "in.csv", db_path=bad_path) is None


def test_malformed_result_rolls_back_whole_run(tmp_path):
    db = str(tmp_path / "test.db")
    broken = make_result("REQ-002")
    del broken["category"]  # обов'язкове поле відсутнє -> KeyError

    assert save_results([make_result("REQ-001"), broken], "in.csv", db_path=db) is None
    # Нічого не записано: ні запуск, ні рядки
    assert fetch_all(db, "SELECT COUNT(*) FROM runs") == [(0,)]
    assert fetch_all(db, "SELECT COUNT(*) FROM results") == [(0,)]


def test_foreign_key_is_enforced(tmp_path):
    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO results (run_id, request_id, category, priority, "
            "short_summary, processing_status) "
            "VALUES (999, 'X', 'інтеграція', 'low', 's', 'ok')"
        )
    conn.close()


def test_check_constraint_rejects_bad_priority(tmp_path):
    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    conn.execute("INSERT INTO runs (source_file, started_at, total) VALUES ('f', 't', 1)")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO results (run_id, request_id, category, priority, "
            "short_summary, processing_status) "
            "VALUES (1, 'X', 'інтеграція', 'urgent', 's', 'ok')"
        )
    conn.close()


# --- SQL-статистика для звіту ------------------------------------------------


def test_stats_section_counts_categories_for_run(tmp_path):
    db = str(tmp_path / "test.db")
    results = [
        make_result("A", category="баг/підтримка"),
        make_result("B", category="баг/підтримка"),
        make_result("C", category="інтеграція"),
    ]
    run_id = save_results(results, "in.csv", db_path=db)

    section = build_stats_section(run_id, db_path=db)

    assert section is not None
    assert "## Статистика з бази (SQL)" in section
    assert "Запуск #1." in section
    assert "| баг/підтримка | 2 |" in section
    assert "| інтеграція | 1 |" in section
    # найчастіша категорія йде першою
    assert section.index("| баг/підтримка | 2 |") < section.index("| інтеграція | 1 |")


def test_stats_high_priority_by_department_with_unknown(tmp_path):
    db = str(tmp_path / "test.db")
    results = [
        make_result("A", priority="high", target_department="Фінанси"),
        make_result("B", priority="high", target_department=""),
        make_result("C", priority="low", target_department="Фінанси"),
    ]
    run_id = save_results(results, "in.csv", db_path=db)

    section = build_stats_section(run_id, db_path=db)

    assert "| Фінанси | 1 |" in section  # low-запит не рахується
    assert "| не визначено | 1 |" in section


def test_stats_recent_runs_show_failed_percent_newest_first(tmp_path):
    db = str(tmp_path / "test.db")
    save_results([make_result("A")], "first.csv", db_path=db)
    second = save_results(
        [make_result("B"), make_result("C", processing_status="failed")],
        "second.csv",
        db_path=db,
    )

    section = build_stats_section(second, db_path=db)

    assert "| #2 | second.csv |" in section
    assert "| 2 | 1 | 50.0% |" in section
    assert "| 1 | 0 | 0.0% |" in section
    assert section.index("second.csv") < section.index("first.csv")


def test_stats_section_none_without_db(monkeypatch, tmp_path):
    monkeypatch.delenv("DB_PATH", raising=False)
    assert build_stats_section(1) is None
    assert build_stats_section(1, db_path=str(tmp_path / "missing.db")) is None


def test_append_stats_to_report_keeps_original_text(tmp_path):
    db = str(tmp_path / "test.db")
    run_id = save_results([make_result()], "in.csv", db_path=db)
    report = tmp_path / "report.md"
    report.write_text("# Звіт по обробці запитів", encoding="utf-8")

    assert append_stats_to_report(str(report), run_id, db_path=db) is True

    text = report.read_text(encoding="utf-8")
    assert text.startswith("# Звіт по обробці запитів")
    assert "## Статистика з бази (SQL)" in text


def test_append_stats_to_report_false_when_disabled_or_unwritable(monkeypatch, tmp_path):
    monkeypatch.delenv("DB_PATH", raising=False)
    report = tmp_path / "report.md"
    report.write_text("# Звіт", encoding="utf-8")
    assert append_stats_to_report(str(report), 1) is False
    assert report.read_text(encoding="utf-8") == "# Звіт"

    db = str(tmp_path / "test.db")
    run_id = save_results([make_result()], "in.csv", db_path=db)
    bad_report = str(tmp_path / "no_such_dir" / "report.md")
    assert append_stats_to_report(bad_report, run_id, db_path=db) is False
