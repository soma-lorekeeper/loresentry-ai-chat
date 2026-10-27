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

    assert {
        "chat_sessions",
        "chat_messages",
        "chat_turns",
        "chat_held_documents",
        "document_summaries",
        "summary_queue",
        "chat_usage",
        "setting_check_runs",
        "setting_check_findings",
        "setting_check_verdicts",
    } <= {row[0] for row in rows}


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


def test_a_session_holds_one_generating_turn_at_a_time(conn):
    session_id = conn.execute(
        "insert into chat_sessions (project_id, user_id, title) "
        "values (uuidv7(), uuidv7(), 't') returning id"
    ).fetchone()[0]
    conn.execute(
        "insert into chat_turns (session_id, status) values (%s, 'GENERATING')",
        (session_id,),
    )
    conn.execute(
        "insert into chat_turns (session_id, status) values (%s, 'COMPLETE')",
        (session_id,),
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into chat_turns (session_id, status) values (%s, 'GENERATING')",
            (session_id,),
        )


def test_question_ids_are_unique_per_session(conn):
    session_id = conn.execute(
        "insert into chat_sessions (project_id, user_id, title) "
        "values (uuidv7(), uuidv7(), 't') returning id"
    ).fetchone()[0]
    client_id = conn.execute("select uuidv7()").fetchone()[0]
    insert = (
        "insert into chat_messages (session_id, role, content_md, status, "
        "client_message_id) values (%s, 'USER', 'q', 'COMPLETE', %s)"
    )
    conn.execute(insert, (session_id, client_id))

    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(insert, (session_id, client_id))


def test_summary_rows_written_without_event_seq_start_before_any_event(conn):
    document = conn.execute("select uuidv7()").fetchone()[0]
    conn.execute(
        "insert into summary_queue (document_id, project_id, revision_no, "
        "folder_code, title, body, changed_at, attempts, next_attempt_at) "
        "values (%s, uuidv7(), 1, 'MANUSCRIPT', 't', 'b', now(), 0, now())",
        (document,),
    )
    conn.execute(
        "insert into document_summaries (document_id, project_id, revision_no, "
        "summary, fingerprint, model, prompt_version) "
        "values (%s, uuidv7(), 1, 's', '[]', 'm', 'v')",
        (document,),
    )

    for table in ("summary_queue", "document_summaries"):
        assert conn.execute(
            f"select event_seq from {table} where document_id = %s", (document,)
        ).fetchone() == (0,)


def insert_run(conn, project, status: str = "RUNNING"):
    return conn.execute(
        "insert into setting_check_runs (project_id, user_id, document_id, "
        "document_title, revision_no, status) values (%s, uuidv7(), uuidv7(), 't', 1, %s) "
        "returning id",
        (project, status),
    ).fetchone()[0]


def test_a_project_runs_one_setting_check_at_a_time(conn):
    project = conn.execute("select uuidv7()").fetchone()[0]
    insert_run(conn, project)
    insert_run(conn, project, "DONE")

    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_run(conn, project)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_run(conn, project, "QUEUED")


def test_findings_go_with_their_run_and_verdicts_are_checked(conn):
    run = insert_run(conn, conn.execute("select uuidv7()").fetchone()[0], "DONE")
    conn.execute(
        "insert into setting_check_findings (run_id, position, kind, confidence, "
        "summary, direction, quote, source_document_id, source_title, "
        "source_doc_type, source_quote, fingerprint) values "
        "(%s, 0, 'SETTING', 'HIGH', 's', 'd', 'q', uuidv7(), 't', 'place', 'sq', 'f')",
        (run,),
    )
    conn.execute("delete from setting_check_runs where id = %s", (run,))

    assert conn.execute(
        "select count(*) from setting_check_findings where run_id = %s", (run,)
    ).fetchone() == (0,)
    verdict = (
        "insert into setting_check_verdicts (project_id, user_id, fingerprint, "
        "verdict, reason, source_document_id, source_quote) "
        "values (uuidv7(), uuidv7(), 'f', %s, %s, uuidv7(), 'q')"
    )
    for value, reason in (("MAYBE", None), ("INTENDED", "가" * 501)):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(verdict, (value, reason))
    conn.execute(verdict, ("INTENDED", "가" * 500))
