import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.errors import ChatFailure
from app.manuscript import KeyUnavailable, ProjectKeys, Unreadable, unseal
from app.navigation import Summary
from app.prompt import Exchange, Question
from app.text import chat_preview, chat_title, search_text, snippet
from app.usage import credits, today

SESSION_COLUMNS = "id, project_id, title, updated_at, preview"
MESSAGE_COLUMNS = (
    "id, session_id, role, content_md, intent, quote, citations, grounding, created_at"
)
SEARCH_LIMIT = 50

log = logging.getLogger("ai_chat.store")


def iso(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def session_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "title": row["title"],
        "updated_at": iso(row["updated_at"]),
        "preview": row["preview"],
    }


def message_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "role": row["role"].lower(),
        "content": row["content_md"],
        "intent": row["intent"],
        "quote": row["quote"],
        "citations": row["citations"],
        "grounding": row["grounding"],
        "created_at": iso(row["created_at"]),
    }


@dataclass(frozen=True)
class Started:
    session: dict[str, Any]
    question: dict[str, Any]
    turn_id: UUID


@dataclass(frozen=True)
class Replayed:
    question: dict[str, Any]
    answer: dict[str, Any]


@dataclass(frozen=True)
class Metrics:
    model: str | None = None
    context_chars: int | None = None
    input_tokens: int | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    snapshot_ms: int | None = None
    first_token_ms: int | None = None
    total_ms: int | None = None
    steps: int | None = None
    judge_calls: int | None = None
    chars_read: int | None = None
    grounding: str | None = None
    rejections: int | None = None
    reviews: list[dict[str, Any]] | None = None


def like(term: str) -> str:
    return "%" + re.sub(r"([\\%_])", r"\\\1", term) + "%"


