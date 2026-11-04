import asyncio
import signal
import threading

import psycopg
from openai import AsyncOpenAI
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.logs import AppLogger, LogField, configure_logging, setup_tracing
from app.manuscript import ProjectKeys, kms_client
from app.model import OpenAISummaryModel
from app.summarizer.consumer import consume
from app.summarizer.settings import SummarizerSettings
from app.summarizer.store import SummaryStore
from app.summarizer.worker import Worker

SERVICE = "ai-chat-summarizer"
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
                AppLogger.current().warn(
                    "database schema not ready",
                    LogField.error_type("PLAINTEXT_COLUMNS_PRESENT"),
                )
        except psycopg.Error as error:
            AppLogger.current().warn(
                "database schema not ready", LogField.error_type(type(error).__name__)
            )
        try:
            await asyncio.wait_for(stop.wait(), SCHEMA_WAIT_SECONDS)
        except TimeoutError:
            pass


async def main() -> None:
    # 다른 줄보다 먼저 로그(ECS JSON)와 trace를 설정한다. DB 쿼리마다 client span을 만든다(로그 줄은 남기지 않는다)
    configure_logging(SERVICE)
    setup_tracing(SERVICE)
    PsycopgInstrumentor().instrument()
    settings = SummarizerSettings.from_environment()
    stop = asyncio.Event()
    halt = threading.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    if not settings.openai_api_key:
        # SUMMARY_OPENAI_API_KEY도 OPENAI_API_KEY도 없다
        AppLogger.current().error(
            "openai api key is not set", LogField.error_type("OPENAI_API_KEY_MISSING")
        )
        raise SystemExit(1)
    if not settings.base.manuscript_key_id:
        # 키가 생길 때까지 암호화된 본문은 큐에서 기다린다
        AppLogger.current().warn(
            "manuscript key id is not set",
            LogField.error_type("MANUSCRIPT_KEY_ID_MISSING"),
        )
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
    AppLogger.current().info(
        "summarizer started", LogField.param("topics", list(settings.topics))
    )
    consumer = asyncio.create_task(asyncio.to_thread(consume, settings, halt))
    ticking = asyncio.create_task(worker.run(stop))
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {consumer, ticking, stopping}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            failure = None if task is stopping else task.exception()
            if failure is not None:
                # 예외 메시지에는 SQL·브로커 주소가 섞일 수 있어 클래스 이름과 메시지 없는 스택만 남긴다
                AppLogger.current().error(
                    "summarizer stopped",
                    LogField.error_type(type(failure).__name__),
                    LogField.safe_stack_trace(failure),
                )
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
