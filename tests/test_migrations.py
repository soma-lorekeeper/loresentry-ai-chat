from pathlib import Path

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer


def version(path: Path) -> int:
    return int(path.name[1:].split("__")[0])


MIGRATIONS = sorted(
    (Path(__file__).parent.parent / "db" / "migration").glob("V*__*.sql"),
    key=version,
)
SEALED = b"\x01" + bytes(28)


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
        "folder_code, title, body_enc, wrapped_key, changed_at, attempts, "
        "next_attempt_at) "
        "values (%s, uuidv7(), 1, 'MANUSCRIPT', 't', %s, 'w', now(), 0, now())",
        (document, SEALED),
    )
    conn.execute(
        "insert into document_summaries (document_id, project_id, revision_no, "
        "summary_enc, wrapped_key, fingerprint, model, prompt_version) "
        "values (%s, uuidv7(), 1, %s, 'w', '[]', 'm', 'v')",
        (document, SEALED),
    )

    for table in ("summary_queue", "document_summaries"):
        assert conn.execute(
            f"select event_seq from {table} where document_id = %s", (document,)
        ).fetchone() == (0,)


def test_queued_bodies_and_summaries_are_only_kept_sealed(conn):
    queue = (
        "insert into summary_queue (document_id, project_id, revision_no, "
        "folder_code, title, body_enc, wrapped_key, changed_at) "
        "values (uuidv7(), uuidv7(), 1, 'MANUSCRIPT', 't', %s, %s, now())"
    )
    summary = (
        "insert into document_summaries (document_id, project_id, revision_no, "
        "summary_enc, wrapped_key, fingerprint, model, prompt_version) "
        "values (uuidv7(), uuidv7(), 1, %s, %s, '[]', 'm', 'v')"
    )

    for insert in (queue, summary):
        conn.execute(insert, (SEALED, b"wrapped"))
        for sealed, wrapped_key in ((None, b"wrapped"), (SEALED, None)):
            with pytest.raises(psycopg.errors.NotNullViolation):
                conn.execute(insert, (sealed, wrapped_key))
        for sealed, wrapped_key in (
            (SEALED[:-1], b"wrapped"),
            (b"{" + SEALED[1:], b"wrapped"),
            (SEALED, b""),
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(insert, (sealed, wrapped_key))
    for table, column in (("summary_queue", "body"), ("document_summaries", "summary")):
        assert conn.execute(
            "select count(*) from information_schema.columns "
            "where table_schema = 'public' and table_name = %s and column_name = %s",
            (table, column),
        ).fetchone() == (0,)


def test_dropping_plaintext_refuses_while_any_is_left(conn):
    conn.execute("create schema contract")
    try:
        conn.execute("set search_path to contract")
        for migration in MIGRATIONS:
            if version(migration) < 11:
                conn.execute(migration.read_text())
        contract = next(m for m in MIGRATIONS if version(m) == 11).read_text()
        emptied, kept = (conn.execute("select uuidv7()").fetchone()[0] for _ in "ab")
        queue = (
            "insert into summary_queue (document_id, project_id, event_seq, "
            "revision_no, folder_code, title, body, body_enc, wrapped_key, "
            "changed_at) values (%s, uuidv7(), %s, 1, 'MANUSCRIPT', 't', %s, %s, "
            "%s, now())"
        )
        summary = (
            "insert into document_summaries (document_id, project_id, event_seq, "
            "revision_no, summary, summary_enc, wrapped_key, fingerprint, model, "
            "prompt_version) values (%s, uuidv7(), %s, 1, %s, %s, %s, '[]', 'm', "
            "'v')"
        )

        conn.execute(queue, (emptied, 1, "plain body", None, None))
        with pytest.raises(psycopg.errors.RaiseException, match="bodies"):
            conn.execute(contract)
        conn.execute("delete from summary_queue")
        conn.execute(summary, (emptied, 1, "plain summary", None, None))
        with pytest.raises(psycopg.errors.RaiseException, match="summaries"):
            conn.execute(contract)
        conn.execute("delete from document_summaries")

        conn.execute(queue, (emptied, 5, "", None, None))
        conn.execute(queue, (kept, 4, "", None, None))
        conn.execute(summary, (emptied, 3, None, SEALED, b"w"))
        conn.execute(summary, (kept, 9, None, SEALED, b"w"))
        conn.execute(
            queue, (conn.execute("select uuidv7()").fetchone()[0], 6, "", SEALED, b"w")
        )
        conn.execute(contract)

        assert conn.execute(
            "select document_id from document_summaries"
        ).fetchall() == [(kept,)]
        assert conn.execute("select event_seq from summary_queue").fetchall() == [(6,)]
    finally:
        conn.execute("set search_path to public")
        conn.execute("drop schema contract cascade")


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
        "explanation, quote, source_document_id, source_title, "
        "source_doc_type, source_quote, fingerprint) values "
        "(%s, 0, 'SETTING', 'HIGH', 'e', 'q', uuidv7(), 't', 'place', 'sq', 'f')",
        (run,),
    )
    assert conn.execute(
        "select explanation, quote_focus, source_focus from setting_check_findings "
        "where run_id = %s",
        (run,),
    ).fetchone() == ("e", None, None)
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
