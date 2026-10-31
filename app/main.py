import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app import db, logs
from app.content import ContentClient, ContentSource
from app.errors import ChatFailure, handle_failure, handle_unexpected, handle_validation
from app.manuscript import ProjectKeys, kms_client
from app.model import (
    AnswerModel,
    CheckModel,
    OpenAICheckModel,
    OpenAIModel,
    UnconfiguredModel,
)
from app.routes import Services, router
from app.setting_check.routes import router as check_router
from app.setting_check.runner import CheckRunner
from app.setting_check.store import CheckStore
from app.settings import Settings
from app.store import Store
from app.turns import TurnRunner

SERVICE = "ai-chat-api"

log = logging.getLogger("ai_chat")


def create_app(
    settings: Settings | None = None,
    content: ContentSource | None = None,
    model: AnswerModel | None = None,
    checker: CheckModel | None = None,
    keys: ProjectKeys | None = None,
) -> FastAPI:
    config = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logs.configure()
        async with AsyncExitStack() as stack:
            pool = AsyncConnectionPool(
                config.conninfo,
                min_size=1,
                max_size=config.db_pool_size,
                kwargs={"row_factory": dict_row, "autocommit": True},
                # Test each connection before handing it out, so a DB restart
                # costs a reconnect instead of a failed request.
                check=AsyncConnectionPool.check_connection,
                open=False,
            )
            await pool.open(wait=False)
            stack.push_async_callback(pool.close)
            source = content
            if source is None:
                http = httpx.AsyncClient(
                    base_url=config.content_base_url,
                    timeout=config.content_timeout_seconds,
                )
                stack.push_async_callback(http.aclose)
                source = ContentClient(http)
            client = None
            if model is None or checker is None:
                if config.openai_api_key:
                    client = AsyncOpenAI(api_key=config.openai_api_key, max_retries=2)
                    stack.push_async_callback(client.close)
                else:
                    log.error("OPENAI_API_KEY is not set; answers will fail")
            answers = model
            if answers is None:
                answers = (
                    OpenAIModel(
                        client,
                        config.openai_model,
                        config.openai_reasoning_effort,
                        config.openai_max_output_tokens,
                        config.openai_timeout_seconds,
                        config.judge,
                        config.judge_reasoning_effort,
                        config.judge_max_output_tokens,
                    )
                    if client
                    else UnconfiguredModel(config.openai_model)
                )
            checks_model = checker
            if checks_model is None:
                checks_model = (
                    OpenAICheckModel(
                        client,
                        config.openai_model,
                        config.setting_check_reasoning_effort,
                        config.openai_max_output_tokens,
                        config.judge,
                        config.setting_check_judge_reasoning_effort,
                        config.setting_check_judge_max_output_tokens,
                        config.setting_check_timeout_seconds,
                    )
                    if client
                    else UnconfiguredModel(config.openai_model)
                )
            if keys is None and not config.manuscript_key_id:
                log.warning(
                    "MANUSCRIPT_KEY_ID is not set; encrypted summaries are left out"
                )
            store = Store(
                pool,
                config.stale_turn_seconds,
                keys
                or ProjectKeys(
                    kms_client(config.manuscript_key_region), config.manuscript_key_id
                ),
            )
            runner = TurnRunner(store, source, answers, config)
            check_store = CheckStore(pool, config.setting_check_stale_seconds)
            checks = CheckRunner(check_store, source, checks_model, config)
            app.state.services = Services(config, store, source, runner, checks)
            try:
                async with asyncio.timeout(5):
                    await store.expire_stale()
            except Exception:
                log.warning("could not expire stale turns at startup")
            try:
                async with asyncio.timeout(5):
                    await check_store.expire_stale()
            except Exception:
                log.warning("could not expire stale setting checks at startup")
            yield
            await asyncio.gather(runner.drain(), checks.drain())

    app = FastAPI(title=SERVICE, lifespan=lifespan)
    app.add_exception_handler(ChatFailure, handle_failure)
    app.add_exception_handler(RequestValidationError, handle_validation)
    app.add_exception_handler(Exception, handle_unexpected)
    app.include_router(router)
    app.include_router(check_router)

    @app.get("/")
    def root() -> dict[str, str]:
        return {"service": SERVICE}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/db")
    def health_db() -> JSONResponse:
        try:
            result = db.check()
        except Exception as exception:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "service": SERVICE,
                    "error": str(exception),
                },
            )
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "service": SERVICE, **result},
        )

    return app


app = create_app()
