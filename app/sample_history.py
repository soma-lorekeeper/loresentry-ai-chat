import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.content import ContentSource, Node
from app.setting_check.findings import fingerprint
from app.text import chat_preview, chat_title

KOREAN = Path(__file__).parent / "sample_history" / "ko.json"
MODEL = "sample-replay"


def load(path: Path = KOREAN) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def titles(data: dict[str, Any]) -> set[str]:
    check = data["setting_check"]
    needed = {check["document"]}
    needed |= {finding["source"]["title"] for finding in check["findings"]}
    for session in data["sessions"]:
        for message in session["messages"]:
            needed |= {citation["title"] for citation in message.get("citations", [])}
    return needed


async def seed(
    pool: AsyncConnectionPool,
    content: ContentSource,
    user_id: UUID,
    project_id: UUID,
    data: dict[str, Any],
) -> bool:
    chat_map = await content.map(user_id, project_id)
    nodes = {node.title: node for node in (*chat_map.manuscripts, *chat_map.settings)}
    if not titles(data) <= nodes.keys():
        return False
    now = datetime.now(UTC)
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"sample-history:{project_id}",),
        )
        taken = await (
            await conn.execute(
                "select exists (select 1 from chat_sessions where project_id = %s) "
                "or exists (select 1 from setting_check_runs where project_id = %s) "
                "as taken",
                (project_id, project_id),
            )
        ).fetchone()
        if taken["taken"]:
            return False
        for session in data["sessions"]:
            await insert_session(conn, user_id, project_id, session, nodes, now)
        await insert_check(conn, user_id, project_id, data["setting_check"], nodes, now)
    return True


def cited(
    citations: list[dict[str, Any]], nodes: dict[str, Node]
) -> list[dict[str, Any]]:
    return [
        {**citation, "file_id": nodes[citation["title"]].id} for citation in citations
    ]


async def insert_session(
    conn: AsyncConnection,
    user_id: UUID,
    project_id: UUID,
    session: dict[str, Any],
    nodes: dict[str, Node],
    now: datetime,
) -> None:
    start = now - timedelta(seconds=session["starts_before_seconds"])
    messages = session["messages"]
    question = next(message for message in messages if message["role"] == "user")
    answer = [message for message in messages if message["role"] == "assistant"][-1]
    ended = start + timedelta(seconds=messages[-1]["offset_seconds"])
    row = await (
        await conn.execute(
            "insert into chat_sessions "
            "(project_id, user_id, title, preview, created_at, updated_at) "
            "values (%s, %s, %s, %s, %s, %s) returning id",
            (
                project_id,
                user_id,
                chat_title(question["content"]),
                chat_preview(answer["content"], cited(answer["citations"], nodes)),
                start,
                ended,
            ),
        )
    ).fetchone()
    for message in messages:
        await conn.execute(
            "insert into chat_messages "
            "(session_id, role, content_md, status, intent, citations, created_at) "
            "values (%s, %s, %s, 'COMPLETE', %s, %s, %s)",
            (
                row["id"],
                message["role"].upper(),
                message["content"],
                message.get("intent"),
                Jsonb(cited(message.get("citations", []), nodes)),
                start + timedelta(seconds=message["offset_seconds"]),
            ),
        )


async def insert_check(
    conn: AsyncConnection,
    user_id: UUID,
    project_id: UUID,
    check: dict[str, Any],
    nodes: dict[str, Node],
    now: datetime,
) -> None:
    target = nodes[check["document"]]
    started = now - timedelta(seconds=check["starts_before_seconds"])
    read = check["read"]
    run = await (
        await conn.execute(
            "insert into setting_check_runs (project_id, user_id, document_id, "
            "document_title, revision_no, status, stage, read_manuscripts, "
            "read_from_ordinal, read_to_ordinal, read_settings, read_core_facts, model, "
            "created_at, finished_at) "
            "values (%s, %s, %s, %s, %s, 'DONE', null, %s, %s, %s, %s, %s, %s, %s, %s) "
            "returning id",
            (
                project_id,
                user_id,
                target.id,
                target.title,
                target.revision_no,
                read["manuscripts"],
                read["from_ordinal"],
                read["to_ordinal"],
                read["settings"],
                read["core_facts"],
                MODEL,
                started,
                started + timedelta(seconds=check["duration_seconds"]),
            ),
        )
    ).fetchone()
    for position, finding in enumerate(check["findings"]):
        source = finding["source"]
        node = nodes[source["title"]]
        await conn.execute(
            "insert into setting_check_findings (run_id, position, kind, core, "
            "confidence, explanation, quote, quote_focus, source_document_id, "
            "source_title, source_doc_type, source_ordinal, source_quote, source_focus, "
            "fingerprint) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run["id"],
                position,
                finding["kind"],
                finding["core"],
                finding["confidence"],
                finding["explanation"],
                finding["quote"],
                finding["quote_focus"],
                node.id,
                node.title,
                node.doc_type,
                node.ordinal,
                source["quote"],
                source["quote_focus"],
                fingerprint(
                    finding["kind"], node.id, source["quote"], finding["quote"]
                ),
            ),
        )
