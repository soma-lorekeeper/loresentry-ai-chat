from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg_pool import AsyncConnectionPool

from app.errors import ChatFailure
from app.model import Usage
from app.setting_check.prompts import Note
from app.store import iso

RUN_SELECT = (
    "select r.id, r.project_id, r.document_id, r.document_title, r.revision_no, "
    "r.status, r.stage, r.read_manuscripts, r.read_from_ordinal, r.read_to_ordinal, "
    "r.read_settings, r.read_core_facts, r.failure_code, r.created_at, "
    "r.finished_at, c.open, c.intended, c.not_conflict "
    "from setting_check_runs r cross join lateral (select "
    "count(*) filter (where v.verdict is null) as open, "
    "count(*) filter (where v.verdict = 'INTENDED') as intended, "
    "count(*) filter (where v.verdict = 'NOT_CONFLICT') as not_conflict "
    "from setting_check_findings f left join setting_check_verdicts v "
    "on v.project_id = r.project_id and v.fingerprint = f.fingerprint "
    "where f.run_id = r.id) c "
)

FINDING_COLUMNS = (
    "f.id, f.kind, f.core, f.confidence, f.explanation, f.quote, "
    "f.quote_focus, f.source_document_id, f.source_title, f.source_doc_type, "
    "f.source_ordinal, f.source_quote, f.source_focus, v.verdict, v.reason "
)

FINDING_SELECT = (
    f"select {FINDING_COLUMNS}from setting_check_findings f "
    "join setting_check_runs r on r.id = f.run_id "
    "left join setting_check_verdicts v "
    "on v.project_id = r.project_id and v.fingerprint = f.fingerprint "
)

FINDING_OWNER = (
    "select f.fingerprint, f.source_document_id, f.source_quote, r.project_id "
    "from setting_check_findings f join setting_check_runs r on r.id = f.run_id "
    "where f.id = %s and r.user_id = %s"
)


def run_json(
    row: dict[str, Any], findings: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    body = {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "document_id": str(row["document_id"]),
        "document_title": row["document_title"],
        "revision_no": row["revision_no"],
        "status": row["status"],
        "stage": row["stage"],
        "created_at": iso(row["created_at"]),
        "finished_at": iso(row["finished_at"]) if row["finished_at"] else None,
        "read": {
            "manuscripts": row["read_manuscripts"],
            "from_ordinal": row["read_from_ordinal"],
            "to_ordinal": row["read_to_ordinal"],
            "settings": row["read_settings"],
            "core_facts": row["read_core_facts"],
        },
        "counts": {
            "open": row["open"],
            "intended": row["intended"],
            "not_conflict": row["not_conflict"],
        },
        "failure_code": row["failure_code"],
    }
    if findings is not None:
        body["findings"] = findings
    return body


def finding_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "core": row["core"],
        "confidence": row["confidence"],
        "explanation": row["explanation"],
        "quote": row["quote"],
        "quote_focus": row["quote_focus"],
        "source": {
            "document_id": str(row["source_document_id"]),
            "title": row["source_title"],
            "doc_type": row["source_doc_type"],
            "ordinal": row["source_ordinal"],
            "quote": row["source_quote"],
            "quote_focus": row["source_focus"],
        },
        "verdict": row["verdict"],
        "reason": row["reason"],
    }


@dataclass(frozen=True)
class Read:
    manuscripts: int
    from_ordinal: int | None
    to_ordinal: int | None
    settings: int
    core_facts: int


@dataclass(frozen=True)
class Finding:
    kind: str
    core: bool
    confidence: str
    explanation: str
    quote: str
    quote_focus: str | None
    source_document_id: str
    source_title: str
    source_doc_type: str
    source_ordinal: int | None
    source_quote: str
    source_focus: str | None
    fingerprint: str