class Store:
    def __init__(
        self, pool: AsyncConnectionPool, stale_seconds: int, keys: ProjectKeys
    ):
        self.pool = pool
        self.stale_seconds = stale_seconds
        self.keys = keys

    async def create_session(
        self, user_id: UUID, project_id: UUID, title: str
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    f"insert into chat_sessions (project_id, user_id, title) "
                    f"values (%s, %s, %s) returning {SESSION_COLUMNS}",
                    (project_id, user_id, title),
                )
            ).fetchone()
        return session_json(row)

    async def sessions(self, user_id: UUID, project_id: UUID) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    f"select {SESSION_COLUMNS} from chat_sessions "
                    "where user_id = %s and project_id = %s and deleted_at is null "
                    "order by updated_at desc, id desc",
                    (user_id, project_id),
                )
            ).fetchall()
        return [session_json(row) for row in rows]

    async def session(self, user_id: UUID, session_id: UUID) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    f"select {SESSION_COLUMNS} from chat_sessions "
                    "where id = %s and user_id = %s and deleted_at is null",
                    (session_id, user_id),
                )
            ).fetchone()
        if row is None:
            raise ChatFailure("CHAT_SESSION_NOT_FOUND")
        return row

    async def rename(
        self, user_id: UUID, session_id: UUID, title: str
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "update chat_sessions set title = %s, title_set_by_user = true "
                    "where id = %s and user_id = %s and deleted_at is null "
                    f"returning {SESSION_COLUMNS}",
                    (title, session_id, user_id),
                )
            ).fetchone()
        if row is None:
            raise ChatFailure("CHAT_SESSION_NOT_FOUND")
        return session_json(row)

    async def delete_session(self, user_id: UUID, session_id: UUID) -> list[UUID]:
        async with self.pool.connection() as conn:
            turns = await self._generating_turns(
                conn, "s.id = %s and s.user_id = %s", (session_id, user_id)
            )
            row = await (
                await conn.execute(
                    "delete from chat_sessions where id = %s and user_id = %s "
                    "returning id",
                    (session_id, user_id),
                )
            ).fetchone()
        if row is None:
            raise ChatFailure("CHAT_SESSION_NOT_FOUND")
        return turns

    async def delete_user_data(self, user_id: UUID) -> list[UUID]:
        async with self.pool.connection() as conn:
            turns = await self._generating_turns(conn, "s.user_id = %s", (user_id,))
            await conn.execute(
                "delete from chat_sessions where user_id = %s", (user_id,)
            )
            await conn.execute("delete from chat_usage where user_id = %s", (user_id,))
            for table in ("setting_check_runs", "setting_check_verdicts"):
                await conn.execute(
                    f"delete from {table} where user_id = %s", (user_id,)
                )
        return turns

    async def delete_project_data(self, user_id: UUID, project_id: UUID) -> list[UUID]:
        async with self.pool.connection() as conn:
            turns = await self._generating_turns(
                conn, "s.user_id = %s and s.project_id = %s", (user_id, project_id)
            )
            for table in (
                "chat_sessions",
                "setting_check_runs",
                "setting_check_verdicts",
            ):
                await conn.execute(
                    f"delete from {table} where user_id = %s and project_id = %s",
                    (user_id, project_id),
                )
        return turns

    async def _generating_turns(
        self, conn: AsyncConnection, where: str, params: tuple[Any, ...]
    ) -> list[UUID]:
        rows = await (
            await conn.execute(
                "select t.id from chat_turns t join chat_sessions s on s.id = t.session_id "
                f"where t.status = 'GENERATING' and {where}",
                params,
            )
        ).fetchall()
        return [row["id"] for row in rows]

    async def messages(self, session_id: UUID) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    f"select {MESSAGE_COLUMNS} from chat_messages "
                    "where session_id = %s and status = 'COMPLETE' "
                    "order by created_at, id",
                    (session_id,),
                )
            ).fetchall()
        return [message_json(row) for row in rows]

    async def generating(self, session_id: UUID) -> bool:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "select exists (select 1 from chat_turns where session_id = %s "
                    "and status = 'GENERATING' "
                    "and created_at > now() - make_interval(secs => %s)) as busy",
                    (session_id, self.stale_seconds),
                )
            ).fetchone()
        return row["busy"]

    async def expire_stale(self) -> int:
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "update chat_turns set status = 'FAILED', failure_code = 'STALE', "
                "finished_at = now() where status = 'GENERATING' "
                "and created_at <= now() - make_interval(secs => %s)",
                (self.stale_seconds,),
            )
        return result.rowcount

    async def begin_turn(
        self,
        user_id: UUID,
        session_id: UUID,
        content: str,
        intent: str,
        quote: dict[str, Any] | None,
        replaces: UUID | None,
        client_message_id: UUID,
    ) -> Started | Replayed:
        async with self.pool.connection() as conn, conn.transaction():
            session = await (
                await conn.execute(
                    "select id, project_id, title, title_set_by_user from chat_sessions "
                    "where id = %s and user_id = %s and deleted_at is null for update",
                    (session_id, user_id),
                )
            ).fetchone()
            if session is None:
                raise ChatFailure("CHAT_SESSION_NOT_FOUND")
            await conn.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(user_id),),
            )
            await conn.execute(
                "update chat_turns t set status = 'FAILED', failure_code = 'STALE', "
                "finished_at = now() from chat_sessions s "
                "where t.session_id = s.id and s.user_id = %s "
                "and t.status = 'GENERATING' "
                "and t.created_at <= now() - make_interval(secs => %s)",
                (user_id, self.stale_seconds),
            )
            if await self._busy(conn, session_id):
                raise ChatFailure("CHAT_ANSWER_IN_PROGRESS")
            if await self._user_busy(conn, user_id):
                raise ChatFailure("CHAT_ANOTHER_ANSWER_IN_PROGRESS")

            existing = await (
                await conn.execute(
                    f"select {MESSAGE_COLUMNS} from chat_messages "
                    "where session_id = %s and client_message_id = %s",
                    (session_id, client_message_id),
                )
            ).fetchone()
            if existing is not None:
                replay = await self._replay(conn, existing)
                if replay is not None:
                    return replay
                if await self._last_question_id(conn, session_id) != existing["id"]:
                    raise ChatFailure("CHAT_NOT_LAST_QUESTION")
                turn_id = await self._insert_turn(conn, session_id, existing["id"])
                return Started(session, existing, turn_id)

            if replaces is not None:
                last = await (
                    await conn.execute(
                        "select id, created_at from chat_messages "
                        "where session_id = %s and role = 'USER' "
                        "order by created_at desc, id desc limit 1",
                        (session_id,),
                    )
                ).fetchone()
                if last is None or last["id"] != replaces:
                    raise ChatFailure("CHAT_NOT_LAST_QUESTION")
                await conn.execute(
                    "delete from chat_messages where session_id = %s "
                    "and (created_at, id) >= (%s, %s)",
                    (session_id, last["created_at"], last["id"]),
                )

            first = (
                await (
                    await conn.execute(
                        "select not exists (select 1 from chat_messages "
                        "where session_id = %s) as first",
                        (session_id,),
                    )
                ).fetchone()
            )["first"]
            question = await (
                await conn.execute(
                    "insert into chat_messages (session_id, role, content_md, status, "
                    "intent, quote, client_message_id) "
                    "values (%s, 'USER', %s, 'COMPLETE', %s, %s, %s) "
                    f"returning {MESSAGE_COLUMNS}",
                    (
                        session_id,
                        content,
                        intent,
                        Jsonb(quote) if quote else None,
                        client_message_id,
                    ),
                )
            ).fetchone()
            if first and not session["title_set_by_user"]:
                title = chat_title(content or (quote or {}).get("text", ""))
                if title:
                    await conn.execute(
                        "update chat_sessions set title = %s where id = %s",
                        (title, session_id),
                    )
            await conn.execute(
                "update chat_sessions set updated_at = now() where id = %s",
                (session_id,),
            )
            turn_id = await self._insert_turn(conn, session_id, question["id"])
            return Started(session, question, turn_id)

    async def _busy(self, conn: AsyncConnection, session_id: UUID) -> bool:
        row = await (
            await conn.execute(
                "select exists (select 1 from chat_turns where session_id = %s "
                "and status = 'GENERATING') as busy",
                (session_id,),
            )
        ).fetchone()
        return row["busy"]

    async def _user_busy(self, conn: AsyncConnection, user_id: UUID) -> bool:
        row = await (
            await conn.execute(
                "select exists (select 1 from chat_turns t "
                "join chat_sessions s on s.id = t.session_id "
                "where s.user_id = %s and t.status = 'GENERATING') as busy",
                (user_id,),
            )
        ).fetchone()
        return row["busy"]

    async def _replay(
        self, conn: AsyncConnection, question: dict[str, Any]
    ) -> Replayed | None:
        row = await (
            await conn.execute(
                f"select {', '.join('a.' + c.strip() for c in MESSAGE_COLUMNS.split(','))} "
                "from chat_turns t join chat_messages a on a.id = t.answer_id "
                "where t.question_id = %s and t.status = 'COMPLETE' "
                "order by t.created_at desc limit 1",
                (question["id"],),
            )
        ).fetchone()
        if row is None:
            return None
        return Replayed(message_json(question), message_json(row))

    async def _last_question_id(
        self, conn: AsyncConnection, session_id: UUID
    ) -> UUID | None:
        row = await (
            await conn.execute(
                "select id from chat_messages where session_id = %s and role = 'USER' "
                "order by created_at desc, id desc limit 1",
                (session_id,),
            )
        ).fetchone()
        return row["id"] if row else None

    async def _insert_turn(
        self, conn: AsyncConnection, session_id: UUID, question_id: UUID
    ) -> UUID:
        try:
            async with conn.transaction():
                row = await (
                    await conn.execute(
                        "insert into chat_turns (session_id, question_id, status) "
                        "values (%s, %s, 'GENERATING') returning id",
                        (session_id, question_id),
                    )
                ).fetchone()
        except UniqueViolation:
            raise ChatFailure("CHAT_ANSWER_IN_PROGRESS") from None
        return row["id"]

    async def history(self, session_id: UUID, question_id: UUID) -> list[Exchange]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    f"select {MESSAGE_COLUMNS} from chat_messages "
                    "where session_id = %s and status = 'COMPLETE' "
                    "order by created_at, id",
                    (session_id,),
                )
            ).fetchall()
        exchanges: list[Exchange] = []
        pending: dict[str, Any] | None = None
        for row in rows:
            if row["id"] == question_id:
                break
            if row["role"] == "USER":
                pending = row
            elif pending is not None:
                exchanges.append(
                    Exchange(
                        question=Question(
                            content=pending["content_md"],
                            intent=pending["intent"] or "ask",
                            quote=pending["quote"],
                        ),
                        answer=row["content_md"],
                        citations=row["citations"],
                    )
                )
                pending = None
        return exchanges

    async def held(self, session_id: UUID) -> list[tuple[str, str]]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select target_id, kind from chat_held_documents "
                    "where session_id = %s order by first_read_at, target_id",
                    (session_id,),
                )
            ).fetchall()
        return [(str(row["target_id"]), row["kind"]) for row in rows]

    async def drop_held(self, session_id: UUID, target_ids: list[str]) -> None:
        if not target_ids:
            return
        async with self.pool.connection() as conn:
            await conn.execute(
                "delete from chat_held_documents "
                "where session_id = %s and target_id = any(%s::uuid[])",
                (session_id, target_ids),
            )

    async def summaries(self, document_ids: list[str]) -> dict[str, Summary]:
        if not document_ids:
            return {}
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select document_id, project_id, revision_no, summary_enc, "
                    "wrapped_key from document_summaries "
                    "where document_id = any(%s::uuid[])",
                    (document_ids,),
                )
            ).fetchall()
        if not rows:
            return {}
        return await asyncio.to_thread(self.open_summaries, rows)

    def open_summaries(self, rows: list[dict[str, Any]]) -> dict[str, Summary]:
        keys: dict[tuple[str, bytes], bytes | None] = {}
        summaries: dict[str, Summary] = {}
        for row in rows:
            document, project = str(row["document_id"]), str(row["project_id"])
            entry = (project, bytes(row["wrapped_key"]))
            if entry not in keys:
                try:
                    keys[entry] = self.keys.unwrap(*entry)
                except KeyUnavailable as failure:
                    keys[entry] = None
                    log.warning(
                        "summary key unavailable",
                        extra={"project_id": project, "code": failure.code},
                    )
            key = keys[entry]
            if key is None:
                continue
            try:
                text = unseal(key, row["summary_enc"], project, document, "summary")
            except Unreadable as failure:
                log.warning(
                    "summary unreadable",
                    extra={"document_id": document, "reason": str(failure)},
                )
                continue
            summaries[document] = Summary(row["revision_no"], text)
        return summaries

    async def turn_status(self, turn_id: UUID) -> str | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "select status from chat_turns where id = %s", (turn_id,)
                )
            ).fetchone()
        return row["status"] if row else None

    async def complete_turn(
        self,
        turn_id: UUID,
        session_id: UUID,
        content: str,
        citations: list[dict[str, Any]],
        metrics: Metrics,
        grounding: dict[str, Any] | None = None,
        held: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any] | None:
        async with self.pool.connection() as conn, conn.transaction():
            turn = await (
                await conn.execute(
                    "select status from chat_turns where id = %s for update",
                    (turn_id,),
                )
            ).fetchone()
            if turn is None or turn["status"] != "GENERATING":
                return None
            answer = await (
                await conn.execute(
                    "insert into chat_messages (session_id, role, content_md, status, "
                    "citations, grounding) "
                    "values (%s, 'ASSISTANT', %s, 'COMPLETE', %s, %s) "
                    f"returning {MESSAGE_COLUMNS}",
                    (
                        session_id,
                        content,
                        Jsonb(citations),
                        Jsonb(grounding) if grounding is not None else None,
                    ),
                )
            ).fetchone()
            if held:
                await conn.execute(
                    "insert into chat_held_documents "
                    "(session_id, target_id, kind, first_read_at) "
                    "select %s, target, kind, "
                    "now() + make_interval(secs => position / 1000000.0) "
                    "from unnest(%s::uuid[], %s::text[]) "
                    "with ordinality as t(target, kind, position) "
                    "on conflict (session_id, target_id) do nothing",
                    (
                        session_id,
                        [target for target, _ in held],
                        [kind for _, kind in held],
                    ),
                )
            await self._finish(conn, turn_id, "COMPLETE", None, metrics, answer["id"])
            await conn.execute(
                "update chat_sessions set preview = %s, updated_at = now() "
                "where id = %s",
                (chat_preview(content, citations), session_id),
            )
        return answer

    async def fail_turn(self, turn_id: UUID, code: str, metrics: Metrics) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await self._finish(conn, turn_id, "FAILED", code, metrics, None)

    async def stop_turn(self, turn_id: UUID, metrics: Metrics) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await self._finish(conn, turn_id, "CANCELLED", None, metrics, None)

    async def _finish(
        self,
        conn: AsyncConnection,
        turn_id: UUID,
        status: str,
        failure_code: str | None,
        metrics: Metrics,
        answer_id: UUID | None,
    ) -> None:
        await conn.execute(
            "update chat_turns set status = %s, failure_code = %s, answer_id = %s, "
            "model = %s, context_chars = %s, input_tokens = %s, cached_tokens = %s, "
            "cache_write_tokens = %s, output_tokens = %s, reasoning_tokens = %s, "
            "snapshot_ms = %s, first_token_ms = %s, total_ms = %s, steps = %s, "
            "judge_calls = %s, chars_read = %s, grounding = %s, rejections = %s, "
            "reviews = %s, finished_at = now() "
            "where id = %s and status = 'GENERATING'",
            (
                status,
                failure_code,
                answer_id,
                metrics.model,
                metrics.context_chars,
                metrics.input_tokens,
                metrics.cached_tokens,
                metrics.cache_write_tokens,
                metrics.output_tokens,
                metrics.reasoning_tokens,
                metrics.snapshot_ms,
                metrics.first_token_ms,
                metrics.total_ms,
                metrics.steps,
                metrics.judge_calls,
                metrics.chars_read,
                metrics.grounding,
                metrics.rejections,
                Jsonb(metrics.reviews) if metrics.reviews is not None else None,
                turn_id,
            ),
        )
        spent = credits(
            metrics.input_tokens,
            metrics.cached_tokens,
            metrics.output_tokens,
            metrics.cache_write_tokens,
        )
        if spent:
            await conn.execute(
                "insert into chat_usage (turn_id, user_id, usage_date, credits) "
                "select t.id, s.user_id, %s, %s from chat_turns t "
                "join chat_sessions s on s.id = t.session_id where t.id = %s "
                "on conflict (turn_id) do nothing",
                (today(), spent, turn_id),
            )

    async def used_today(self, user_id: UUID) -> int:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "select coalesce(sum(credits), 0) as used from chat_usage "
                    "where user_id = %s and usage_date = %s",
                    (user_id, today()),
                )
            ).fetchone()
        return int(row["used"])

    async def cancel(self, user_id: UUID, session_id: UUID) -> list[UUID]:
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "update chat_turns t set status = 'CANCELLED', finished_at = now() "
                    "from chat_sessions s where t.session_id = s.id and s.id = %s "
                    "and s.user_id = %s and t.status = 'GENERATING' returning t.id",
                    (session_id, user_id),
                )
            ).fetchall()
        return [row["id"] for row in rows]

    async def search(
        self, user_id: UUID, project_id: UUID, term: str
    ) -> list[dict[str, Any]]:
        pattern = like(term)
        async with self.pool.connection() as conn:
            sessions = await (
                await conn.execute(
                    f"select {SESSION_COLUMNS}, (title ilike %(p)s) as in_title "
                    "from chat_sessions s where user_id = %(u)s and project_id = %(pr)s "
                    "and deleted_at is null and (title ilike %(p)s or exists ("
                    "select 1 from chat_messages m where m.session_id = s.id "
                    "and m.status = 'COMPLETE' and (m.content_md ilike %(p)s "
                    "or m.quote->>'text' ilike %(p)s or exists ("
                    "select 1 from jsonb_array_elements(m.citations) c "
                    "where c->>'title' ilike %(p)s)))) "
                    "order by in_title desc, updated_at desc, id desc "
                    f"limit {SEARCH_LIMIT}",
                    {"p": pattern, "u": user_id, "pr": project_id},
                )
            ).fetchall()
            ids = [row["id"] for row in sessions]
            rows = await (
                await conn.execute(
                    "select session_id, content_md, quote, citations from chat_messages "
                    "where session_id = any(%s) and status = 'COMPLETE' "
                    "order by created_at, id",
                    (ids,),
                )
            ).fetchall()
        found: dict[UUID, dict[str, str] | None] = {}
        for row in rows:
            if found.get(row["session_id"]):
                continue
            text = search_text(row["content_md"], row["quote"], row["citations"])
            found[row["session_id"]] = snippet(text, term)
        return [
            {"session": session_json(row), "snippet": found.get(row["id"])}
            for row in sessions
        ]
