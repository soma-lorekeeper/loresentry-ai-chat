import asyncio
import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from testcontainers.community.postgres import PostgresContainer

from app.model import ModelFailure, Usage
from app.settings import Settings
from app.summarizer.events import Remove, RemoveProject, Upsert, header_map, parse
from app.summarizer.fingerprint import changed, fingerprint, threshold
from app.summarizer.settings import SummarizerSettings
from app.summarizer.store import SummaryStore, apply
from app.summarizer.worker import Worker

MIGRATIONS = sorted(
    (Path(__file__).parent.parent / "db" / "migration").glob("V*__*.sql"),
    key=lambda path: int(path.name[1:].split("__")[0]),
)

CHAPTER = " ".join(
    f"레나는 {n}번째 유리 문을 지나 성당으로 걸어갔다." for n in range(60)
)


def test_identical_text_has_no_change():
    assert changed(fingerprint(CHAPTER), fingerprint(CHAPTER)) == 0


def test_change_counts_removed_and_added_sentences():
    old = fingerprint("가나다. 라마바. 사아자.")
    new = fingerprint("가나다. 라마바! 사아자. 차카타.")

    assert changed(old, new) == len("라마바.") + len("라마바!") + len("차카타.")


def test_threshold_is_the_smaller_of_chars_and_ratio():
    assert threshold(10_000, 1_000, 0.3) == 1_000
    assert threshold(1_000, 1_000, 0.3) == 300


def event(kind: str, payload: dict) -> tuple[dict[str, str], bytes]:
    return header_map([("ce_type", kind.encode())]), json.dumps(payload).encode()


def test_file_changes_become_upserts_and_removals():
    project, file = str(uuid4()), str(uuid4())
    body = {"title": "1화", "folder_code": "MANUSCRIPT", "body_text": "본문"}

    assert parse(
        *event(
            "FileChanged",
            {
                "project_id": project,
                "file_id": file,
                "revision_no": 3,
                "change_type": "UPDATED",
                "file": body,
            },
        )
    ) == Upsert(file, project, 3, "MANUSCRIPT", "1화", "본문")
    assert parse(
        *event(
            "FileChanged",
            {
                "project_id": project,
                "file_id": file,
                "revision_no": 4,
                "change_type": "TRASHED",
                "file": None,
            },
        )
    ) == Remove(file)
    assert parse(
        *event(
            "FileChanged",
            {"project_id": project, "file_id": file, "change_type": "DELETED"},
        )
    ) == Remove(file)
    assert parse(*event("ProjectDeleted", {"project_id": project})) == RemoveProject(
        project
    )
    assert parse(*event("GraphRefreshRequested", {"project_id": project})) is None


@pytest.fixture(scope="module")
def database():
    with PostgresContainer("postgres:18", driver=None) as postgres:
        info = {
            "host": postgres.get_container_host_ip(),
            "port": postgres.get_exposed_port(5432),
            "dbname": postgres.dbname,
            "user": postgres.username,
            "password": postgres.password,
        }
        with psycopg.connect(**info, autocommit=True) as conn:
            for migration in MIGRATIONS:
                conn.execute(migration.read_text())
        yield info


class FakeSummaries:
    name = "fake-summary"

    def __init__(self):
        self.inputs: list[str] = []
        self.fail = 0

    async def summarize(self, instructions: str, text: str):
        if self.fail:
            self.fail -= 1
            raise ModelFailure("MODEL_UNAVAILABLE")
        self.inputs.append(text)
        return f"요약 {len(self.inputs)}", Usage(10, 0, 0, 5, 0)


class Rig:
    def __init__(self, database):
        self.database = database
        self.settings = SummarizerSettings(
            base=Settings(
                db_host=database["host"],
                db_port=str(database["port"]),
                db_name=database["dbname"],
                db_username=database["user"],
                db_password=database["password"],
            ),
            idle_seconds=0,
            heartbeat_path="/dev/null",
        )
        self.model = FakeSummaries()
        self.project = str(uuid4())

    def change(self, *changes) -> None:
        with psycopg.connect(self.settings.base.conninfo) as conn:
            apply(conn, list(changes), self.settings.prompt_version)

    def upsert(self, document: str, revision: int, body: str = CHAPTER) -> Upsert:
        return Upsert(document, self.project, revision, "MANUSCRIPT", "1화", body)

    def tick(self) -> list[str]:
        async def once() -> list[str]:
            pool = AsyncConnectionPool(
                self.settings.base.conninfo,
                kwargs={"row_factory": dict_row, "autocommit": True},
                open=False,
            )
            await pool.open()
            try:
                worker = Worker(SummaryStore(pool), self.model, self.settings)
                return await worker.tick()
            finally:
                await pool.close()

        return asyncio.run(once())

    def query(self, sql: str, *params):
        with psycopg.connect(**self.database, autocommit=True) as conn:
            return conn.execute(sql, params).fetchall()


