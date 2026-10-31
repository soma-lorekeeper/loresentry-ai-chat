import asyncio
import logging
import signal
import threading

import psycopg
from openai import AsyncOpenAI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app import logs
from app.manuscript import ProjectKeys, kms_client
from app.model import OpenAISummaryModel
from app.summarizer.consumer import consume
from app.summarizer.settings import SummarizerSettings
from app.summarizer.store import SummaryStore
from app.summarizer.worker import Worker

log = logging.getLogger("ai_chat.summarizer")

SCHEMA_WAIT_SECONDS = 5


async def wait_for_schema(settings: SummarizerSettings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with await psycopg.AsyncConnection.connect(
                settings.base.conninfo
            ) as conn:
                # This pod does not run flyway; the API pod does. Wait until the
                # columns this code writes exist, not just the tables.
                await conn.execute(
                    "select event_seq, body_enc, wrapped_key from summary_queue limit 0"
                )
                await conn.execute(
                    "select event_seq, summary_enc, wrapped_key "
                    "from document_summaries limit 0"
                )
                await conn.execute("select project_id from setting_check_runs limit 0")
                await conn.execute(
                    "select project_id from setting_check_verdicts limit 0"
                )
                plaintext = await (
                    await conn.execute(
                        "select count(*) from information_schema.columns "
                        "where table_schema = current_schema() and "
                        "(table_name, column_name) in "
                        "(('summary_queue', 'body'), ('document_summaries', 'summary'))"
                    )
                ).fetchone()
                if plaintext == (0,):
                    return
                log.warning("waiting for schema", extra={"error": "plaintext columns"})
        except psycopg.Error as error:
            log.warning("waiting for schema", extra={"error": type(error).__name__})
        try:
            await asyncio.wait_for(stop.wait(), SCHEMA_WAIT_SECONDS)
        except TimeoutError:
            pass


async def main() -> None:
    logs.configure()
    settings = SummarizerSettings.from_environment()
    stop = asyncio.Event()
    halt = threading.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    if not settings.openai_api_key:
        log.error("SUMMARY_OPENAI_API_KEY or OPENAI_API_KEY is not set")
        raise SystemExit(1)
    if not settings.base.manuscript_key_id:
        log.warning("MANUSCRIPT_KEY_ID is not set; encrypted bodies wait for it")
    client = AsyncOpenAI(api_key=settings.openai_api_key, max_retries=2)
    await wait_for_schema(settings, stop)
    if stop.is_set():
        await client.close()
        return
    pool = AsyncConnectionPool(
        settings.base.conninfo,
        min_size=1,
        max_size=settings.workers + 1,
        kwargs={"row_factory": dict_row, "autocommit": True},
        # Test each connection before handing it out, so a DB restart
        # costs a reconnect instead of a failed job.
        check=AsyncConnectionPool.check_connection,
        open=False,
    )
    await pool.open()
    model = OpenAISummaryModel(
        client,
        settings.model,
        settings.reasoning_effort,
        settings.max_output_tokens,
        settings.base.openai_timeout_seconds,
    )
    keys = ProjectKeys(
        kms_client(settings.base.manuscript_key_region),
        settings.base.manuscript_key_id,
    )
    worker = Worker(SummaryStore(pool), model, settings, keys)
    log.info(
        "summarizer started",
        extra={"topics": list(settings.topics), "group": settings.group_id},
    )
    consumer = asyncio.create_task(asyncio.to_thread(consume, settings, halt))
    ticking = asyncio.create_task(worker.run(stop))
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {consumer, ticking, stopping}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if task is not stopping and task.exception() is not None:
                log.error("summarizer stopped", exc_info=task.exception())
                raise SystemExit(1)
    finally:
        stop.set()
        halt.set()
        await asyncio.gather(consumer, ticking, return_exceptions=True)
        stopping.cancel()
        await client.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
