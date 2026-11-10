"""Спільні фікстури для тестів, що працюють з PostgreSQL.

Потрібна змінна середовища TEST_DATABASE_URL (рядок підключення до PostgreSQL).
Кожен тест отримує порожню тимчасову схему, яка видаляється після тесту,
тож реальні дані в базі не зачіпаються.
"""

import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

BASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture
def db_url():
    """Рядок підключення до порожньої тимчасової схеми (search_path)."""
    if not BASE_URL:
        pytest.skip("TEST_DATABASE_URL не задано (потрібен PostgreSQL)")
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(BASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    yield make_conninfo(BASE_URL, options=f"-c search_path={schema}")
    with psycopg.connect(BASE_URL, autocommit=True) as admin:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