class CheckStore:
    def __init__(self, pool: AsyncConnectionPool, stale_seconds: int):
        self.pool = pool
        self.stale_seconds = stale_seconds

    async def _expire(
        self, conn: AsyncConnection, where: str, params: tuple[Any, ...]
    ) -> int:
        result = await conn.execute(
            "update setting_check_runs set status = 'FAILED', stage = null, "
            "failure_code = 'INTERRUPTED', finished_at = now() "
            "where status = 'RUNNING' "
            "and created_at <= now() - make_interval(secs => %s) "
            f"and {where}",
            (self.stale_seconds, *params),
        )
        return result.rowcount

    async def expire_stale(self) -> int:
        async with self.pool.connection() as conn:
            return await self._expire(conn, "true", ())

    async def _run(self, conn: AsyncConnection, run_id: UUID) -> dict[str, Any]:
        row = await (
            await conn.execute(f"{RUN_SELECT} where r.id = %s", (run_id,))
        ).fetchone()
        return run_json(row)

    async def begin(
        self,
        user_id: UUID,
        project_id: UUID,
        document_id: str,
        title: str,
        revision_no: int,
        model: str,
    ) -> dict[str, Any]:
        try:
            async with self.pool.connection() as conn, conn.transaction():
                await self._expire(conn, "project_id = %s", (project_id,))
                row = await (
                    await conn.execute(
                        "insert into setting_check_runs (project_id, user_id, "
                        "document_id, document_title, revision_no, status, stage, "
                        "model) values (%s, %s, %s, %s, %s, 'RUNNING', 'READING', %s) "
                        "returning id",
                        (project_id, user_id, document_id, title, revision_no, model),
                    )
                ).fetchone()
                return await self._run(conn, row["id"])
        except UniqueViolation:
            raise ChatFailure("SETTING_CHECK_IN_PROGRESS") from None

    async def advance(
        self, run_id: UUID, title: str, revision_no: int, read: Read
    ) -> bool:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "update setting_check_runs set stage = 'FINDING', "
                    "document_title = %s, revision_no = %s, read_manuscripts = %s, "
                    "read_from_ordinal = %s, read_to_ordinal = %s, read_settings = %s, "
                    "read_core_facts = %s where id = %s and status = 'RUNNING' "
                    "returning id",
                    (
                        title,
                        revision_no,
                        read.manuscripts,
                        read.from_ordinal,
                        read.to_ordinal,
                        read.settings,
                        read.core_facts,
                        run_id,
                    ),
                )
            ).fetchone()
        return row is not None

    async def stage(self, run_id: UUID, stage: str) -> bool:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "update setting_check_runs set stage = %s "
                    "where id = %s and status = 'RUNNING' returning id",
                    (stage, run_id),
                )
            ).fetchone()
        return row is not None

    async def finish(self, run_id: UUID, findings: list[Finding], usage: Usage) -> bool:
        async with self.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "select status from setting_check_runs where id = %s for update",
                    (run_id,),
                )
            ).fetchone()
            if row is None or row["status"] != "RUNNING":
                return False
            if findings:
                async with conn.cursor() as cursor:
                    await cursor.executemany(
                        "insert into setting_check_findings (run_id, position, kind, "
                        "core, confidence, explanation, quote, quote_focus, "
                        "source_document_id, source_title, source_doc_type, "
                        "source_ordinal, source_quote, source_focus, fingerprint) "
                        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                        "%s, %s)",
                        [
                            (
                                run_id,
                                position,
                                finding.kind,
                                finding.core,
                                finding.confidence,
                                finding.explanation,
                                finding.quote,
                                finding.quote_focus,
                                finding.source_document_id,
                                finding.source_title,
                                finding.source_doc_type,
                                finding.source_ordinal,
                                finding.source_quote,
                                finding.source_focus,
                                finding.fingerprint,
                            )
                            for position, finding in enumerate(findings)
                        ],
                    )
            await self._close(conn, run_id, "DONE", None, usage)
        return True

    async def fail(self, run_id: UUID, code: str, usage: Usage) -> None:
        async with self.pool.connection() as conn:
            await self._close(conn, run_id, "FAILED", code, usage)

    async def _close(
        self,
        conn: AsyncConnection,
        run_id: UUID,
        status: str,
        code: str | None,
        usage: Usage,
    ) -> None:
        await conn.execute(
            "update setting_check_runs set status = %s, stage = null, "
            "failure_code = %s, input_tokens = %s, cached_tokens = %s, "
            "output_tokens = %s, finished_at = now() "
            "where id = %s and status = 'RUNNING'",
            (
                status,
                code,
                usage.input_tokens,
                usage.cached_tokens,
                usage.output_tokens,
                run_id,
            ),
        )

    async def record_usage(self, run_id: UUID, usage: Usage) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "update setting_check_runs set input_tokens = %s, cached_tokens = %s, "
                "output_tokens = %s where id = %s",
                (usage.input_tokens, usage.cached_tokens, usage.output_tokens, run_id),
            )

    async def cancel(self, user_id: UUID, run_id: UUID) -> bool:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "update setting_check_runs set status = 'CANCELLED', stage = null, "
                    "finished_at = now() where id = %s and user_id = %s "
                    "and status = 'RUNNING' returning id",
                    (run_id, user_id),
                )
            ).fetchone()
            if row is not None:
                return True
            found = await (
                await conn.execute(
                    "select 1 from setting_check_runs where id = %s and user_id = %s",
                    (run_id, user_id),
                )
            ).fetchone()
        if found is None:
            raise ChatFailure("SETTING_CHECK_NOT_FOUND")
        return False

    async def summary(self, user_id: UUID, project_id: UUID) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            await self._expire(conn, "project_id = %s", (project_id,))
            rows = await (
                await conn.execute(
                    "with latest as (select distinct on (document_id) id "
                    "from setting_check_runs where user_id = %s and project_id = %s "
                    "order by document_id, created_at desc, id desc) "
                    f"{RUN_SELECT} where r.id in (select id from latest) "
                    "order by r.created_at desc, r.id desc",
                    (user_id, project_id),
                )
            ).fetchall()
        runs = [run_json(row) for row in rows]
        return {
            "running": next((run for run in runs if run["status"] == "RUNNING"), None),
            "chapters": [
                {"document_id": run["document_id"], "latest": run} for run in runs
            ],
        }

    async def runs(
        self, user_id: UUID, project_id: UUID, document_id: UUID | None
    ) -> list[dict[str, Any]]:
        where = "r.user_id = %s and r.project_id = %s"
        params: tuple[Any, ...] = (user_id, project_id)
        if document_id is not None:
            where += " and r.document_id = %s"
            params += (document_id,)
        async with self.pool.connection() as conn:
            await self._expire(conn, "project_id = %s", (project_id,))
            rows = await (
                await conn.execute(
                    f"{RUN_SELECT} where {where} order by r.created_at desc, r.id desc",
                    params,
                )
            ).fetchall()
        return [run_json(row) for row in rows]

    async def run(self, user_id: UUID, run_id: UUID) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            await self._expire(conn, "id = %s", (run_id,))
            row = await (
                await conn.execute(
                    f"{RUN_SELECT} where r.id = %s and r.user_id = %s",
                    (run_id, user_id),
                )
            ).fetchone()
            if row is None:
                raise ChatFailure("SETTING_CHECK_NOT_FOUND")
            findings = await (
                await conn.execute(
                    f"{FINDING_SELECT} where f.run_id = %s order by f.position",
                    (run_id,),
                )
            ).fetchall()
        return run_json(row, [finding_json(finding) for finding in findings])

    async def decisions(self, user_id: UUID, project_id: UUID) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select * from (select distinct on (v.fingerprint) "
                    f"{FINDING_COLUMNS}, r.document_id, r.document_title, "
                    "v.updated_at as decided_at from setting_check_verdicts v "
                    "join setting_check_findings f on f.fingerprint = v.fingerprint "
                    "join setting_check_runs r on r.id = f.run_id "
                    "and r.project_id = v.project_id "
                    "where v.project_id = %s and r.user_id = %s "
                    "order by v.fingerprint, r.created_at desc, f.id desc) d "
                    "order by decided_at desc, id desc",
                    (project_id, user_id),
                )
            ).fetchall()
        return [
            {
                "document_id": str(row["document_id"]),
                "document_title": row["document_title"],
                "decided_at": iso(row["decided_at"]),
                "finding": finding_json(row),
            }
            for row in rows
        ]

    async def notes(
        self, project_id: UUID, source_ids: list[str]
    ) -> dict[str, list[Note]]:
        if not source_ids:
            return {}
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select source_document_id, verdict, source_quote, reason "
                    "from setting_check_verdicts where project_id = %s "
                    "and source_document_id = any(%s::uuid[]) "
                    "order by updated_at desc, fingerprint",
                    (project_id, source_ids),
                )
            ).fetchall()
        found: dict[str, list[Note]] = {}
        for row in rows:
            found.setdefault(str(row["source_document_id"]), []).append(
                Note(row["verdict"], row["source_quote"], row["reason"])
            )
        return found

    async def _owned(
        self, conn: AsyncConnection, user_id: UUID, finding_id: UUID
    ) -> dict[str, Any]:
        row = await (
            await conn.execute(FINDING_OWNER, (finding_id, user_id))
        ).fetchone()
        if row is None:
            raise ChatFailure("SETTING_CHECK_FINDING_NOT_FOUND")
        return row

    async def _finding(self, conn: AsyncConnection, finding_id: UUID) -> dict[str, Any]:
        row = await (
            await conn.execute(f"{FINDING_SELECT} where f.id = %s", (finding_id,))
        ).fetchone()
        return finding_json(row)

    async def set_verdict(
        self, user_id: UUID, finding_id: UUID, verdict: str, reason: str | None
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn, conn.transaction():
            owned = await self._owned(conn, user_id, finding_id)
            await conn.execute(
                "insert into setting_check_verdicts (project_id, user_id, fingerprint, "
                "verdict, reason, source_document_id, source_quote) "
                "values (%s, %s, %s, %s, %s, %s, %s) "
                "on conflict (project_id, fingerprint) do update set "
                "user_id = excluded.user_id, verdict = excluded.verdict, "
                "reason = excluded.reason, updated_at = now()",
                (
                    owned["project_id"],
                    user_id,
                    owned["fingerprint"],
                    verdict,
                    reason,
                    owned["source_document_id"],
                    owned["source_quote"],
                ),
            )
            return await self._finding(conn, finding_id)

    async def clear_verdict(self, user_id: UUID, finding_id: UUID) -> dict[str, Any]:
        async with self.pool.connection() as conn, conn.transaction():
            owned = await self._owned(conn, user_id, finding_id)
            await conn.execute(
                "delete from setting_check_verdicts "
                "where project_id = %s and fingerprint = %s",
                (owned["project_id"], owned["fingerprint"]),
            )
            return await self._finding(conn, finding_id)
