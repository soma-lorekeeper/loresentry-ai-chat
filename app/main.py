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
from app.model import AnswerModel, OpenAIModel, UnconfiguredModel
from app.routes import Services, router
from app.settings import Settings
from app.store import Store
from app.turns import TurnRunner

SERVICE = "ai-chat-api"

log = logging.getLogger("ai_chat")


def create_app(
    settings: Settings | None = None,
    content: ContentSource | None = None,
    model: AnswerModel | None = None,
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
            answers = model
            if answers is None and not config.openai_api_key:
                log.error("OPENAI_API_KEY is not set; answers will fail")
                answers = UnconfiguredModel(config.openai_model)
            if answers is None:
                client = AsyncOpenAI(api_key=config.openai_api_key, max_retries=0)
                stack.push_async_callback(client.close)
                answers = OpenAIModel(
                    client,
                    config.openai_model,
                    config.openai_reasoning_effort,
                    config.openai_max_output_tokens,
                    config.openai_timeout_seconds,
                )
            store = Store(pool, config.stale_turn_seconds)
            runner = TurnRunner(store, source, answers, config)
            app.state.services = Services(config, store, source, runner)
            try:
                async with asyncio.timeout(5):
                    await store.expire_stale()
            except Exception:
                log.warning("could not expire stale turns at startup")
            yield
            await runner.drain()

    app = FastAPI(title=SERVICE, lifespan=lifespan)
    app.add_exception_handler(ChatFailure, handle_failure)
    app.add_exception_handler(RequestValidationError, handle_validation)
    app.add_exception_handler(Exception, handle_unexpected)
    app.include_router(router)

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
