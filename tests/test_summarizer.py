import asyncio
import base64
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from confluent_kafka import KafkaError
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from testcontainers.community.postgres import PostgresContainer

from app.manuscript import ProjectKeys, seal
from app.model import ModelFailure, Usage
from app.settings import Settings
from app.summarizer.consumer import changes_of
from app.summarizer.events import (
    Malformed,
    Remove,
    RemoveProject,
    Upsert,
    header_map,
    parse,
)
from app.summarizer.fingerprint import changed, fingerprint, threshold
from app.summarizer.settings import SummarizerSettings
from app.summarizer.store import SummaryStore, apply
from app.summarizer.worker import Worker
from tests.support import FakeKms

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


def event(
    kind: str, payload: dict, sequence: str | None = "7"
) -> tuple[dict[str, str], bytes]:
    headers = [("ce_type", kind.encode())]
    if sequence is not None:
        headers.append(("ce_sequence", sequence.encode()))
    return header_map(headers), json.dumps(payload).encode()


def file_changed(
    project: str, file: str, change_type: str, state: str = "ACTIVE"
) -> dict:
    """A FileChanged as content sends it: the whole document, whatever the action."""
    return {
        "project_id": project,
        "file_id": file,
        "change_type": change_type,
        "changed_fields": ["BODY"],
        "file": {
            "title": "1화",
            "folder_code": "MANUSCRIPT",
            "episode_id": None,
            "state": state,
            "revision_no": 3,
            "body_text": "본문",
            "properties": [],
        },
        "relations": [],
    }


def test_file_changes_become_upserts_and_removals():
    project, file = str(uuid4()), str(uuid4())
    upsert = Upsert(file, project, 7, 3, "MANUSCRIPT", "1화", "본문")

    for change_type in ("CREATED", "UPDATED", "RESTORED"):
        assert (
            parse(*event("FileChanged", file_changed(project, file, change_type)))
            == upsert
        )
    # A trashed document keeps its snapshot; its state is what removes it.
    for change_type in ("TRASHED", "UPDATED"):
        assert parse(
            *event("FileChanged", file_changed(project, file, change_type, "TRASHED"))
        ) == Remove(file, 7)
    deleted = {
        "project_id": project,
        "file_id": file,
        "change_type": "DELETED",
        "changed_fields": [],
        "file": None,
        "relations": None,
    }
    assert parse(*event("FileChanged", deleted)) == Remove(file, 7)
    assert parse(
        *event("ProjectDeleted", {"project_id": project}, sequence=None)
    ) == RemoveProject(project)
    assert parse(*event("GraphRefreshRequested", {"project_id": project})) is None


def encrypted(payload: dict, body_enc: bytes, wrapped_key: bytes | None) -> dict:
    file = {key: value for key, value in payload["file"].items() if key != "body_text"}
    file["body_text_enc"] = base64.b64encode(body_enc).decode()
    return {
        **payload,
        "wrapped_key": (
            None if wrapped_key is None else base64.b64encode(wrapped_key).decode()
        ),
        "file": file,
    }


def test_encrypted_bodies_are_kept_sealed_with_their_wrapped_key():
    project, file = str(uuid4()), str(uuid4())
    body_enc = seal(os.urandom(32), project, file, "body_text", "본문")
    wrapped = os.urandom(180)
    payload = encrypted(file_changed(project, file, "UPDATED"), body_enc, wrapped)

    assert parse(*event("FileChanged", payload)) == Upsert(
        file, project, 7, 3, "MANUSCRIPT", "1화", "", body_enc, wrapped
    )
    trashed = encrypted(
        file_changed(project, file, "UPDATED", "TRASHED"), body_enc, wrapped
    )
    assert parse(*event("FileChanged", trashed)) == Remove(file, 7)
    both = file_changed(project, file, "UPDATED")
    both["file"]["body_text_enc"] = payload["file"]["body_text_enc"]
    both["wrapped_key"] = payload["wrapped_key"]
    assert parse(*event("FileChanged", both)).body_enc == body_enc
    legacy = {**file_changed(project, file, "UPDATED"), "wrapped_key": None}
    legacy["file"]["body_text_enc"] = None
    assert parse(*event("FileChanged", legacy)) == Upsert(
        file, project, 7, 3, "MANUSCRIPT", "1화", "본문"
    )