@pytest.fixture
def rig(database):
    with psycopg.connect(**database, autocommit=True) as conn:
        conn.execute("truncate summary_queue, document_summaries")
    return Rig(database)


def test_a_new_document_is_summarized_and_leaves_the_queue(rig):
    document = str(uuid4())
    rig.change(rig.upsert(document, 1))

    assert rig.tick() == ["summarized"]
    assert rig.query(
        "select document_id::text, revision_no, summary, model from document_summaries"
    ) == [(document, 1, "요약 1", "fake-summary")]
    assert rig.query("select count(*) from summary_queue") == [(0,)]
    assert '<document kind="manuscript" title="1화">' in rig.model.inputs[0]


def test_a_small_edit_keeps_the_summary_and_its_fingerprint(rig):
    document = str(uuid4())
    rig.change(rig.upsert(document, 1))
    rig.tick()
    first = rig.query("select fingerprint from document_summaries")

    rig.change(rig.upsert(document, 2, CHAPTER + " 짧은 문장."))
    assert rig.tick() == ["unchanged"]
    assert rig.query("select revision_no from document_summaries") == [(1,)]
    assert rig.query("select fingerprint from document_summaries") == first

    long_edit = CHAPTER.replace("성당으로", "온실로")
    rig.change(rig.upsert(document, 4, long_edit))
    assert rig.tick() == ["summarized"]
    assert rig.query("select revision_no, summary from document_summaries") == [
        (4, "요약 2")
    ]


def test_short_and_empty_documents_skip_the_model(rig):
    short_doc, empty_doc = str(uuid4()), str(uuid4())
    rig.change(
        rig.upsert(short_doc, 1, "레나는 정원사다."), rig.upsert(empty_doc, 1, "")
    )

    assert sorted(rig.tick()) == ["copied", "empty"]
    assert rig.query("select document_id::text, summary from document_summaries") == [
        (short_doc, "레나는 정원사다.")
    ]
    assert rig.model.inputs == []


def test_old_and_repeated_events_are_ignored(rig):
    document = str(uuid4())
    rig.change(rig.upsert(document, 2))
    rig.change(rig.upsert(document, 1, "옛 본문"))
    assert rig.query("select revision_no from summary_queue") == [(2,)]

    rig.tick()
    rig.change(rig.upsert(document, 2))
    assert rig.query("select count(*) from summary_queue") == [(0,)]


def test_trash_and_project_deletion_remove_summaries(rig):
    kept, trashed = str(uuid4()), str(uuid4())
    rig.change(rig.upsert(kept, 1), rig.upsert(trashed, 1))
    rig.tick()

    rig.change(Remove(trashed))
    assert rig.query("select document_id::text from document_summaries") == [(kept,)]

    rig.change(rig.upsert(str(uuid4()), 1), RemoveProject(rig.project))
    assert rig.query("select count(*) from document_summaries") == [(0,)]
    assert rig.query("select count(*) from summary_queue") == [(0,)]


def test_documents_still_being_edited_wait(rig):
    rig.settings = SummarizerSettings(base=rig.settings.base, idle_seconds=600)
    rig.change(rig.upsert(str(uuid4()), 1))

    assert rig.tick() == []
    assert rig.query("select count(*) from summary_queue") == [(1,)]


def test_failures_are_retried_later_then_dropped(rig):
    document = str(uuid4())
    rig.model.fail = 5
    rig.change(rig.upsert(document, 1))

    assert rig.tick() == ["failed"]
    assert rig.query("select attempts, next_attempt_at > now() from summary_queue") == [
        (1, True)
    ]
    rig.query("update summary_queue set next_attempt_at = now() returning 1")
    rig.tick()
    rig.query("update summary_queue set next_attempt_at = now() returning 1")
    rig.tick()
    assert rig.query("select count(*) from summary_queue") == [(0,)]
