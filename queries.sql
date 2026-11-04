-- Практичні SQL-запити до бази Request Classifier (SQLite).
-- Відкрий requests.db у DB Browser for SQLite (вкладка "Execute SQL")
-- або в терміналі: sqlite3 requests.db

-- 1. Усі запуски (DQL: SELECT, ORDER BY)
SELECT id, source_file, started_at, total, failed
FROM runs
ORDER BY id DESC;

-- 2. Скільки запитів у кожній категорії (GROUP BY, COUNT)
SELECT category, COUNT(*) AS cnt
FROM results
GROUP BY category
ORDER BY cnt DESC;

-- 3. Запити з високим пріоритетом, які потребують уточнення (WHERE, AND)
SELECT request_id, category, short_summary
FROM results
WHERE priority = 'high' AND needs_clarification = 1;

-- 4. Запити, які не вдалося обробити (failed)
SELECT run_id, request_id, short_summary
FROM results
WHERE processing_status = 'failed';

-- 5. Запуски, в яких були помилки (JOIN + GROUP BY + HAVING)
SELECT r.id, r.source_file, COUNT(*) AS failed_cnt
FROM runs r
JOIN results x ON x.run_id = r.id
WHERE x.processing_status = 'failed'
GROUP BY r.id, r.source_file
HAVING COUNT(*) >= 1;

-- 6. Запити з назвою файлу запуску (INNER JOIN)
SELECT x.request_id, x.category, r.source_file
FROM results x
JOIN runs r ON r.id = x.run_id;

-- 7. Відділи, які створюють найбільше запитів (порожній відділ = "не визначено")
SELECT
    CASE WHEN target_department = '' THEN 'не визначено' ELSE target_department END AS department,
    COUNT(*) AS cnt
FROM results
GROUP BY department
ORDER BY cnt DESC;

-- 8. Запуски без жодного результату (LEFT JOIN ... IS NULL, як у тестовому завданні)
SELECT r.id, r.source_file
FROM runs r
LEFT JOIN results x ON x.run_id = r.id
WHERE x.id IS NULL;

-- 9. Частка failed у кожному запуску, у відсотках (арифметика в SELECT)
SELECT id, total, failed, ROUND(100.0 * failed / total, 1) AS failed_pct
FROM runs;
