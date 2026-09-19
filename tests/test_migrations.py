from pathlib import Path

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

MIGRATIONS = sorted(
    (Path(__file__).parent.parent / "db" / "migration").glob("V*__*.sql"),
    key=lambda path: int(path.name[1:].split("__")[0]),
)


@pytest.fixture(scope="module")
def conn():
    with PostgresContainer("postgres:18", driver=None) as postgres:
        with psycopg.connect(
            host=postgres.get_container_host_ip(),
            port=postgres.get_exposed_port(5432),
            dbname=postgres.dbname,
            user=postgres.username,
            password=postgres.password,
            autocommit=True,
        ) as connection:
            for migration in MIGRATIONS:
                connection.execute(migration.read_text())
            yield connection


def test_migrations_create_chat_tables(conn):
    rows = conn.execute(
        "select table_name from information_schema.tables where table_schema = 'public'"
    ).fetchall()

    assert {"chat_sessions", "chat_messages"} <= {row[0] for row in rows}


def test_chat_messages_reject_unknown_role(conn):
    session_id = conn.execute(
        "insert into chat_sessions (project_id, user_id, title) "
        "values (uuidv7(), uuidv7(), 't') returning id"
    ).fetchone()[0]

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into chat_messages (session_id, role, content_md, status) "
            "values (%s, 'SYSTEM', 'x', 'COMPLETE')",
            (session_id,),
        )
