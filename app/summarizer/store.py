from dataclasses import dataclass
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.summarizer.events import Change, Remove, RemoveProject, Upsert

BACKOFF_SECONDS = (60, 300, 1800)


@dataclass(frozen=True)
class Job:
    document_id: str
    project_id: str
    sequence: int
    revision_no: int
    folder_code: str
    title: str
    body: str
    attempts: int


@dataclass(frozen=True)
class Previous:
    revision_no: int
    fingerprint: list[list[str | int]]
    prompt_version: str


# event_seq is the ce_sequence of the event a row came from and orders everything
# here. revision_no is only recorded, so the map can say which revision a summary
# was written from.
def apply(conn: Connection, changes: list[Change], prompt_version: str) -> None:
    with conn.transaction():
        for change in changes:
            if isinstance(change, Upsert):
                conn.execute(
                    "insert into summary_queue (document_id, project_id, event_seq, "
                    "revision_no, folder_code, title, body, changed_at, attempts, "
                    "next_attempt_at) "
                    "select %(id)s, %(project)s, %(seq)s, %(revision)s, %(folder)s, "
                    "%(title)s, %(body)s, now(), 0, now() "
                    "where not exists (select 1 from document_summaries s "
                    "where s.document_id = %(id)s and s.event_seq >= %(seq)s "
                    "and s.prompt_version = %(version)s) "
                    "on conflict (document_id) do update set "
                    "project_id = excluded.project_id, "
                    "event_seq = excluded.event_seq, "
                    "revision_no = excluded.revision_no, "
                    "folder_code = excluded.folder_code, title = excluded.title, "
                    "body = excluded.body, changed_at = excluded.changed_at, "
                    "attempts = 0, next_attempt_at = excluded.next_attempt_at "
                    "where summary_queue.event_seq < excluded.event_seq",
                    {
                        "id": change.document_id,
                        "project": change.project_id,
                        "seq": change.sequence,
                        "revision": change.revision_no,
                        "folder": change.folder_code,
                        "title": change.title,
                        "body": change.body,
                        "version": prompt_version,
                    },
                )
            elif isinstance(change, Remove):
                # A removal that arrives late must not take away a newer change.
                for table in ("summary_queue", "document_summaries"):
                    conn.execute(
                        f"delete from {table} where document_id = %s "
                        "and event_seq < %s",
                        (change.document_id, change.sequence),
                    )
            elif isinstance(change, RemoveProject):
                for table in ("summary_queue", "document_summaries"):
                    conn.execute(
                        f"delete from {table} where project_id = %s",
                        (change.project_id,),
                    )


class SummaryStore:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def claim(self, idle_seconds: float, limit: int, lease: int) -> list[Job]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "update summary_queue q "
                    "set next_attempt_at = now() + make_interval(secs => %(lease)s) "
                    "where q.document_id in (select document_id from summary_queue "
                    "where changed_at <= now() - make_interval(secs => %(idle)s) "
                    "and next_attempt_at <= now() "
                    "order by changed_at desc limit %(limit)s for update skip locked) "
                    "returning q.document_id, q.project_id, q.event_seq, "
                    "q.revision_no, q.folder_code, q.title, q.body, q.attempts",
                    {"lease": lease, "idle": idle_seconds, "limit": limit},
                )
            ).fetchall()
        return [
            Job(
                str(row["document_id"]),
                str(row["project_id"]),
                row["event_seq"],
                row["revision_no"],
                row["folder_code"],
                row["title"],
                row["body"],
                row["attempts"],
            )
            for row in rows
        ]

    async def previous(self, document_id: str) -> Previous | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "select revision_no, fingerprint, prompt_version "
                    "from document_summaries where document_id = %s",
                    (document_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return Previous(row["revision_no"], row["fingerprint"], row["prompt_version"])

    async def save(
        self,
        job: Job,
        summary: str,
        fingerprint: list[list[str | int]],
        model: str,
        prompt_version: str,
    ) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "insert into document_summaries (document_id, project_id, event_seq, "
                "revision_no, summary, fingerprint, model, prompt_version) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (document_id) do update set "
                "project_id = excluded.project_id, event_seq = excluded.event_seq, "
                "revision_no = excluded.revision_no, "
                "summary = excluded.summary, fingerprint = excluded.fingerprint, "
                "model = excluded.model, prompt_version = excluded.prompt_version, "
                "created_at = now() "
                "where document_summaries.event_seq <= excluded.event_seq",
                (
                    job.document_id,
                    job.project_id,
                    job.sequence,
                    job.revision_no,
                    summary,
                    Jsonb(fingerprint),
                    model,
                    prompt_version,
                ),
            )
            await self._done(conn, job)

    async def drop_summary(self, job: Job) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "delete from document_summaries where document_id = %s "
                "and event_seq <= %s",
                (job.document_id, job.sequence),
            )
            await self._done(conn, job)

    async def skip(self, job: Job) -> None:
        # The summary keeps its text, revision and fingerprint, but it now stands
        # for this event too, so an older event that arrives late is not queued.
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "update document_summaries set event_seq = greatest(event_seq, %s) "
                "where document_id = %s",
                (job.sequence, job.document_id),
            )
            await self._done(conn, job)

    async def _done(self, conn: Any, job: Job) -> None:
        # A rename during the model call queues a newer event with the same
        # revision, so the queued row is matched by its event.
        await conn.execute(
            "delete from summary_queue where document_id = %s and event_seq = %s",
            (job.document_id, job.sequence),
        )

    async def retry(self, job: Job, max_attempts: int) -> bool:
        attempts = job.attempts + 1
        async with self.pool.connection() as conn:
            if attempts >= max_attempts:
                await self._done(conn, job)
                return False
            delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
            await conn.execute(
                "update summary_queue set attempts = %s, "
                "next_attempt_at = now() + make_interval(secs => %s) "
                "where document_id = %s and event_seq = %s",
                (attempts, delay, job.document_id, job.sequence),
            )
        return True