def test_encrypted_bodies_off_the_contract_are_malformed():
    project, file = str(uuid4()), str(uuid4())
    body_enc = seal(os.urandom(32), project, file, "body_text", "본문")
    current = file_changed(project, file, "UPDATED")

    for payload in (
        encrypted(current, body_enc, None),
        encrypted(current, body_enc, b""),
        encrypted(current, b'{"body_text": "x"}', os.urandom(180)),
    ):
        with pytest.raises(Malformed):
            parse(*event("FileChanged", payload))
    not_base64 = encrypted(current, body_enc, os.urandom(180))
    not_base64["file"]["body_text_enc"] = "not base64!"
    with pytest.raises(ValueError):
        parse(*event("FileChanged", not_base64))


def test_events_off_the_contract_are_malformed():
    project, file = str(uuid4()), str(uuid4())
    before = {
        "project_id": project,
        "file_id": file,
        "revision_no": 3,
        "change_type": "UPDATED",
        "file": {"title": "1화", "folder_code": "MANUSCRIPT", "body_text": "본문"},
    }
    trashed_before = {**before, "change_type": "TRASHED", "file": None}
    current = file_changed(project, file, "UPDATED")

    for payload in (before, trashed_before):
        with pytest.raises(Malformed):
            parse(*event("FileChanged", payload))
    for sequence in (None, "", "seven"):
        with pytest.raises(Malformed):
            parse(*event("FileChanged", current, sequence=sequence))


class Message:
    def __init__(self, offset: int, headers: dict[str, str], value: bytes, error=None):
        self._offset = offset
        self._headers = [(name, text.encode()) for name, text in headers.items()]
        self._value = value
        self._error = error

    def error(self):
        return self._error

    def headers(self):
        return self._headers

    def value(self):
        return self._value

    def topic(self):
        return "content.project.changed.v1"

    def partition(self):
        return 0

    def offset(self):
        return self._offset


def test_events_off_the_contract_are_skipped_and_the_rest_applied():
    project, file = str(uuid4()), str(uuid4())
    current = event("FileChanged", file_changed(project, file, "UPDATED"))
    before = event(
        "FileChanged", {**file_changed(project, file, "UPDATED"), "file": {}}
    )

    changes, skipped = changes_of(
        [
            Message(1, *before),
            Message(2, current[0], b"not json"),
            Message(3, *current),
        ]
    )

    assert changes == [Upsert(file, project, 7, 3, "MANUSCRIPT", "1화", "본문")]
    assert skipped == 2


def test_a_topic_that_does_not_exist_yet_is_passed_over():
    project, file = str(uuid4()), str(uuid4())
    current = event("FileChanged", file_changed(project, file, "UPDATED"))
    missing = KafkaError(KafkaError.UNKNOWN_TOPIC_OR_PART)

    changes, skipped = changes_of([Message(0, {}, b"", missing), Message(1, *current)])

    assert changes == [Upsert(file, project, 7, 3, "MANUSCRIPT", "1화", "본문")]
    assert skipped == 0


