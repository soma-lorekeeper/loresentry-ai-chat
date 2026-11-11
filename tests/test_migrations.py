from datetime import UTC, datetime
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
    } <= {row[0] for row in rows}
    assert "setting_check_verdicts" not in {row[0] for row in rows}


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
    run = insert_run(conn, conn.execute("select uuidv7()").fetchone()[0], "DONE")
    finding = (
        "insert into setting_check_findings (run_id, position, kind, confidence, "
        "explanation, quote, source_document_id, source_title, source_doc_type, "
        "source_quote, fingerprint, verdict, reason, decided_at, decided_revision_no) "
        "values (%s, %s, 'SETTING', 'HIGH', 'e', 'q', uuidv7(), 't', 'place', 'sq', "
        "'f', %s, %s, %s, %s)"
    )
    now = datetime.now(UTC)
    for position, (value, reason, decided_at, revision) in enumerate(
        (
            ("MAYBE", None, now, 1),
            ("INTENDED", "가" * 501, now, 1),
            ("INTENDED", None, None, None),
            (None, None, now, 1),
        )
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(finding, (run, position, value, reason, decided_at, revision))
    conn.execute(finding, (run, 9, "INTENDED", "가" * 500, now, 3))


def test_verdicts_move_onto_every_finding_with_their_fingerprint(conn):
    conn.execute("create schema upgrade")
    conn.execute("set search_path to upgrade")
    try:
        for migration in MIGRATIONS:
            if version(migration) >= 12:
                break
            conn.execute(migration.read_text())
        project, other = conn.execute("select uuidv7(), uuidv7()").fetchone()
        first = insert_run(conn, project, "DONE")
        second = insert_run(conn, project, "DONE")
        elsewhere = insert_run(conn, other, "DONE")
        conn.execute(
            "update setting_check_runs set revision_no = 7 where id = %s", (second,)
        )
        finding = (
            "insert into setting_check_findings (run_id, position, kind, confidence, "
            "explanation, quote, source_document_id, source_title, source_doc_type, "
            "source_quote, fingerprint) values (%s, %s, 'SETTING', 'HIGH', 'e', 'q', "
            "uuidv7(), 't', 'place', 'sq', %s)"
        )
        for run, position, mark in (
            (first, 0, "a"),
            (second, 0, "a"),
            (second, 1, "b"),
            (elsewhere, 0, "a"),
        ):
            conn.execute(finding, (run, position, mark))
        conn.execute(
            "insert into setting_check_verdicts (project_id, user_id, fingerprint, "
            "verdict, reason, source_document_id, source_quote, updated_at) "
            "values (%s, uuidv7(), 'a', 'INTENDED', 'r', uuidv7(), 'sq', "
            "'2026-10-30T00:00:00Z')",
            (project,),
        )

        conn.execute(MIGRATIONS[[version(m) for m in MIGRATIONS].index(12)].read_text())

        rows = conn.execute(
            "select f.run_id, f.fingerprint, f.verdict, f.reason, f.decided_at, "
            "f.decided_revision_no from setting_check_findings f "
            "order by f.run_id, f.position"
        ).fetchall()
        decided = datetime(2026, 10, 30, tzinfo=UTC)
        assert rows == [
            (first, "a", "INTENDED", "r", decided, 1),
            (second, "a", "INTENDED", "r", decided, 7),
            (second, "b", None, None, None, None),
            (elsewhere, "a", None, None, None, None),
        ]
        assert conn.execute(
            "select count(*) from information_schema.tables "
            "where table_schema = 'upgrade' and table_name = 'setting_check_verdicts'"
        ).fetchone() == (0,)
        assert conn.execute(
            "select version_id from setting_check_runs where id = %s", (first,)
        ).fetchone() == (None,)
    finally:
        conn.execute("set search_path to public")
        conn.execute("drop schema upgrade cascade")