def test_both_topic_versions_are_consumed_by_default(monkeypatch):
    for name in ("SUMMARY_TOPICS", "SUMMARY_TOPIC"):
        monkeypatch.delenv(name, raising=False)
    assert SummarizerSettings.from_environment().topics == (
        "content.project.changed.v1",
        "content.project.changed.v2",
    )
    monkeypatch.setenv("SUMMARY_TOPIC", "content.project.changed.v1")
    assert SummarizerSettings.from_environment().topics == (
        "content.project.changed.v1",
    )
    monkeypatch.setenv("SUMMARY_TOPICS", " a.v1, a.v2 ,")
    assert SummarizerSettings.from_environment().topics == ("a.v1", "a.v2")


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
        self.during = None

    async def summarize(self, instructions: str, text: str):
        if self.during:
            self.during()
            self.during = None
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
        self.kms = FakeKms()
        self.key = os.urandom(32)
        self.wrapped_key = self.kms.wrap(self.project, self.key)

    def change(self, *changes) -> None:
        with psycopg.connect(self.settings.base.conninfo) as conn:
            apply(conn, list(changes), self.settings.prompt_version)

    def upsert(
        self,
        document: str,
        sequence: int,
        body: str = CHAPTER,
        revision: int | None = None,
        title: str = "1화",
    ) -> Upsert:
        return Upsert(
            document,
            self.project,
            sequence,
            sequence if revision is None else revision,
            "MANUSCRIPT",
            title,
            body,
        )

    def sealed(
        self, document: str, sequence: int, body: str = CHAPTER, title: str = "1화"
    ) -> Upsert:
        return Upsert(
            document,
            self.project,
            sequence,
            sequence,
            "MANUSCRIPT",
            title,
            "",
            seal(self.key, self.project, document, "body_text", body),
            self.wrapped_key,
        )

    def tick(self) -> list[str]:
        async def once() -> list[str]:
            pool = AsyncConnectionPool(
                self.settings.base.conninfo,
                kwargs={"row_factory": dict_row, "autocommit": True},
                open=False,
            )
            await pool.open()
            try:
                keys = ProjectKeys(self.kms, self.kms.key_id)
                worker = Worker(SummaryStore(pool), self.model, self.settings, keys)
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
        conn.execute(
            "truncate summary_queue, document_summaries, setting_check_runs, "
            "setting_check_verdicts cascade"
        )
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
    # The summary still shows the revision it was written from, but stands for
    # the skipped event too.
    assert rig.query("select revision_no, event_seq from document_summaries") == [
        (1, 2)
    ]
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
    assert rig.query("select event_seq from summary_queue") == [(2,)]

    rig.tick()
    rig.change(rig.upsert(document, 2))
    assert rig.query("select count(*) from summary_queue") == [(0,)]


def test_trash_and_project_deletion_remove_summaries(rig):
    kept, trashed = str(uuid4()), str(uuid4())
    rig.change(rig.upsert(kept, 1), rig.upsert(trashed, 1))
    rig.tick()

    rig.change(Remove(trashed, 2))
    assert rig.query("select document_id::text from document_summaries") == [(kept,)]

    rig.change(rig.upsert(str(uuid4()), 1), RemoveProject(rig.project))
    assert rig.query("select count(*) from document_summaries") == [(0,)]
    assert rig.query("select count(*) from summary_queue") == [(0,)]


def test_project_deletion_removes_its_setting_checks(rig):
    kept = str(uuid4())
    for project in (rig.project, kept):
        rig.query(
            "insert into setting_check_runs (project_id, user_id, document_id, "
            "document_title, revision_no, status) "
            "values (%s, uuidv7(), uuidv7(), 't', 1, 'DONE') returning id",
            project,
        )
        rig.query(
            "insert into setting_check_verdicts (project_id, user_id, fingerprint, "
            "verdict, source_document_id, source_quote) "
            "values (%s, uuidv7(), 'f', 'INTENDED', uuidv7(), 'q') returning 1",
            project,
        )

    rig.change(RemoveProject(rig.project))

    for table in ("setting_check_runs", "setting_check_verdicts"):
        assert rig.query(f"select project_id::text from {table}") == [(kept,)]


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


def test_a_rename_with_the_same_revision_replaces_the_queued_change(rig):
    document = str(uuid4())
    rig.change(rig.upsert(document, 1, revision=1))
    rig.change(rig.upsert(document, 2, revision=1, title="2화"))

    assert rig.query("select event_seq, revision_no, title from summary_queue") == [
        (2, 1, "2화")
    ]


def test_a_late_removal_keeps_what_came_after_it(rig):
    document = str(uuid4())
    rig.change(rig.upsert(document, 20))
    rig.tick()

    rig.change(Remove(document, 15))
    assert rig.query("select event_seq from document_summaries") == [(20,)]

    rig.change(rig.upsert(document, 30, CHAPTER.replace("성당으로", "온실로")))
    rig.change(Remove(document, 25))
    assert rig.query("select count(*) from document_summaries") == [(0,)]
    assert rig.query("select event_seq from summary_queue") == [(30,)]

    rig.change(Remove(document, 31))
    assert rig.query("select count(*) from summary_queue") == [(0,)]


def test_a_late_change_after_a_skip_is_not_queued(rig):
    document = str(uuid4())
    rig.change(rig.upsert(document, 10))
    rig.tick()
    rig.change(rig.upsert(document, 20, CHAPTER + " 짧은 문장."))
    assert rig.tick() == ["unchanged"]

    rig.change(rig.upsert(document, 15, CHAPTER.replace("성당으로", "온실로")))
    assert rig.query("select count(*) from summary_queue") == [(0,)]


def test_a_change_during_the_model_call_stays_queued(rig):
    document = str(uuid4())
    rig.change(rig.upsert(document, 10, revision=1))
    rig.model.during = lambda: rig.change(rig.upsert(document, 11, revision=1))

    assert rig.tick() == ["summarized"]
    assert rig.query("select event_seq from document_summaries") == [(10,)]
    assert rig.query("select event_seq from summary_queue") == [(11,)]


def test_a_retry_leaves_a_change_that_came_during_the_call(rig):
    document = str(uuid4())
    rig.model.fail = 1
    rig.change(rig.upsert(document, 10, revision=1))
    rig.model.during = lambda: rig.change(rig.upsert(document, 11, revision=1))

    assert rig.tick() == ["failed"]
    assert rig.query("select event_seq, attempts from summary_queue") == [(11, 0)]


def test_queued_bodies_follow_the_newest_event_in_either_form(rig):
    document = str(uuid4())
    sealed = rig.sealed(document, 2)
    columns = "select body, body_enc, wrapped_key from summary_queue"

    rig.change(rig.upsert(document, 1, "평문 본문"), sealed)
    assert rig.query(columns) == [("", sealed.body_enc, rig.wrapped_key)]

    rig.change(rig.upsert(document, 1, "늦게 온 평문"))
    assert rig.query(columns) == [("", sealed.body_enc, rig.wrapped_key)]

    rig.change(rig.upsert(document, 3, "새 평문"))
    assert rig.query(columns) == [("새 평문", None, None)]

    newer = rig.sealed(document, 4)
    rig.change(newer)
    assert rig.query(columns) == [("", newer.body_enc, rig.wrapped_key)]


def test_sealed_bodies_are_opened_only_to_summarize(rig):
    first, second = str(uuid4()), str(uuid4())
    rig.change(rig.sealed(first, 1), rig.sealed(second, 1, "레나는 정원사다."))

    assert sorted(rig.tick()) == ["copied", "summarized"]
    assert CHAPTER in rig.model.inputs[0]
    assert sorted(rig.query("select summary from document_summaries")) == [
        ("레나는 정원사다.",),
        ("요약 1",),
    ]
    assert rig.query("select count(*) from summary_queue") == [(0,)]
    assert len(rig.kms.calls) == 1


def test_a_kms_outage_keeps_the_row_without_using_an_attempt(rig):
    document = str(uuid4())
    rig.kms.fail_with = "KMSInternalException"
    rig.change(rig.sealed(document, 1))

    assert rig.tick() == ["deferred"]
    assert rig.query("select attempts, next_attempt_at > now() from summary_queue") == [
        (0, True)
    ]
    assert rig.model.inputs == []

    rig.kms.fail_with = None
    rig.query("update summary_queue set next_attempt_at = now() returning 1")
    assert rig.tick() == ["summarized"]
    assert rig.query("select count(*) from summary_queue") == [(0,)]


def test_a_body_that_does_not_open_is_dropped(rig):
    document = str(uuid4())
    wrong_subject = Upsert(
        document,
        rig.project,
        1,
        1,
        "MANUSCRIPT",
        "1화",
        "",
        seal(rig.key, rig.project, str(uuid4()), "body_text", CHAPTER),
        rig.wrapped_key,
    )
    rig.change(wrong_subject)

    assert rig.tick() == ["unreadable"]
    assert rig.query("select count(*) from summary_queue") == [(0,)]
    assert rig.query("select count(*) from document_summaries") == [(0,)]
    assert rig.model.inputs == []
